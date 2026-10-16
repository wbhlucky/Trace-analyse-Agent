"""File-backed adapter, kept as the rollback path.

This exists so ``TRACE_AGENT_MEMORY_STORE=json`` is a one-variable revert to
pre-P1 behaviour rather than a code removal. It subclasses the legacy backend
instead of wrapping it, so the read/write surface is literally the same code
that has been in production; the adapter only adds the governance members of
:class:`~trace_agent.memory.ports.MemoryStore`.

Two honest limitations, both documented rather than papered over:

* **No ``search``.** The class deliberately does not implement it, so
  :func:`~trace_agent.memory.ports.store_supports_pushdown` reports ``False``
  and recall falls back to scanning. Pretending to index would hide the exact
  difference the SQLite store was introduced to provide.

   Embeddings are the exception: this adapter *does* implement the vector
   members, because "the rollback switch costs you semantic recall" is a real
   regression rather than a documented difference in speed. Vectors live in an
   append-only ``governance/embeddings.jsonl`` sidecar next to the versions and
   links sidecars, so a corpus can move between adapters without losing its
   semantic channel - and both sides quantise through the same float32 helpers,
   which is what makes their cosines equal rather than approximately equal.
* **No access tracking.** ``last_accessed_at`` stays a SQL-only column, so
  ``touch`` is a no-op here. Adding it to the file layout would mean inventing
  a second schema for a path whose purpose is to be the old one.

  Retention *is* honoured, because P2 moved ``expires_at`` onto
  :class:`~trace_agent.memory.models.CuratedMemory` itself: the deadline is a
  property of the record, not of one adapter, and a rollback that silently
  stopped expiring stale memory would be a rollback into a worse lifecycle.
  ``expire`` therefore archives on both stores.

Namespace isolation *is* supported, because losing it would silently break the
eval separation section 8 depends on. Records for a non-default namespace live
under ``<root>/ns/<namespace>/``; the default namespace keeps the historical
``<root>/`` layout so existing corpora are read unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trace_agent.memory.backend import JsonMemoryBackend, _safe_name
from trace_agent.memory.embedding.ports import (
    cosine,
    round_trip,
    unpack_vector,
)
from trace_agent.memory.episode import apply_grade
from trace_agent.memory.flags import MEMORY_NAMESPACE_DEFAULT, memory_namespace
from trace_agent.memory.models import (
    EpisodicMemory,
    CuratedMemory,
    MemoryStatus,
    SessionMemory,
)
from trace_agent.memory.ports import (
    MemoryRelation,
    MemoryQuery,
    MemoryStats,
    MemoryTrust,
    PendingEmbedding,
    ScoredMemory,
    memory_type_for,
)
from trace_agent.memory.search_text import (
    curated_index_text,
    episode_index_text,
)
from trace_agent.models import utc_now

# ``memory_links.relation`` value counted by :attr:`MemoryStats.contradictions`.
# Taken from the shared vocabulary in ``ports`` rather than the SQL adapter, so
# this module keeps no sqlite dependency - which is the point of it being the
# fallback - while still agreeing with it on the one string the gate reads.
CONTRADICTS_RELATION = MemoryRelation.CONTRADICTS.value
DUPLICATE_RELATION = MemoryRelation.DUPLICATE_OF.value

# Sidecar holding one append-only line per stored vector.
EMBEDDINGS_SIDECAR = "embeddings.jsonl"

# Appends tolerated before the sidecar is rewritten to drop superseded lines.
# A rewrite is O(corpus); without it a long-lived file adapter would carry every
# vector it ever held, including ones whose record was re-embedded three times.
# 256 bounds the waste at a few hundred lines while keeping the amortised cost
# of one ``set_embedding`` effectively constant.
_EMBEDDING_COMPACT_AFTER = 256


def namespaced_root(root: Path, namespace: str) -> Path:
    """Directory holding one namespace's records.

    The default namespace maps to ``root`` itself. That is a compatibility
    decision, not an oversight: every corpus written before P1 lives directly
    under ``root``, and re-homing it would strand existing memory.
    """
    if namespace == MEMORY_NAMESPACE_DEFAULT:
        return Path(root)
    return Path(root) / "ns" / _safe_name(namespace)


def _count_records(root: Path) -> dict[str, Any]:
    """Record counts and newest mtime for one namespace directory.

    Counting files rather than parsing them: this backs an inspection surface,
    and reading every record to answer "how big is this domain" would make
    ``memory namespaces`` the most expensive command in the CLI. An mtime is
    enough for ``last_updated`` because every write here rewrites the file.
    """
    counts: dict[str, Any] = {
        "memories": 0,
        "episodes": 0,
        "sessions": 0,
        "last_updated": "",
    }
    newest = 0.0
    for key, directory in (
        ("memories", "curated"),
        ("episodes", "episodes"),
        ("sessions", "sessions"),
    ):
        folder = root / directory
        if not folder.is_dir():
            continue
        for path in folder.glob("*.json"):
            counts[key] = int(counts[key]) + 1
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    if newest:
        counts["last_updated"] = datetime.fromtimestamp(
            newest, tz=timezone.utc
        ).isoformat()
    return counts


class JsonStore(JsonMemoryBackend):
    """Legacy file backend presenting the full :class:`MemoryStore` surface."""

    #: Value reported by ``TRACE_AGENT_MEMORY_STORE`` and ``memory stats``.
    store_kind = "json"

    #: Non-empty when SQLite was configured but unusable, so an operator can see
    #: that the corpus silently fell back to files instead of wondering why
    #: recall got slower.
    degraded_reason: str | None = None

    def __init__(self, root: Path | str, *, namespace: str | None = None) -> None:
        self.namespace = (
            namespace if namespace is not None else memory_namespace()
        )
        self.base_root = Path(root)
        super().__init__(namespaced_root(self.base_root, self.namespace))
        # ``(mtime_ns, size) -> parsed sidecar``. Vector reads happen inside
        # recall, which runs several times per analysis; re-parsing a JSONL file
        # on every one would make the rollback path slower than the indexed path
        # it is standing in for.
        self._embedding_cache: (
            tuple[tuple[int, int], dict[str, dict[str, Any]]] | None
        ) = None
        self._embedding_appends = 0

    @property
    def db_path(self) -> Path | None:
        """``None``: there is no database file. Present for uniform diagnostics."""
        return None

    # ------------------------------------------------------------- governance
    @property
    def _governance_dir(self) -> Path:
        return self.root / "governance"

    def _append_jsonl(self, name: str, record: dict[str, Any]) -> None:
        """Append one governance event, best-effort like everything else here.

        Versions and links have no home in the record layout, so they go to
        sidecar JSONL files. That keeps section 7's auditability requirement
        alive on the rollback path instead of silently dropping trust history
        the moment an operator switches stores.
        """
        try:
            path = self._governance_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str))
                handle.write("\n")
        except OSError:
            return

    def _read_jsonl(self, name: str) -> list[dict[str, Any]]:
        path = self._governance_dir / name
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    out.append(record)
        except OSError:
            return []
        return out

    def add_source(
        self,
        memory_id: str,
        *,
        run_id: str,
        episode_id: str = "",
    ) -> bool:
        """Add a run reference, which is what ``distinct_runs`` counts here.

        Semantically equivalent to the SQL ``memory_sources`` insert: the record
        model already derives ``distinct_runs`` from ``run:`` references, so
        appending one that is absent is the same idempotent operation, and
        re-adding an existing run still returns ``False``.
        """
        if not run_id:
            return False
        memory = self.load_curated(memory_id)
        if memory is None:
            return False
        references = list(memory.provenance.references)
        added = False
        for reference in filter(None, [f"run:{run_id}", episode_id]):
            if reference not in references:
                references.append(reference)
                added = True
        if not added:
            return False
        updated = memory.model_copy(
            update={
                "provenance": memory.provenance.model_copy(
                    update={"references": references}
                )
            }
        )
        self.write_curated(updated)
        return True

    def distinct_runs(self, memory_id: str) -> int:
        memory = self.load_curated(memory_id)
        return memory.distinct_runs if memory is not None else 0

    def set_trust(
        self,
        memory_id: str,
        trust: MemoryTrust,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> bool:
        memory = self.load_curated(memory_id)
        if memory is None:
            return False
        previous = memory.status.value
        updated = memory.model_copy(
            update={
                "status": trust,
                "status_reason": reason or None,
                "occurrences": memory.distinct_runs,
            }
        )
        self.write_curated(updated)
        self._append_jsonl(
            "versions.jsonl",
            {
                "memory_id": memory_id,
                "topic": memory.topic,
                "content": memory.content,
                "from_trust": previous,
                "trust": trust.value,
                "confidence": memory.confidence,
                "reason": reason,
                "actor": actor,
                "created_at": utc_now().isoformat(),
            },
        )
        return True

    def revise(
        self,
        memory: CuratedMemory,
        *,
        reason: str,
        actor: str | None = None,
    ) -> bool:
        """Governance edit with its own audit reason, mirroring the SQL adapter.

        The record layout has no version table, so the sidecar carries the same
        fields: switching stores must not switch an operator back to "we know
        something changed but not why".
        """
        previous = self.load_curated(memory.memory_id)
        self.write_curated(memory)
        self._append_jsonl(
            "versions.jsonl",
            {
                "memory_id": memory.memory_id,
                "topic": memory.topic,
                "content": memory.content,
                "from_trust": previous.status.value if previous else None,
                "trust": memory.status.value,
                "confidence": memory.confidence,
                "reason": reason,
                "actor": actor,
                "created_at": utc_now().isoformat(),
            },
        )
        return True

    def link(
        self,
        left: str,
        right: str,
        relation: str,
        *,
        evidence: str | None = None,
    ) -> None:
        """Assert a typed relation, upserting on ``(left, right, relation)``.

        The SQL adapter has ``PRIMARY KEY(memory_id, related_memory_id,
        relation)`` and upserts, so a conflict sweep fired every beat records one
        edge no matter how many times it re-finds the pair. A plain append here
        would make the *same* sweep increment ``count_links`` on every pass, and
        since the gate archives at ``MAX_CONTRADICTIONS`` edges the file adapter
        would retire memories the SQL adapter keeps - one behaviour, two
        adapters, and the difference only visible after enough beats. Rewriting
        the sidecar is cheap: it holds one row per distinct edge, not per event.
        """
        record = {
            "memory_id": left,
            "related_memory_id": right,
            "relation": relation,
            "evidence": evidence,
            "created_at": utc_now().isoformat(),
        }
        links = [
            link
            for link in self._read_jsonl("links.jsonl")
            if not (
                link.get("memory_id") == left
                and link.get("related_memory_id") == right
                and link.get("relation") == relation
            )
        ]
        links.append(record)
        self._rewrite_jsonl("links.jsonl", links)
        # A link is what the quality gate reads as corroboration or as a
        # contradiction, so it can change a trust-filtered recall.
        self.bump_epoch()

    def _rewrite_jsonl(self, name: str, records: list[dict[str, Any]]) -> None:
        """Replace a sidecar wholesale; the upsert counterpart of ``_append_jsonl``."""
        try:
            path = self._governance_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                json.dumps(record, ensure_ascii=False, default=str)
                for record in records
            ]
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError:
            return

    def touch(self, memory_id: str) -> None:
        """No-op: the record layout has no ``last_accessed_at`` to stamp."""
        return None

    def expire(self, now: datetime | None = None) -> int:
        """Archive memories past ``expires_at``; returns how many were retired.

        Mirrors the SQL adapter's semantics exactly - archive rather than
        delete, ``archived_reason='expired'``, and skip anything already
        retired - so retention behaves the same on both sides of the rollback
        switch.
        """
        reference = now or utc_now()
        expired = 0
        for memory in self.list_curated(active_only=False):
            if memory.expires_at is None:
                continue
            if _as_utc(memory.expires_at) > _as_utc(reference):
                continue
            if memory.status is MemoryStatus.ARCHIVED:
                continue
            self.write_curated(
                memory.model_copy(
                    update={
                        "status": MemoryStatus.ARCHIVED,
                        "archived_reason": "expired",
                        "status_reason": "retention: past expires_at",
                    }
                )
            )
            expired += 1
        return expired

    def count_links(self, memory_id: str, relation: str | None = None) -> int:
        """Edges out of one memory, counted from the sidecar."""
        return sum(
            1
            for link in self._read_jsonl("links.jsonl")
            if link.get("memory_id") == memory_id
            and (relation is None or link.get("relation") == relation)
        )

    def linked_ids(
        self, memory_id: str, relation: str | None = None
    ) -> list[str]:
        """Sidecar neighbours, sorted to match the SQL adapter's ordering."""
        return sorted(
            str(link.get("related_memory_id") or "")
            for link in self._read_jsonl("links.jsonl")
            if link.get("memory_id") == memory_id
            and (relation is None or link.get("relation") == relation)
            and link.get("related_memory_id")
        )

    def list_links(
        self, relation: str | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Sidecar edges, newest first, matching the SQL adapter's shape."""
        links = [
            link
            for link in self._read_jsonl("links.jsonl")
            if relation is None or link.get("relation") == relation
        ]
        links.sort(key=lambda link: str(link.get("created_at") or ""), reverse=True)
        return links[: max(1, int(limit))]

    def recent_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Sidecar transitions, newest first.

        The SQL adapter orders by an autoincrement sequence; the sidecar is
        append-only, so reversing file order is the same thing.
        """
        versions = self._read_jsonl("versions.jsonl")
        versions.reverse()
        return versions[: max(1, int(limit))]

    def import_governance(
        self,
        versions: list[dict[str, Any]],
        links: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Merge a migration bundle's audit trail and edges into the sidecars.

        Same contract as the two SQL adapters - idempotent, returning what was
        actually added - so ``memory migrate`` can target either store without
        knowing which one it is talking to. Identity is computed rather than
        enforced, because a JSONL sidecar has no unique constraint to lean on:
        versions key on ``(memory_id, version, trust, reason, created_at)`` and
        links on the SQL primary key's triple.

        Versions are re-sorted by ``created_at`` after merging. File order *is*
        this adapter's chronology - :meth:`recent_versions` reverses it to get
        "newest first" - so appending an older imported row at the end would make
        the rollback path report a transition order the SQL adapter does not.
        """
        merged_versions = self._read_jsonl("versions.jsonl")
        seen_versions = {_version_key(row) for row in merged_versions}
        added_versions = 0
        for record in versions:
            if not str(record.get("memory_id") or ""):
                continue
            key = _version_key(record)
            if key in seen_versions:
                continue
            seen_versions.add(key)
            merged_versions.append(dict(record))
            added_versions += 1
        if added_versions:
            merged_versions.sort(key=lambda row: str(row.get("created_at") or ""))
            self._rewrite_jsonl("versions.jsonl", merged_versions)

        merged_links = self._read_jsonl("links.jsonl")
        seen_links = {_link_key(row) for row in merged_links}
        added_links = 0
        for record in links:
            key = _link_key(record)
            # An edge missing an endpoint or its relation is not an edge; the SQL
            # adapters reject the same row through ``NOT NULL`` columns.
            if not all(key) or key in seen_links:
                continue
            seen_links.add(key)
            merged_links.append(dict(record))
            added_links += 1
        if added_links:
            self._rewrite_jsonl("links.jsonl", merged_links)
        if added_versions or added_links:
            # Governance feeds the gate's corroboration and contradiction counts,
            # so a trust-filtered recall can change with no record touched.
            self.bump_epoch()
        return added_versions, added_links

    def reindex(self) -> int:
        """No-op: a file scan needs no index, so 0 rows are indexed."""
        return 0

    def stats(self) -> MemoryStats:
        memories = self.list_curated(active_only=False)
        by_trust: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        expired = 0
        for memory in memories:
            by_trust[memory.status.value] = by_trust.get(memory.status.value, 0) + 1
            by_kind[memory.kind.value] = by_kind.get(memory.kind.value, 0) + 1
            bucket = memory_type_for(memory.kind).value
            by_type[bucket] = by_type.get(bucket, 0) + 1
            if memory.archived_reason == "expired":
                expired += 1
        links = self._read_jsonl("links.jsonl")
        return MemoryStats(
            store="json",
            namespace=self.namespace,
            memories=len(memories),
            episodes=len(self.list_episodes()),
            sessions=len(self.list_sessions()),
            by_trust=by_trust,
            by_type=by_type,
            by_kind=by_kind,
            contradictions=sum(
                1 for link in links if link.get("relation") == CONTRADICTS_RELATION
            ),
            duplicates=sum(
                1 for link in links if link.get("relation") == DUPLICATE_RELATION
            ),
            versions=len(self._read_jsonl("versions.jsonl")),
            links=len(links),
            expired=expired,
            superseded=by_trust.get(MemoryStatus.SUPERSEDED.value, 0),
        )

    def close(self) -> None:
        """No connection to release; present so callers can close either store."""
        return None

    # ------------------------------------------------- semantic vectors (P3)
    # The same six members the SQL adapter implements, because "flip one variable
    # to roll back" must not quietly mean "lose semantic recall". Vectors live in
    # an append-only sidecar keyed ``memory:<id>`` / ``episode:<id>`` where the
    # last line for a key wins. That keeps ``set_embedding`` O(1) and lets a
    # tombstone (``vector: null``) retire a single vector without rewriting the
    # file; :meth:`_compact_embeddings` folds the log back down once the waste is
    # bounded but no longer trivial.

    def _embeddings_path(self) -> Path:
        return self._governance_dir / EMBEDDINGS_SIDECAR

    @staticmethod
    def _embedding_key(record_id: str, *, episode: bool) -> str:
        """Namespaced sidecar key: the two record kinds share no id space."""
        return f"{'episode' if episode else 'memory'}:{record_id}"

    def _load_embeddings(self) -> dict[str, dict[str, Any]]:
        """Last-wins view of the sidecar, cached on ``(mtime_ns, size)``."""
        path = self._embeddings_path()
        try:
            stamp = path.stat()
        except OSError:
            self._embedding_cache = None
            return {}
        key = (stamp.st_mtime_ns, stamp.st_size)
        if self._embedding_cache is not None and self._embedding_cache[0] == key:
            return self._embedding_cache[1]
        loaded: dict[str, dict[str, Any]] = {}
        for record in self._read_jsonl(EMBEDDINGS_SIDECAR):
            record_id = str(record.get("id") or "")
            if record_id:
                loaded[record_id] = record
        self._embedding_cache = (key, loaded)
        return loaded

    def _vector_from(
        self, stored: dict[str, dict[str, Any]], record_id: str, *, episode: bool
    ) -> list[float]:
        """One record's vector from an already-loaded sidecar; ``[]`` if absent.

        ``[]`` covers three cases that must behave identically: never embedded,
        tombstoned, and corrupt. All three mean "this record is not in the
        semantic channel", and the next backfill repairs the first two.
        """
        record = stored.get(self._embedding_key(record_id, episode=episode))
        return unpack_vector(record.get("vector")) if record else []

    def _stored_model(
        self, stored: dict[str, dict[str, Any]], record_id: str, *, episode: bool
    ) -> str | None:
        record = stored.get(self._embedding_key(record_id, episode=episode))
        if not record or not record.get("vector"):
            return None
        return str(record.get("model") or "") or None

    def _append_embedding(
        self,
        record_id: str,
        *,
        episode: bool,
        model: str | None,
        vector: Sequence[float] | None,
    ) -> None:
        """One sidecar line. ``vector=None`` is a tombstone, not an empty vector."""
        self._append_jsonl(
            EMBEDDINGS_SIDECAR,
            {
                "id": self._embedding_key(record_id, episode=episode),
                "episode": bool(episode),
                "model": model,
                "vector": list(vector) if vector else None,
                "updated_at": utc_now().isoformat(),
            },
        )
        self._embedding_cache = None
        self._embedding_appends += 1
        if self._embedding_appends >= _EMBEDDING_COMPACT_AFTER:
            self._compact_embeddings()

    def _compact_embeddings(self) -> None:
        """Rewrite the sidecar keeping only each key's live vector."""
        live = [
            record
            for record in self._load_embeddings().values()
            if record.get("vector")
        ]
        self._rewrite_jsonl(EMBEDDINGS_SIDECAR, live)
        self._embedding_cache = None
        self._embedding_appends = 0

    def pending_embeddings(
        self, *, model: str, limit: int = 32
    ) -> list[PendingEmbedding]:
        """Records needing a vector: unembedded, or embedded by another recipe.

        Mirrors the SQL adapter down to the interleaving of the two record kinds.
        One kind after the other would starve whichever came second on a corpus
        where the first never finishes, and the episodic channel is the one a
        paraphrase query needs most. ``active_only=True`` is the file layout's
        spelling of the SQL ``trust NOT IN (retired)`` predicate. Both listings
        already come back newest first, which is the order that matters: a fresh
        run is the record most likely to be recalled before the next beat.
        """
        budget = max(1, int(limit))
        memories = self._pending_records(
            self.list_curated(active_only=True), model, budget, curated_index_text
        )
        episodes = self._pending_records(
            self.list_episodes(), model, budget, episode_index_text, episode=True
        )
        out: list[PendingEmbedding] = []
        for pair in zip(memories, episodes, strict=False):
            out.extend(pair)
        longer, shorter = (
            (memories, episodes)
            if len(memories) >= len(episodes)
            else (episodes, memories)
        )
        out.extend(longer[len(shorter) :])
        return out[:budget]

    def _pending_records(
        self,
        records: Sequence[Any],
        model: str,
        limit: int,
        index_text: Any,
        *,
        episode: bool = False,
    ) -> list[PendingEmbedding]:
        """One record kind's outstanding embedding work."""
        stored = self._load_embeddings()
        out: list[PendingEmbedding] = []
        for record in records:
            if len(out) >= limit:
                break
            record_id = str(
                getattr(record, "episode_id", None)
                or getattr(record, "memory_id", "")
                or ""
            )
            if not record_id:
                continue
            stored_model = self._stored_model(stored, record_id, episode=episode)
            if stored_model == model:
                # Already embedded by the recipe in force. Comparing names rather
                # than widths is deliberate: the store cannot tell whether two
                # recipes are comparable, and guessing wrong yields cosines that
                # look plausible and mean nothing.
                continue
            text = str(index_text(record) or "").strip()
            if not text:
                continue
            out.append(
                PendingEmbedding(
                    record_id=record_id,
                    text=text,
                    episode=episode,
                    stored_model=stored_model,
                )
            )
        return out

    def set_embedding(
        self,
        record_id: str,
        vector: Sequence[float],
        *,
        model: str,
        episode: bool = False,
    ) -> bool:
        """Store one quantised vector; ``True`` only if the record exists.

        Quantising through :func:`round_trip` instead of writing the provider's
        float64 is what makes a cosine computed here *equal* the one the SQL
        adapter computes over its packed blob, rather than approximately equal -
        and "both adapters return the same hits" is only a testable property if
        the numbers agree exactly.
        """
        try:
            values = round_trip([float(item) for item in (vector or ())])
        except (TypeError, ValueError):
            return False
        if not values or not record_id:
            return False
        loader = self.load_episode if episode else self.load_curated
        if loader(record_id) is None:
            # Same as the SQL ``UPDATE ... WHERE id = ?`` touching no rows: a
            # vector for a record that is not there would be invisible to recall
            # and immortal in the sidecar.
            return False
        self._append_embedding(
            record_id, episode=episode, model=model, vector=values
        )
        return True

    def vectors_for(
        self, record_ids: Sequence[str], *, episode: bool = False
    ) -> dict[str, list[float]]:
        """Stored vectors for a set of ids; ids without one are absent."""
        stored = self._load_embeddings()
        out: dict[str, list[float]] = {}
        for record_id in (str(item) for item in (record_ids or ()) if item):
            vector = self._vector_from(stored, record_id, episode=episode)
            if vector:
                out[record_id] = vector
        return out

    def vector_search(
        self, vector: Sequence[float], query: MemoryQuery
    ) -> list[ScoredMemory]:
        """Nearest records to ``vector``, under the query's own filters.

        The predicates are the scan path's - status, scenario (NULL-permissive for
        curated, strict for episodes), namespace - plus the type filter the SQL
        adapter applies. A vector channel that admitted records the lexical
        channel would have dropped would make a query's answer depend on which
        channel happened to find it.

        ``bm25`` stays ``None``: a cosine and a bm25 rank are not comparable, and
        filling the field would let a caller sort a merged pool by a number that
        means two different things.
        """
        try:
            probe = [float(item) for item in (vector or ())]
        except (TypeError, ValueError):
            return []
        if not probe:
            return []
        if query.namespace and query.namespace != self.namespace:
            return []
        stored = self._load_embeddings()
        if not stored:
            # Nothing embedded yet, which is the state a fresh or freshly
            # migrated corpus is in. Returning early is what stops the vector
            # channel from costing two full corpus scans per recall on the one
            # adapter that has no index to narrow them with.
            return []
        pool: list[ScoredMemory] = []
        for memory in self.list_curated(active_only=True):
            if memory.status not in query.statuses:
                continue
            if query.types and memory_type_for(memory.kind) not in query.types:
                continue
            if (
                query.scenario_type
                and memory.scenario_type
                and memory.scenario_type != query.scenario_type
            ):
                continue
            stored_vector = self._vector_from(
                stored, memory.memory_id, episode=False
            )
            if stored_vector:
                pool.append(
                    ScoredMemory(
                        memory=memory,
                        bm25=None,
                        similarity=cosine(probe, stored_vector),
                    )
                )
        if query.include_episodic:
            for episode in self.list_episodes():
                if query.scenario_type and episode.scenario_type != query.scenario_type:
                    continue
                stored_vector = self._vector_from(
                    stored, episode.episode_id, episode=True
                )
                if stored_vector:
                    pool.append(
                        ScoredMemory(
                            memory=episode,
                            bm25=None,
                            similarity=cosine(probe, stored_vector),
                        )
                    )
        pool.sort(
            key=lambda item: (-(item.similarity or 0.0), _record_id(item.memory))
        )
        return pool[: max(1, query.limit)]

    def prune_embeddings(self, *, keep_model: str) -> int:
        """Drop vectors not produced by ``keep_model``; returns how many went.

        ``keep_model=""`` matches nothing and therefore clears everything, which
        is how :meth:`~trace_agent.memory.embedding.index.SemanticIndex.rebuild`
        turns a recipe change into a fresh corpus instead of a mixture whose
        vectors compare as ``0.0`` and silently reorder recall.
        """
        kept = keep_model or ""
        survivors: list[dict[str, Any]] = []
        pruned = 0
        for record in self._load_embeddings().values():
            if not record.get("vector"):
                continue
            if kept and str(record.get("model") or "") == kept:
                survivors.append(record)
            else:
                pruned += 1
        self._rewrite_jsonl(EMBEDDINGS_SIDECAR, survivors)
        self._embedding_cache = None
        self._embedding_appends = 0
        return pruned

    def embedding_coverage(self) -> dict[str, Any]:
        """What is embedded, with which recipe, and what is still outstanding.

        Same keys as the SQL adapter, so ``memory stats`` and the dashboard read
        one shape whichever store is live. Retired records are counted in the
        denominator on both sides.
        """
        stored = self._load_embeddings()
        memories = self.list_curated(active_only=False)
        episodes = self.list_episodes()
        models: dict[str, int] = {}
        memories_embedded = 0
        episodes_embedded = 0
        for memory in memories:
            if not self._vector_from(stored, memory.memory_id, episode=False):
                continue
            memories_embedded += 1
            key = self._stored_model(stored, memory.memory_id, episode=False) or ""
            models[key] = models.get(key, 0) + 1
        for episode in episodes:
            if not self._vector_from(stored, episode.episode_id, episode=True):
                continue
            episodes_embedded += 1
            key = self._stored_model(stored, episode.episode_id, episode=True) or ""
            models[key] = models.get(key, 0) + 1
        return {
            "memories": len(memories),
            "memories_embedded": memories_embedded,
            "episodes": len(episodes),
            "episodes_embedded": episodes_embedded,
            "models": models,
            "unembedded": max(
                0,
                (len(memories) - memories_embedded)
                + (len(episodes) - episodes_embedded),
            ),
        }

    # ---------------------------------------------------- embedding hygiene
    # ------------------------------------------------------- corpus epoch (P4)
    #: Sidecar holding this namespace's write counter and its real name.
    EPOCH_SIDECAR = "epoch.json"

    @property
    def _epoch_path(self) -> Path:
        return self.root / self.EPOCH_SIDECAR

    def _read_epoch(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._epoch_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def corpus_epoch(self) -> int:
        """How many times this namespace's corpus has been written.

        A single counter file rather than a per-record timestamp: the recall
        cache keys on it, so invalidation stays a property of the corpus instead
        of something every writer has to reason about. Missing or corrupt reads
        as ``0``, which is also what an untouched corpus reports - acceptable
        here because the file adapter has no "unreachable" state to confuse it
        with.
        """
        try:
            return int(self._read_epoch().get("epoch", 0))
        except (TypeError, ValueError):
            return 0

    def bump_epoch(self) -> None:
        """Advance the counter, recording the namespace it belongs to.

        The write is read-increment-replace, so two processes writing the same
        corpus can lose an increment. That is a real limitation of the file
        adapter and it is why the counter is a cache hint rather than a
        correctness guarantee: a lost bump leaves a stale entry alive for at
        most one TTL, and multi-writer deployments are exactly the trigger
        section 3.1 lists for moving to PostgreSQL.

        Storing ``namespace`` alongside the number is what makes
        :meth:`list_namespaces` possible - the directory name is
        :func:`_safe_name`-mangled and cannot be reversed.
        """
        payload = {
            "namespace": self.namespace,
            "epoch": self.corpus_epoch() + 1,
            "updated_at": utc_now().isoformat(),
        }
        try:
            path = self._epoch_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            return

    # ----------------------------------------------------- multi-tenancy (P4)
    def list_namespaces(self) -> list[dict[str, Any]]:
        """Every isolation domain under the base root, with its record counts.

        The file layout has no namespace column, so domains are discovered from
        the directory shape :func:`namespaced_root` defines: the base root is the
        default namespace, and each directory under ``ns/`` is one more. Names
        come from the epoch sidecar when it exists and fall back to the mangled
        directory name, which is why :meth:`bump_epoch` writes the name at all.
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(root: Path, fallback: str) -> None:
            sidecar_root = root / self.EPOCH_SIDECAR
            counts = _count_records(root)
            try:
                raw = json.loads(sidecar_root.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                raw = {}
            name = str(
                (raw.get("namespace") if isinstance(raw, dict) else None)
                or fallback
            )
            # Empty domains are left out, matching the SQL adapters: they derive
            # their list from rows, so a namespace that was created but never
            # written does not appear there either. Listing one here would only
            # report a directory the layout happens to have made.
            if not (
                int(counts["memories"]) or int(counts["episodes"]) or int(counts["sessions"])
            ):
                return
            if name in seen:
                return
            seen.add(name)
            out.append(
                {
                    "namespace": name,
                    "memories": counts["memories"],
                    "episodes": counts["episodes"],
                    "sessions": counts["sessions"],
                    "last_updated": counts["last_updated"],
                    "current": name == self.namespace,
                }
            )

        if self.base_root.is_dir():
            add(self.base_root, MEMORY_NAMESPACE_DEFAULT)
            ns_root = self.base_root / "ns"
            if ns_root.is_dir():
                for entry in sorted(ns_root.iterdir()):
                    if entry.is_dir():
                        add(entry, entry.name)
        return sorted(out, key=lambda item: str(item.get("namespace") or ""))

    # ---------------------------------------------------- embedding hygiene
    def write_curated(self, memory: CuratedMemory) -> Path:
        """Write a record, retiring its vector when the indexed text changed."""
        previous = self.load_curated(memory.memory_id)
        path = super().write_curated(memory)
        self._retire_if_changed(
            memory.memory_id,
            episode=False,
            before=curated_index_text(previous) if previous is not None else "",
            after=curated_index_text(memory),
        )
        self.bump_epoch()
        return path

    def write_episode(self, episode: EpisodicMemory) -> Path:
        """Episode counterpart of :meth:`write_curated`."""
        previous = self.load_episode(episode.episode_id)
        path = super().write_episode(episode)
        self._retire_if_changed(
            episode.episode_id,
            episode=True,
            before=episode_index_text(previous) if previous is not None else "",
            after=episode_index_text(episode),
        )
        self.bump_epoch()
        return path

    def write_session(self, session: SessionMemory) -> Path:
        """Session write that also advances the epoch.

        Overridden only for the bump: the inherited writer already restamps
        ``updated_at`` and normalises messages, and duplicating that here would
        give the rollback path a second, drifting definition of "write a
        session".
        """
        path = super().write_session(session)
        self.bump_epoch()
        return path

    def remove_curated(self, memory_id: str) -> bool:
        """Delete a record and advance the epoch.

        Deletion changes recall exactly as much as an insert does, so it has to
        invalidate the cache too; without this a query would keep returning a
        memory that no longer exists until its TTL ran out.
        """
        removed = super().remove_curated(memory_id)
        self.bump_epoch()
        return removed

    def mark_episode_graded(
        self,
        episode_id: str,
        *,
        graded_pass: bool,
        graded_score: float | None = None,
        eval_run_id: str | None = None,
    ) -> bool:
        """File-layout counterpart of the SQL adapter's grade stamp.

        Identical semantics on purpose: the rollback switch must not silently
        drop the eval evidence the gate's ``active -> verified`` path reads, or
        ``TRACE_AGENT_MEMORY_STORE=json`` would mean "grading stops counting".
        """
        episode = self.load_episode(episode_id)
        if episode is None:
            return False
        self.write_episode(
            apply_grade(
                episode,
                graded_pass=graded_pass,
                graded_score=graded_score,
                eval_run_id=eval_run_id,
            )
        )
        return True

    def _retire_if_changed(
        self, record_id: str, *, episode: bool, before: str, after: str
    ) -> None:
        """Tombstone a vector whose source text no longer matches the record.

        The SQL adapter clears its ``embedding`` column in the same situation.
        Recomputing instead would put provider latency on the write path for a
        vector the next dreaming beat produces anyway; in between, the record is
        simply absent from the semantic channel and recall degrades to lexical
        for that one row. ``import_curated`` deliberately bypasses this: a
        migration must not invalidate a corpus it is carrying over.
        """
        if before == after:
            return
        key = self._embedding_key(record_id, episode=episode)
        if not (self._load_embeddings().get(key) or {}).get("vector"):
            return
        self._append_embedding(record_id, episode=episode, model=None, vector=None)

    # ------------------------------------------------------- faithful import
    def import_curated(
        self,
        memory: CuratedMemory,
        *,
        reason: str = "migrate",
        actor: str | None = None,
    ) -> Path:
        """Write a record keeping its stored ``updated_at``.

        ``write_curated`` restamps, which is right for consolidation and wrong
        for migration: ``updated_at`` feeds the freshness term in reranking, so a
        SQLite -> JSON export that restamped would silently reorder recall on the
        rollback path. ``reason``/``actor`` are accepted for signature parity with
        the SQL adapters; the file layout has no version table to record them in,
        so a demotion that lands here is still traceable - just from the record's
        own ``status_reason`` rather than from a history row.
        """
        path = self._write_json(self.curated_dir, memory.memory_id, memory)
        # Imported records are new to *this* corpus even though they are old in
        # the one they came from, so cached results have to go.
        self.bump_epoch()
        return path

    def import_session(self, session: SessionMemory) -> Path:
        """Session counterpart of :meth:`import_curated`."""
        path = self._write_json(self.sessions_dir, session.session_id, session)
        self.bump_epoch()
        return path

    def _write_json(self, directory: Path, key: str, model: Any) -> Path:
        """Serialise one record verbatim, bypassing the restamping writers."""
        path = directory / f"{_safe_name(key)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path


def _as_utc(value: datetime) -> datetime:
    """Compare-aware timestamp: legacy JSON records may predate tz-awareness."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _version_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of one audit row, for an idempotent sidecar import.

    ``created_at`` carries most of the weight: the sidecar has no ``seq`` column
    to order by, so two rows about the same memory are told apart by when they
    happened. ``version`` is included because rows exported from a SQL store
    carry an ordinal, and it is ``None`` for sidecar-native ones - which is why a
    round trip through both adapters can legitimately record one transition
    twice. A duplicated audit row is recoverable; a lost one is not.
    """
    return (
        str(record.get("memory_id") or ""),
        record.get("version"),
        str(record.get("trust") or ""),
        str(record.get("reason") or ""),
        str(record.get("created_at") or ""),
    )


def _link_key(record: dict[str, Any]) -> tuple[str, str, str]:
    """Edge identity, mirroring ``PRIMARY KEY(memory_id, related, relation)``."""
    return (
        str(record.get("memory_id") or ""),
        str(record.get("related_memory_id") or ""),
        str(record.get("relation") or ""),
    )


def _record_id(record: Any) -> str:
    """Stable tie-breaker for a scored row, whichever record shape it holds.

    Sorting by similarity alone leaves equal cosines in whatever order the scan
    produced, so a corpus edited between two runs would reorder its hits. The SQL
    adapter has the same helper for the same reason: recall must be deterministic.
    """
    return str(
        getattr(record, "memory_id", None)
        or getattr(record, "episode_id", "")
        or ""
    )


__all__ = [
    "CONTRADICTS_RELATION",
    "DUPLICATE_RELATION",
    "EMBEDDINGS_SIDECAR",
    "JsonStore",
    "namespaced_root",
]
