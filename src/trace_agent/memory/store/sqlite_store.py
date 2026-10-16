"""SQLite adapter for the long-term memory corpus.

Drop-in replacement for :class:`~trace_agent.memory.backend.JsonMemoryBackend`:
it implements the same legacy surface (``write_curated``, ``list_episodes``,
``append_dream_job``, ...) plus the indexed ``search`` and the governance
members of :class:`~trace_agent.memory.ports.MemoryStore`. Keeping the legacy
surface is what makes ``TRACE_AGENT_MEMORY_STORE`` a genuine rollback switch
instead of two implementations that drift apart.

Two rules hold this adapter to the file backend's behaviour:

* **Reads come from the ``payload`` column**, which holds the full Pydantic
  dump. The structured columns are caches for filtering, ordering and
  analytics, never the source of an object. A stale ``importance`` column
  therefore cannot change what a caller sees, and both adapters return equal
  objects for equal writes.
* **Ordering, timestamps and side effects match the file backend.** Writes
  refresh ``updated_at`` the same way, listings sort the same way, and a
  malformed record is skipped rather than raising, so swapping stores cannot
  change recall results.

Concurrency: the dashboard and the CLI can hold the corpus open at once, so
connections are thread-local, the database runs in WAL, and every write takes
a process-wide lock around an explicit ``BEGIN IMMEDIATE``. That gives one
writer at a time with readers never blocked, and a busy second process waits
instead of failing.

The dream queue and ``execution_snapshot`` stay file-backed on purpose. The
queue is a transient work list whose JSONL append semantics the dreaming beat
already depends on, and a checkpoint snapshot is a projection of a *run*
directory rather than corpus state; neither belongs in the memory database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.embedding.ports import cosine, pack_vector, unpack_vector
from trace_agent.memory.episode import apply_grade
from trace_agent.memory.flags import memory_namespace
from trace_agent.memory.models import (
    RETIRED_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    ExecutionCheckpointSnapshot,
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
from trace_agent.memory.rerank import episode_confidence, importance_for
from trace_agent.memory.search_text import (
    curated_index_text,
    episode_index_text,
)
from trace_agent.memory.store.fts import fts_body, fts_match
from trace_agent.memory.store.rows import (
    VersionResolver,
    as_float,
    optional_int,
    parse_stamp,
    state_key,
    stored_validator,
)
from trace_agent.memory.store.schema import apply_schema, schema_version
from trace_agent.models import utc_now

DB_FILE_NAME = "memory.sqlite3"

# Trust levels ``list_curated(active_only=True)`` hides, taken from the same
# constant the JSON backend hard-codes so both adapters filter identically.
_RETIRED_TRUSTS: tuple[str, ...] = tuple(
    sorted(status.value for status in RETIRED_STATUSES)
)

# ``memory_links.relation`` value counted by :attr:`MemoryStats.contradictions`.
# Aliased from the shared vocabulary so the two adapters cannot drift on the
# one string the quality gate treats as evidence.
CONTRADICTS_RELATION = MemoryRelation.CONTRADICTS.value
DUPLICATE_RELATION = MemoryRelation.DUPLICATE_OF.value


def _dumps(value: Any) -> str:
    """Compact JSON for a column. ``ensure_ascii=False`` keeps CJK readable."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# Aliased from the shared row helpers so the PostgreSQL adapter and this one
# cannot drift on whether an expert sign-off counts as a change.
_stored_validator = stored_validator


def _epoch_key(namespace: str) -> str:
    """``store_meta`` key holding one namespace's write counter.

    Same key shape as the Postgres adapter on purpose: a corpus exported from
    one store and imported into the other keeps a comparable identity for its
    counter, and neither cache implementation has to special-case a backend.
    """
    return f"epoch:{namespace}"


class SqliteStore:
    """SQL-backed memory store with indexed retrieval and governance tables."""

    #: Value reported by ``TRACE_AGENT_MEMORY_STORE`` and ``memory stats``.
    store_kind = "sqlite"

    #: Set by the factory when a *different* store was requested but could not
    #: be used. Always ``None`` on an instance that is actually a SqliteStore.
    degraded_reason: str | None = None

    def __init__(
        self,
        root: Path | str,
        *,
        namespace: str | None = None,
        db_name: str = DB_FILE_NAME,
    ) -> None:
        self.root = Path(root)
        self.db_path = self.root / db_name
        # Resolved once at construction: a namespace that changed mid-process
        # would silently split one corpus across two isolation domains.
        self.namespace = (
            namespace if namespace is not None else memory_namespace()
        )
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # Sidecars that are deliberately still files (dream queue, snapshots).
        self._files = JsonMemoryBackend(self.root)

    # ------------------------------------------------------- connection mgmt
    def _conn(self) -> sqlite3.Connection:
        """This thread's connection, creating the database on first use.

        Lazy on purpose. ``default_memory_runtime`` promises not to touch disk,
        and a CLI invocation that never recalls or remembers anything should
        not leave an empty database file behind.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.root.mkdir(parents=True, exist_ok=True)
        # ``isolation_level=None`` puts the driver in autocommit so the explicit
        # BEGIN/COMMIT in :meth:`_tx` is the only transaction control.
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        apply_schema(conn)
        self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers in this process, then commit or roll back.

        ``BEGIN IMMEDIATE`` takes the write lock up front. Deferring it lets two
        transactions both read and then collide on write, raising ``SQLITE_BUSY``
        even with ``busy_timeout`` set, because SQLite cannot promote a read
        transaction once another writer holds the lock.

        Nesting is supported and is what makes :meth:`batch` work: an inner
        ``_tx`` inside an outer one yields the same connection and commits
        nothing, so callers written against one-record-per-transaction semantics
        can be wrapped in a single transaction without knowing about it.
        """
        conn = self._conn()
        with self._write_lock:
            depth = int(getattr(self._local, "depth", 0))
            if depth:
                self._local.depth = depth + 1
                try:
                    yield conn
                finally:
                    self._local.depth = depth
                return
            conn.execute("BEGIN IMMEDIATE")
            self._local.depth = 1
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                self._local.depth = 0
                raise
            conn.execute("COMMIT")
            self._local.depth = 0

    @contextmanager
    def batch(self) -> Iterator[sqlite3.Connection]:
        """Group many writes into one transaction.

        Migration measured roughly a millisecond per record when each write
        committed on its own, which turns a 10k-memory import into ten seconds of
        mostly fsync. One transaction is also the more honest unit of work here:
        a migration that fails halfway should not leave a partially copied corpus
        that then has to be reasoned about.
        """
        with self._tx() as conn:
            yield conn

    def close(self) -> None:
        """Release this thread's connection. Safe to call more than once."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        try:
            conn.close()
        except sqlite3.Error:
            return

    @property
    def version(self) -> int:
        """Schema version the database file was created with."""
        return schema_version(self._conn())

    # -------------------------------------------------------------- episodes
    def write_episode(self, episode: EpisodicMemory) -> Path:
        """Persist one run's episode projection, keeping one row per run.

        ``UNIQUE (namespace, run_id)`` is the section-4 idempotency rule: a run
        is projected once. Re-analysing a run under a *new* episode id would
        otherwise either violate the constraint or leave two projections of the
        same run competing in recall, so the stale row is retired first - the
        newest projection of a run is the one that counts.
        """
        payload = _dumps(episode.model_dump(mode="json"))
        created = _iso(episode.created_at) or _iso(utc_now()) or ""
        with self._tx() as conn:
            stale = conn.execute(
                "SELECT rowid FROM episodes WHERE namespace = ? AND run_id = ? "
                "AND id <> ?",
                (self.namespace, episode.run_id, episode.episode_id),
            ).fetchall()
            for row in stale:
                conn.execute(
                    "DELETE FROM episodes_fts WHERE rowid = ?", (row["rowid"],)
                )
                conn.execute(
                    "DELETE FROM episodes WHERE namespace = ? AND rowid = ?",
                    (self.namespace, row["rowid"]),
                )
            previous = conn.execute(
                "SELECT scenario_type, scenario, symptom, summary, root_causes, "
                "target_process, device, build "
                "FROM episodes WHERE namespace = ? AND id = ?",
                (self.namespace, episode.episode_id),
            ).fetchone()
            rowid = self._upsert_episode_row(conn, episode, payload, created)
            self._index_episode(conn, rowid, episode)
            # Same rule as :meth:`_put_curated`: re-projecting a run whose
            # narrative changed retires the vector, an identical re-projection
            # keeps it. Comparing the columns the index text is built from,
            # rather than the text itself, avoids recomputing a concatenation
            # for every write on a path that has no provider to call anyway.
            if self._indexed_text_changed(
                previous,
                {
                    "scenario_type": episode.scenario_type,
                    "scenario": episode.scenario,
                    "symptom": episode.symptom,
                    "summary": episode.summary,
                    "root_causes": _dumps(episode.root_causes),
                    "target_process": episode.target_process,
                    "device": episode.device,
                    "build": episode.build,
                },
            ):
                self._clear_embedding(conn, "episodes", episode.episode_id)
            # Invalidates every cached recall for this namespace in the same
            # transaction as the projection itself.
            self.bump_epoch()
        return self.db_path

    def _upsert_episode_row(
        self,
        conn: sqlite3.Connection,
        episode: EpisodicMemory,
        payload: str,
        created: str,
    ) -> int:
        """Write the episode row and return its rowid, which keys the FTS row.

        ``RETURNING rowid`` is what makes index maintenance cheap: the implicit
        rowid is stable across the ``DO UPDATE`` branch, so the FTS entry can be
        replaced by rowid instead of searched for.
        """
        row = conn.execute(
            """
            INSERT INTO episodes (
              namespace, id, session_id, project_id, run_id, trace_id,
              scenario_type, scenario, symptom,
              target_process, device, build, summary, root_causes, outcome,
              confidence, eval_run_id, graded_pass, source_run_dir, created_at,
              payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?)
            ON CONFLICT(namespace, id) DO UPDATE SET
              session_id = excluded.session_id,
              project_id = excluded.project_id,
              run_id = excluded.run_id,
              trace_id = excluded.trace_id,
              scenario_type = excluded.scenario_type,
              scenario = excluded.scenario,
              symptom = excluded.symptom,
              target_process = excluded.target_process,
              device = excluded.device,
              build = excluded.build,
              summary = excluded.summary,
              root_causes = excluded.root_causes,
              outcome = excluded.outcome,
              confidence = excluded.confidence,
              eval_run_id = excluded.eval_run_id,
              graded_pass = excluded.graded_pass,
              source_run_dir = excluded.source_run_dir,
              payload = excluded.payload
            RETURNING rowid
            """,
            (
                self.namespace,
                episode.episode_id,
                episode.session_id,
                episode.project_id,
                episode.run_id,
                episode.trace_id,
                episode.scenario_type,
                episode.scenario,
                episode.symptom,
                episode.target_process,
                episode.device,
                episode.build,
                episode.summary,
                _dumps(episode.root_causes),
                episode.status,
                episode_confidence(episode),
                episode.eval_run_id,
                None if episode.graded_pass is None else int(episode.graded_pass),
                episode.output_dir,
                created,
                payload,
            ),
        ).fetchone()
        return int(row["rowid"])

    def _index_episode(
        self,
        conn: sqlite3.Connection,
        rowid: int,
        episode: EpisodicMemory,
    ) -> None:
        conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO episodes_fts (rowid, namespace, episode_id, body) "
            "VALUES (?, ?, ?, ?)",
            (
                rowid,
                self.namespace,
                episode.episode_id,
                fts_body(episode_index_text(episode)),
            ),
        )

    def load_episode(self, episode_id: str) -> EpisodicMemory | None:
        row = self._conn().execute(
            "SELECT payload FROM episodes WHERE namespace = ? AND id = ?",
            (self.namespace, episode_id),
        ).fetchone()
        if row is None:
            return None
        return _validate(EpisodicMemory, row["payload"])

    def list_episodes(self) -> list[EpisodicMemory]:
        """All episodes in this namespace, newest first.

        The final sort happens in Python even though SQL orders the rows, so the
        result matches ``JsonMemoryBackend.list_episodes`` exactly - including
        for legacy records whose timestamps predate timezone awareness.
        """
        rows = self._conn().execute(
            "SELECT payload FROM episodes WHERE namespace = ? "
            "ORDER BY created_at DESC",
            (self.namespace,),
        ).fetchall()
        episodes = [
            item
            for item in (_validate(EpisodicMemory, row["payload"]) for row in rows)
            if item is not None
        ]
        return sorted(episodes, key=lambda item: item.created_at, reverse=True)

    def mark_episode_graded(
        self,
        episode_id: str,
        *,
        graded_pass: bool,
        graded_score: float | None = None,
        eval_run_id: str | None = None,
    ) -> bool:
        """Stamp a grade onto one episode through the ordinary write path.

        Going through :meth:`write_episode` rather than an ``UPDATE`` keeps the
        payload, the dedicated ``graded_pass`` column and the FTS row in one
        transaction, so a graded episode cannot end up with a stale index entry.
        ``write_episode`` is idempotent on ``(namespace, run_id)``, which makes
        re-grading a trial after a threshold change safe.
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

    # --------------------------------------------------------------- curated
    def write_curated(self, memory: CuratedMemory) -> Path:
        """Persist a curated memory, exactly as the file backend would.

        ``updated_at`` is refreshed here rather than by the caller for the same
        reason it is refreshed in ``JsonMemoryBackend.write_curated``: freshness
        is a ranking signal, and a store that did not bump it would silently
        make every memory look older than it is.
        """
        self._put_curated(memory, reason="consolidate")
        return self.db_path

    def _put_curated(
        self,
        memory: CuratedMemory,
        *,
        reason: str,
        actor: str | None = None,
        stamp: datetime | None = None,
    ) -> None:
        """Write one memory row plus its index, sources and version history.

        Columns split into two ownership classes. Caller-owned columns are
        rewritten from the model on every write. Store-owned columns -
        ``contradictions``, ``version``, ``last_accessed_at``, ``supersedes_id``,
        ``created_at`` - are governed by this class and are
        deliberately excluded from the ``DO UPDATE`` set, so a routine
        consolidation cannot wipe out decay bookkeeping or an access timestamp.

        ``expires_at`` is the exception that proves the rule. It *is* lifecycle
        bookkeeping, but it lives on the record so the JSON adapter can honour
        the same retention decision, which makes it caller-owned here. Nothing
        writes it except :class:`~trace_agent.memory.lifecycle.retention.RetentionService`,
        and every other writer copies it through unchanged.
        """
        if stamp is not None:
            memory = memory.model_copy(update={"updated_at": stamp})
        else:
            memory = memory.model_copy(update={"updated_at": utc_now()})
        payload = _dumps(memory.model_dump(mode="json"))
        episode_ids = sorted(memory.source_episode_ids)
        # Owned by the record, not by the adapter: three stores that each spelled
        # out their own scope dict are three stores that eventually disagree about
        # what a project-scoped query matches.
        scope = _dumps(memory.scope)

        with self._tx() as conn:
            previous = conn.execute(
                "SELECT rowid, version, topic, content, trust, confidence, "
                "distinct_runs, kind, keywords, scope, provenance "
                "FROM memories WHERE namespace = ? AND id = ?",
                (self.namespace, memory.memory_id),
            ).fetchone()
            current_state = state_key(
                memory.topic, memory.content, memory.status.value, memory.confidence
            )
            tail = None
            reimported = False
            # Version numbers continue from the audit trail rather than from the
            # row, and on *both* paths. ``remove_curated`` deliberately keeps
            # ``memory_versions``, so a memory re-created under the same id must
            # not restart at 1 and collide with the history it left behind -
            # which would raise inside a consolidation and cost the run its
            # memory write. The trail can also run ahead of a row that still
            # exists: ``import_governance`` derives an ordinal for an ordinal-less
            # sidecar row from ``MAX(version)`` and leaves the record alone, so a
            # migration replayed onto a populated target puts ordinals there the
            # row's own counter has not reached. Either way the next one has to
            # clear both, or the insert collides with
            # ``UNIQUE(namespace, memory_id, version)``.
            highest = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM memory_versions "
                "WHERE namespace = ? AND memory_id = ?",
                (self.namespace, memory.memory_id),
            ).fetchone()
            last_version = int(highest["version"])
            if previous is None:
                version = last_version + 1
            else:
                version = max(int(previous["version"]), last_version) + 1
            if previous is None and last_version:
                # ``memory migrate`` loads the audit trail before the records,
                # so an insert can arrive with history that already ends at
                # exactly this state. Recognising that is what keeps a
                # re-run migration from appending a duplicate tail.
                tail = conn.execute(
                    "SELECT topic, content, trust, confidence "
                    "FROM memory_versions "
                    "WHERE namespace = ? AND memory_id = ? AND version = ?",
                    (self.namespace, memory.memory_id, last_version),
                ).fetchone()
                if tail is not None and state_key(
                    tail["topic"],
                    tail["content"],
                    tail["trust"],
                    tail["confidence"],
                ) == current_state:
                    reimported = True
                    version = last_version
            if reimported:
                changed = False
            elif tail is not None:
                # Deleted and re-created under the same id with different
                # content: a real transition, and the history says so.
                changed = True
            else:
                changed = previous is None or (
                    previous["topic"] != memory.topic
                    or previous["content"] != memory.content
                    or previous["trust"] != memory.status.value
                    or float(previous["confidence"]) != float(memory.confidence)
                    or int(previous["distinct_runs"]) != memory.distinct_runs
                    # An expert signature changes what a record *claims* without
                    # touching its text, and section 7 requires that to be
                    # auditable. Without this comparison the JSON adapter - which
                    # records every ``revise`` - and this one would disagree about
                    # whether a sign-off happened, and "who vouched for this
                    # memory" would be unanswerable from the SQLite trail.
                    or _stored_validator(previous["provenance"])
                    != ((memory.provenance.validated_by or "").strip() or None)
                )
            if not changed and previous is not None:
                # Re-consolidating identical evidence is a no-op. Recording a
                # version per write would bury real transitions in noise.
                version = int(previous["version"])
            # The other half of the transition. ``trust`` alone says where the
            # record ended up; reconstructing where it came from by reading the
            # previous row is exactly what breaks on a migrated corpus, so the
            # pair is stored together. ``None`` on an insert means "this row is
            # the record's first version", not "unknown".
            from_trust = None if previous is None else previous["trust"]

            rowid = self._upsert_memory_row(
                conn, memory, payload=payload, scope=scope, version=version
            )
            self._index_memory(conn, rowid, memory)
            self._sync_sources(conn, memory, episode_ids)
            # The embedding is derived from the indexed text, so editing that
            # text retires the vector. Trust, confidence and corroboration
            # deliberately do *not*: they reorder recall without changing what
            # the record says, and re-embedding on a promotion would burn
            # provider calls for a vector that would come out identical.
            if self._indexed_text_changed(
                previous,
                {
                    "topic": memory.topic,
                    "content": memory.content,
                    "kind": memory.kind.value,
                    "keywords": _dumps(memory.keywords),
                    "scope": scope,
                },
            ):
                self._clear_embedding(conn, "memories", memory.memory_id)
            if changed:
                conn.execute(
                    "INSERT INTO memory_versions (namespace, memory_id, version, "
                    "topic, content, from_trust, trust, confidence, reason, "
                    "actor, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.namespace,
                        memory.memory_id,
                        version,
                        memory.topic,
                        memory.content,
                        from_trust,
                        memory.status.value,
                        memory.confidence,
                        reason,
                        actor,
                        _iso(memory.updated_at) or "",
                    ),
                )
            # Bumped even when ``changed`` is false: the write still refreshed
            # ``updated_at``, and freshness is a ranking input, so a result
            # cached before it would now come back in a different order.
            self.bump_epoch()

    def _upsert_memory_row(
        self,
        conn: sqlite3.Connection,
        memory: CuratedMemory,
        *,
        payload: str,
        scope: str,
        version: int,
    ) -> int:
        episode_ids = sorted(memory.source_episode_ids)
        row = conn.execute(
            """
            INSERT INTO memories (
              namespace, id, type, kind, topic, content, keywords, scope, trust,
              status_reason, confidence, importance, severity, occurrences,
              distinct_runs, version, source_episode_id, superseded_by,
              archived_reason, provenance, payload, created_at, updated_at,
              expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?)
            ON CONFLICT(namespace, id) DO UPDATE SET
              type = excluded.type,
              kind = excluded.kind,
              topic = excluded.topic,
              content = excluded.content,
              keywords = excluded.keywords,
              scope = excluded.scope,
              trust = excluded.trust,
              status_reason = excluded.status_reason,
              confidence = excluded.confidence,
              importance = excluded.importance,
              severity = excluded.severity,
              occurrences = excluded.occurrences,
              distinct_runs = excluded.distinct_runs,
              version = excluded.version,
              source_episode_id = excluded.source_episode_id,
              superseded_by = excluded.superseded_by,
              archived_reason = excluded.archived_reason,
              provenance = excluded.provenance,
              payload = excluded.payload,
              updated_at = excluded.updated_at,
              expires_at = excluded.expires_at
            RETURNING rowid
            """,
            (
                self.namespace,
                memory.memory_id,
                memory_type_for(memory.kind).value,
                memory.kind.value,
                memory.topic,
                memory.content,
                _dumps(memory.keywords),
                scope,
                memory.status.value,
                memory.status_reason,
                memory.confidence,
                importance_for(memory),
                memory.severity.value if memory.severity is not None else None,
                memory.occurrences,
                memory.distinct_runs,
                version,
                episode_ids[0] if episode_ids else None,
                memory.superseded_by,
                memory.archived_reason,
                _dumps(memory.provenance.model_dump(mode="json")),
                payload,
                _iso(memory.created_at) or "",
                _iso(memory.updated_at) or "",
                _iso(memory.expires_at),
            ),
        ).fetchone()
        return int(row["rowid"])

    def _index_memory(
        self,
        conn: sqlite3.Connection,
        rowid: int,
        memory: CuratedMemory,
    ) -> None:
        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (rowid,))
        conn.execute(
            "INSERT INTO memories_fts (rowid, namespace, memory_id, body) "
            "VALUES (?, ?, ?, ?)",
            (
                rowid,
                self.namespace,
                memory.memory_id,
                fts_body(curated_index_text(memory)),
            ),
        )

    def _sync_sources(
        self,
        conn: sqlite3.Connection,
        memory: CuratedMemory,
        episode_ids: list[str],
    ) -> None:
        """Make ``memory_sources`` agree with provenance, then re-derive the count.

        ``INSERT OR IGNORE`` against ``PRIMARY KEY (namespace, memory_id,
        run_id)`` is the mechanical half of the section-1.2 defect-A fix: the
        same run consolidated twice inserts one row, so ``distinct_runs`` cannot
        be inflated by repetition. The column is then overwritten from a COUNT
        rather than incremented, which means it can never drift from the
        evidence table it summarises.
        """
        now = _iso(utc_now()) or ""
        episode_id = episode_ids[0] if episode_ids else ""
        for run_id in sorted(memory.source_run_ids):
            conn.execute(
                "INSERT OR IGNORE INTO memory_sources "
                "(namespace, memory_id, run_id, episode_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.namespace, memory.memory_id, run_id, episode_id, now),
            )
        conn.execute(
            "UPDATE memories SET distinct_runs = ("
            "  SELECT COUNT(*) FROM memory_sources "
            "  WHERE namespace = ? AND memory_id = ?"
            ") WHERE namespace = ? AND id = ?",
            (
                self.namespace,
                memory.memory_id,
                self.namespace,
                memory.memory_id,
            ),
        )

    def load_curated(self, memory_id: str) -> CuratedMemory | None:
        row = self._conn().execute(
            "SELECT payload, version, last_accessed_at FROM memories "
            "WHERE namespace = ? AND id = ?",
            (self.namespace, memory_id),
        ).fetchone()
        if row is None:
            return None
        return self._governed(row)

    def list_curated(self, *, active_only: bool = False) -> list[CuratedMemory]:
        sql = (
            "SELECT payload, version, last_accessed_at FROM memories "
            "WHERE namespace = ?"
        )
        params: list[Any] = [self.namespace]
        if active_only:
            marks = ", ".join("?" for _ in _RETIRED_TRUSTS)
            sql += f" AND trust NOT IN ({marks})"
            params.extend(_RETIRED_TRUSTS)
        sql += " ORDER BY updated_at DESC, id"
        rows = self._conn().execute(sql, tuple(params)).fetchall()
        memories = [
            item
            for item in (self._governed(row) for row in rows)
            if item is not None
        ]
        return sorted(memories, key=lambda item: item.updated_at, reverse=True)

    @staticmethod
    def _governed(row: Any) -> CuratedMemory | None:
        """Rebuild a record and stamp the two store-owned governance fields.

        ``version`` and ``last_accessed_at`` are columns rather than payload keys
        (see :class:`~trace_agent.memory.models.CuratedMemory`), so a reader that
        returned the payload alone would hand back a record disagreeing with the
        audit trail sitting next to it. Both adapters do this identically, which
        is what keeps "equal writes give equal reads" true for governance too.
        """
        memory = _validate(CuratedMemory, row["payload"])
        if memory is None:
            return None
        return memory.model_copy(
            update={
                "version": optional_int(row["version"]),
                "last_accessed_at": parse_stamp(row["last_accessed_at"]),
            }
        )

    def remove_curated(self, memory_id: str) -> bool:
        """Delete a memory and its evidence rows, keeping its version history.

        ``memory_versions`` and ``memory_links`` survive on purpose: section 7
        requires every trust transition to stay auditable, and deleting the
        history of a retired memory is how a self-contamination incident becomes
        unexplainable after the fact.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT rowid FROM memories WHERE namespace = ? AND id = ?",
                (self.namespace, memory_id),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "DELETE FROM memories_fts WHERE rowid = ?", (row["rowid"],)
                )
            conn.execute(
                "DELETE FROM memories WHERE namespace = ? AND id = ?",
                (self.namespace, memory_id),
            )
            conn.execute(
                "DELETE FROM memory_sources WHERE namespace = ? AND memory_id = ?",
                (self.namespace, memory_id),
            )
            self.bump_epoch()
        return True

    # -------------------------------------------------------------- sessions
    def write_session(self, session: SessionMemory) -> Path:
        """Persist a session, with messages normalised into their own table.

        Messages live in ``session_messages`` and *not* in ``payload``; the
        session dump is stored with the key removed. Keeping them in one place
        only would be simpler but would give two answers to "what did this
        session say" as soon as P2 summarisation starts appending turns.

        ``updated_at`` is refreshed here, as the file backend does, because it
        is the ordering key session listings are sorted by.
        """
        return self._put_session(
            session.model_copy(update={"updated_at": utc_now()})
        )

    def _put_session(self, session: SessionMemory) -> Path:
        """Shared session writer.

        The only thing separating :meth:`write_session` from
        :meth:`import_session` is who decided ``updated_at``, so the row and
        message writes live here once.
        """
        data = session.model_dump(mode="json")
        messages = data.pop("messages", [])
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                  namespace, id, title, topic, summary, agent_id, user_id,
                  project_id, scenario_type, device, build, status, started_at,
                  ended_at, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, id) DO UPDATE SET
                  title = excluded.title,
                  topic = excluded.topic,
                  summary = excluded.summary,
                  agent_id = excluded.agent_id,
                  user_id = excluded.user_id,
                  project_id = excluded.project_id,
                  scenario_type = excluded.scenario_type,
                  device = excluded.device,
                  build = excluded.build,
                  status = excluded.status,
                  updated_at = excluded.updated_at,
                  payload = excluded.payload
                """,
                (
                    self.namespace,
                    session.session_id,
                    session.title,
                    session.topic,
                    session.summary,
                    session.agent_id,
                    session.user_id,
                    session.project_id,
                    session.scenario_type,
                    session.device,
                    session.build,
                    session.status,
                    _iso(session.created_at) or "",
                    None,
                    _iso(session.updated_at) or "",
                    _dumps(data),
                ),
            )
            conn.execute(
                "DELETE FROM session_messages WHERE namespace = ? AND session_id = ?",
                (self.namespace, session.session_id),
            )
            for message in messages:
                conn.execute(
                    'INSERT INTO session_messages (namespace, session_id, role, '
                    'content, "references", created_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (
                        self.namespace,
                        session.session_id,
                        str(message.get("role") or "user"),
                        str(message.get("content") or ""),
                        _dumps(message.get("references") or []),
                        str(message.get("created_at") or ""),
                    ),
                )
            self.bump_epoch()
        return self.db_path

    def load_session(self, session_id: str) -> SessionMemory | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT payload FROM sessions WHERE namespace = ? AND id = ?",
            (self.namespace, session_id),
        ).fetchone()
        if row is None:
            return None
        return self._assemble_session(row["payload"], session_id)

    def list_sessions(self) -> list[SessionMemory]:
        rows = self._conn().execute(
            "SELECT id, payload FROM sessions WHERE namespace = ? "
            "ORDER BY updated_at DESC, id",
            (self.namespace,),
        ).fetchall()
        sessions = [
            item
            for item in (
                self._assemble_session(row["payload"], row["id"]) for row in rows
            )
            if item is not None
        ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def _assemble_session(self, payload: str, session_id: str) -> SessionMemory | None:
        """Rejoin a session row with its messages."""
        try:
            data = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        rows = self._conn().execute(
            'SELECT role, content, "references", created_at FROM session_messages '
            "WHERE namespace = ? AND session_id = ? ORDER BY seq",
            (self.namespace, session_id),
        ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                references = json.loads(row["references"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                references = []
            messages.append(
                {
                    "role": row["role"],
                    "content": row["content"],
                    "references": references,
                    "created_at": row["created_at"],
                }
            )
        data["messages"] = messages
        try:
            return SessionMemory.model_validate(data)
        except ValueError:
            return None

    # ------------------------------------------------- file-backed sidecars
    @property
    def dreams_path(self) -> Path:
        return self._files.dreams_path

    def append_dream_job(self, payload: dict[str, object]) -> None:
        self._files.append_dream_job(payload)

    def read_dream_jobs(self, *, limit: int = 100) -> list[dict[str, object]]:
        return self._files.read_dream_jobs(limit=limit)

    def clear_dream_jobs(self) -> None:
        self._files.clear_dream_jobs()

    @staticmethod
    def execution_snapshot(output_dir: str | Path) -> ExecutionCheckpointSnapshot:
        """Project a run directory; it is not corpus state, so nothing is stored."""
        return JsonMemoryBackend.execution_snapshot(output_dir)

    # ------------------------------------------------------ indexed retrieval
    def search(self, query: MemoryQuery) -> list[ScoredMemory]:
        """Candidate pool from FTS5, already filtered by trust, scope and type.

        This is the member the JSON adapter does not implement, and the reason
        recall stops being an O(N) walk. Filters are pushed into SQL so the
        reranker only ever sees rows that could legally be returned.

        An empty pool for a tokenless query is correct rather than lossy: the
        reranker scores such a query at ``0.0`` lexical relevance, which is below
        the relevance floor, so a full scan would return nothing either. The
        caller additionally only takes this path when the floor sits above
        ``PHRASE_WEIGHT``, which guarantees every surviving hit shares a real
        token with the query - the property that makes the OR'd MATCH a superset
        of what the scan would have kept.
        """
        match = fts_match(query.text)
        if match is None:
            return []
        namespace = query.namespace or self.namespace
        conn = self._conn()
        pool: list[ScoredMemory] = []
        pool.extend(self._search_memories(conn, match, query, namespace))
        if query.include_episodic:
            pool.extend(self._search_episodes(conn, match, query, namespace))
        # bm25 is negative and more negative is better, so ascending is best
        # first. Truncating to ``limit`` here is what bounds the reranker's work.
        pool.sort(key=lambda item: item.bm25 if item.bm25 is not None else 0.0)
        return pool[: max(1, query.limit)]

    def _search_memories(
        self,
        conn: sqlite3.Connection,
        match: str,
        query: MemoryQuery,
        namespace: str,
    ) -> list[ScoredMemory]:
        statuses = sorted(status.value for status in query.statuses)
        if not statuses:
            return []
        clauses = ["memories_fts MATCH ?", "f.namespace = ?"]
        params: list[Any] = [match, namespace]
        clauses.append(f"m.trust IN ({_marks(statuses)})")
        params.extend(statuses)
        if query.types:
            types = sorted(item.value for item in query.types)
            clauses.append(f"m.type IN ({_marks(types)})")
            params.extend(types)
        # A memory with no scenario_type is scenario-agnostic and may match any
        # query, so a NULL scope must survive the filter. Dropping it here would
        # be a silent recall regression against the scan path, which skips the
        # check for exactly those records.
        clauses.append(
            "(? IS NULL OR json_extract(m.scope, '$.scenario_type') IS NULL "
            "OR json_extract(m.scope, '$.scenario_type') = ?)"
        )
        params.extend([query.scenario_type, query.scenario_type])
        params.append(max(1, query.limit))
        sql = (
            "SELECT m.payload AS payload, f.rank AS rank "
            "FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY f.rank LIMIT ?"
        )
        return _scored_rows(conn.execute(sql, tuple(params)).fetchall(), CuratedMemory)

    def _search_episodes(
        self,
        conn: sqlite3.Connection,
        match: str,
        query: MemoryQuery,
        namespace: str,
    ) -> list[ScoredMemory]:
        clauses = ["episodes_fts MATCH ?", "f.namespace = ?"]
        params: list[Any] = [match, namespace]
        # Episodes always carry a scenario_type, so unlike memories the filter
        # is strict - matching the scan path's ``episode.scenario_type != x``.
        clauses.append("(? IS NULL OR e.scenario_type = ?)")
        params.extend([query.scenario_type, query.scenario_type])
        params.append(max(1, query.limit))
        sql = (
            "SELECT e.payload AS payload, f.rank AS rank "
            "FROM episodes_fts f JOIN episodes e ON e.rowid = f.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY f.rank LIMIT ?"
        )
        return _scored_rows(conn.execute(sql, tuple(params)).fetchall(), EpisodicMemory)

    # ------------------------------------------------------------ governance
    def add_source(
        self,
        memory_id: str,
        *,
        run_id: str,
        episode_id: str = "",
    ) -> bool:
        """Record a run as evidence for a memory. ``True`` only if it is new.

        The return value is the signal the quality gate should trust: a second
        consolidation of the *same* run returns ``False``, which is precisely the
        distinction section 1.2 defect A collapsed.
        """
        if not run_id:
            return False
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO memory_sources "
                "(namespace, memory_id, run_id, episode_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self.namespace,
                    memory_id,
                    run_id,
                    episode_id,
                    _iso(utc_now()) or "",
                ),
            )
            inserted = cursor.rowcount > 0
            self._refresh_distinct_runs(conn, memory_id)
        return inserted

    def _refresh_distinct_runs(
        self, conn: sqlite3.Connection, memory_id: str
    ) -> None:
        conn.execute(
            "UPDATE memories SET distinct_runs = ("
            "  SELECT COUNT(*) FROM memory_sources "
            "  WHERE namespace = ? AND memory_id = ?"
            ") WHERE namespace = ? AND id = ?",
            (self.namespace, memory_id, self.namespace, memory_id),
        )

    def distinct_runs(self, memory_id: str) -> int:
        """Independent runs behind a memory, read from the evidence table."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS total FROM memory_sources "
            "WHERE namespace = ? AND memory_id = ?",
            (self.namespace, memory_id),
        ).fetchone()
        return int(row["total"]) if row is not None else 0

    def set_trust(
        self,
        memory_id: str,
        trust: MemoryTrust,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> bool:
        """Change one memory's trust level, leaving an auditable version behind.

        Section 7 makes this the *only* legitimate way to move between trust
        levels, and requires the transition to be recorded. Routing through
        :meth:`_put_curated` gives that for free: the payload is rewritten so
        reads see the new status, and a ``memory_versions`` row captures the
        previous content with the reason and actor attached.
        """
        memory = self.load_curated(memory_id)
        if memory is None:
            return False
        updated = memory.model_copy(
            update={
                "status": trust,
                "status_reason": reason or None,
                "occurrences": memory.distinct_runs,
            }
        )
        self._put_curated(updated, reason=reason or "trust", actor=actor)
        return True

    def revise(
        self,
        memory: CuratedMemory,
        *,
        reason: str,
        actor: str | None = None,
    ) -> bool:
        """Write a governance edit with its own audit reason.

        Identical to :meth:`write_curated` except for the version row's
        ``reason``/``actor``. Halving a memory's confidence because another run
        contradicted it is a substantive change, and filing it as
        ``"consolidate"`` would make the audit trail claim the corpus agreed
        with itself at the exact moment it stopped doing so.
        """
        self._put_curated(memory, reason=reason or "revise", actor=actor)
        return True

    def link(
        self,
        left: str,
        right: str,
        relation: str,
        *,
        evidence: str | None = None,
    ) -> None:
        """Assert a typed relation between two memories (supports/contradicts/...).

        ``memory_links`` is the source of truth; ``memories.contradictions`` is
        a denormalised counter refreshed from it in the same transaction, so a
        SQL-side filter or the dashboard can read the number without a join and
        the two can never disagree.
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO memory_links "
                "(namespace, memory_id, related_memory_id, relation, evidence, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, memory_id, related_memory_id, relation) "
                "DO UPDATE SET evidence = excluded.evidence",
                (
                    self.namespace,
                    left,
                    right,
                    relation,
                    evidence,
                    _iso(utc_now()) or "",
                ),
            )
            if relation == CONTRADICTS_RELATION:
                self._refresh_contradictions(conn, left)
            # A link changes what the quality gate can conclude about a memory,
            # which is enough to change a trust-filtered recall.
            self.bump_epoch()

    def _refresh_contradictions(
        self, conn: sqlite3.Connection, memory_id: str
    ) -> None:
        conn.execute(
            "UPDATE memories SET contradictions = ("
            "  SELECT COUNT(*) FROM memory_links WHERE namespace = ? "
            "  AND memory_id = ? AND relation = ?"
            ") WHERE namespace = ? AND id = ?",
            (
                self.namespace,
                memory_id,
                CONTRADICTS_RELATION,
                self.namespace,
                memory_id,
            ),
        )

    def count_links(self, memory_id: str, relation: str | None = None) -> int:
        """How many edges of one kind point out of a memory.

        The gate calls this with ``contradicts`` to decide whether a memory has
        been refuted enough to archive. Counting links rather than trusting a
        counter column means the verdict is reproducible from the audit trail
        alone.
        """
        sql = (
            "SELECT COUNT(*) FROM memory_links "
            "WHERE namespace = ? AND memory_id = ?"
        )
        params: list[Any] = [self.namespace, memory_id]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation)
        row = self._conn().execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row is not None else 0

    def linked_ids(
        self, memory_id: str, relation: str | None = None
    ) -> list[str]:
        """Neighbours reachable from one memory over one relation kind.

        ``count_links`` answers "how many refutations"; this answers "which
        records corroborate me", which the gate needs in order to read the
        *other* side's run ids. Counting alone cannot do that: two memories may
        share a run, and only the ids let the caller exclude evidence that is
        not actually independent.
        """
        sql = (
            "SELECT related_memory_id FROM memory_links "
            "WHERE namespace = ? AND memory_id = ?"
        )
        params: list[Any] = [self.namespace, memory_id]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation)
        sql += " ORDER BY related_memory_id"
        return [str(row[0]) for row in self._conn().execute(sql, tuple(params))]

    def list_links(
        self, relation: str | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Edges in this namespace, newest first, for the dashboard and audits."""
        sql = "SELECT memory_id, related_memory_id, relation, evidence, created_at "
        sql += "FROM memory_links WHERE namespace = ?"
        params: list[Any] = [self.namespace]
        if relation is not None:
            sql += " AND relation = ?"
            params.append(relation)
        sql += " ORDER BY created_at DESC, memory_id LIMIT ?"
        params.append(max(1, int(limit)))
        return [dict(row) for row in self._conn().execute(sql, tuple(params))]

    def recent_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Latest trust/content transitions, newest first.

        Section 7 requires every transition to be recorded; this is how that
        requirement becomes checkable instead of assumed. The acceptance test
        for P2 asserts that each row carries a non-empty ``reason``.

        The key set matches the file adapter's ``versions.jsonl`` rows
        (``topic``/``content``/``from_trust`` included), because this is what
        ``memory migrate`` exports: two adapters returning different audit
        shapes would make a migration silently lossy in one direction.
        """
        rows = self._conn().execute(
            "SELECT memory_id, version, topic, content, from_trust, trust, "
            "confidence, reason, actor, "
            "created_at FROM memory_versions WHERE namespace = ? "
            "ORDER BY seq DESC LIMIT ?",
            (self.namespace, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def import_governance(
        self,
        versions: list[dict[str, Any]],
        links: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Load a migration bundle's audit trail and edges; returns what was added.

        Deliberately *not* on :class:`~trace_agent.memory.ports.MemoryStore`:
        it is a migration-only entry point, and putting it on the Port would
        oblige every future adapter to implement a method nothing else calls.
        ``migrate`` probes for it with ``getattr`` and reports zero when absent.

        Both inserts ignore conflicts, so re-running a migration adds nothing -
        the idempotence section 12.2 asks for. Counts are a before/after
        ``COUNT(*)`` rather than rowcounts: ``INSERT OR IGNORE`` is not required
        to report an ignored row the same way on every driver, and a migration
        report that guesses is worse than one that measures.

        ``version`` may be missing on rows exported by the JSON adapter, whose
        sidecar records the transition but not its ordinal. Deriving it from the
        memory's own maximum keeps ``UNIQUE(namespace, memory_id, version)``
        satisfiable instead of dropping the row on the floor - but only
        :class:`~trace_agent.memory.store.rows.VersionResolver` makes that
        derivation *stable*, which is what turns "conflicts are ignored" into
        actual idempotence for a bundle whose rows carry no ordinal.
        """
        with self._tx() as conn:
            def count(sql: str) -> int:
                row = conn.execute(sql, (self.namespace,)).fetchone()
                return int(row[0]) if row else 0

            versions_sql = "SELECT COUNT(*) FROM memory_versions WHERE namespace = ?"
            links_sql = "SELECT COUNT(*) FROM memory_links WHERE namespace = ?"
            before_versions = count(versions_sql)
            before_links = count(links_sql)
            now = _iso(utc_now()) or ""
            resolver = VersionResolver(
                (row[0], row[1], row[2], row[3], row[4])
                for row in conn.execute(
                    "SELECT memory_id, version, trust, reason, created_at "
                    "FROM memory_versions WHERE namespace = ?",
                    (self.namespace,),
                )
            )
            for record in versions:
                memory_id = str(record.get("memory_id") or "")
                if not memory_id:
                    continue
                trust = str(record.get("trust") or MemoryStatus.CANDIDATE.value)
                reason = str(record.get("reason") or "")
                created_at = str(record.get("created_at") or now)
                version = resolver.ordinal(
                    memory_id=memory_id,
                    version=record.get("version"),
                    trust=trust,
                    reason=reason,
                    created_at=created_at,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO memory_versions (namespace, memory_id, "
                    "version, topic, content, from_trust, trust, confidence, "
                    "reason, actor, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.namespace,
                        memory_id,
                        version,
                        str(record.get("topic") or ""),
                        str(record.get("content") or ""),
                        record.get("from_trust"),
                        trust,
                        as_float(record.get("confidence")),
                        reason,
                        record.get("actor"),
                        created_at,
                    ),
                )
            for record in links:
                memory_id = str(record.get("memory_id") or "")
                related = str(record.get("related_memory_id") or "")
                relation = str(record.get("relation") or "")
                if not (memory_id and related and relation):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO memory_links (namespace, memory_id, "
                    "related_memory_id, relation, evidence, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.namespace,
                        memory_id,
                        related,
                        relation,
                        record.get("evidence"),
                        str(record.get("created_at") or now),
                    ),
                )
            added_versions = count(versions_sql) - before_versions
            added_links = count(links_sql) - before_links
            if added_versions or added_links:
                # Governance feeds the gate's corroboration and contradiction
                # counts, so a trust-filtered recall can change without a single
                # record being touched.
                self.bump_epoch()
        return added_versions, added_links

    def touch(self, memory_id: str) -> None:
        """Stamp ``last_accessed_at`` without touching the stored record.

        Access time is store-owned metadata: it must not rewrite ``payload`` or
        ``updated_at``, because recalling a memory should not make it look newly
        corroborated and push it up the freshness ranking.
        """
        with self._tx() as conn:
            conn.execute(
                "UPDATE memories SET last_accessed_at = ? "
                "WHERE namespace = ? AND id = ?",
                (_iso(utc_now()), self.namespace, memory_id),
            )

    def expire(self, now: datetime | None = None) -> int:
        """Archive memories past ``expires_at``; returns how many were retired.

        Archiving rather than deleting keeps section 7's rule that retired
        memory stays available for audit, and it is the retention half of the
        lifecycle: a corpus that can only grow eventually drowns recall in
        priors nobody has believed in for a year.
        """
        reference = _iso(now or utc_now()) or ""
        rows = self._conn().execute(
            "SELECT payload FROM memories WHERE namespace = ? "
            "AND expires_at IS NOT NULL AND expires_at <= ? AND trust <> ?",
            (self.namespace, reference, MemoryStatus.ARCHIVED.value),
        ).fetchall()
        expired = 0
        for row in rows:
            memory = _validate(CuratedMemory, row["payload"])
            if memory is None:
                continue
            retired = memory.model_copy(
                update={
                    "status": MemoryStatus.ARCHIVED,
                    "archived_reason": "expired",
                    "status_reason": "retention: past expires_at",
                }
            )
            self._put_curated(retired, reason="expire")
            expired += 1
        return expired

    def stats(self) -> MemoryStats:
        """Corpus shape for ``memory stats``, the dashboard and migration checks."""
        conn = self._conn()
        namespace = self.namespace

        def count(sql: str, *extra: Any) -> int:
            row = conn.execute(sql, (namespace, *extra)).fetchone()
            return int(row[0]) if row is not None else 0

        def grouped(column: str) -> dict[str, int]:
            rows = conn.execute(
                f"SELECT {column} AS bucket, COUNT(*) AS total FROM memories "
                "WHERE namespace = ? GROUP BY bucket",
                (namespace,),
            ).fetchall()
            return {str(row["bucket"]): int(row["total"]) for row in rows}

        return MemoryStats(
            store="sqlite",
            namespace=namespace,
            memories=count("SELECT COUNT(*) FROM memories WHERE namespace = ?"),
            episodes=count("SELECT COUNT(*) FROM episodes WHERE namespace = ?"),
            sessions=count("SELECT COUNT(*) FROM sessions WHERE namespace = ?"),
            by_trust=grouped("trust"),
            by_type=grouped("type"),
            by_kind=grouped("kind"),
            contradictions=count(
                "SELECT COUNT(*) FROM memory_links "
                "WHERE namespace = ? AND relation = ?",
                CONTRADICTS_RELATION,
            ),
            duplicates=count(
                "SELECT COUNT(*) FROM memory_links "
                "WHERE namespace = ? AND relation = ?",
                DUPLICATE_RELATION,
            ),
            versions=count(
                "SELECT COUNT(*) FROM memory_versions WHERE namespace = ?"
            ),
            links=count("SELECT COUNT(*) FROM memory_links WHERE namespace = ?"),
            expired=count(
                "SELECT COUNT(*) FROM memories "
                "WHERE namespace = ? AND archived_reason = 'expired'"
            ),
            superseded=count(
                "SELECT COUNT(*) FROM memories WHERE namespace = ? AND trust = ?",
                MemoryStatus.SUPERSEDED.value,
            ),
        )

    def reindex(self) -> int:
        """Rebuild both FTS indexes from stored records; returns rows indexed.

        The body is recomputed from the loaded model through
        :mod:`trace_agent.memory.search_text`, not copied from the old index, so
        this is also the recovery path after the tokenizer or the indexed field
        set changes. Deletion is by rowid rather than by the UNINDEXED namespace
        column, which keeps it O(log n) and avoids depending on FTS5 scan
        semantics for a maintenance operation.
        """
        indexed = 0
        with self._tx() as conn:
            for table in ("memories_fts", "episodes_fts"):
                stale = conn.execute(
                    f"SELECT rowid FROM {table} WHERE namespace = ?",
                    (self.namespace,),
                ).fetchall()
                for row in stale:
                    conn.execute(
                        f"DELETE FROM {table} WHERE rowid = ?", (row["rowid"],)
                    )
            for row in conn.execute(
                "SELECT rowid, payload FROM memories WHERE namespace = ?",
                (self.namespace,),
            ).fetchall():
                memory = _validate(CuratedMemory, row["payload"])
                if memory is None:
                    continue
                conn.execute(
                    "INSERT INTO memories_fts (rowid, namespace, memory_id, body) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        row["rowid"],
                        self.namespace,
                        memory.memory_id,
                        fts_body(curated_index_text(memory)),
                    ),
                )
                indexed += 1
            for row in conn.execute(
                "SELECT rowid, payload FROM episodes WHERE namespace = ?",
                (self.namespace,),
            ).fetchall():
                episode = _validate(EpisodicMemory, row["payload"])
                if episode is None:
                    continue
                conn.execute(
                    "INSERT INTO episodes_fts (rowid, namespace, episode_id, body) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        row["rowid"],
                        self.namespace,
                        episode.episode_id,
                        fts_body(episode_index_text(episode)),
                    ),
                )
                indexed += 1
            for table in ("memories_fts", "episodes_fts"):
                conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
            # Rebuilding the index can change which rows a lexical query matches,
            # so cached results from before the rebuild are no longer trustworthy.
            self.bump_epoch()
        return indexed

    # ------------------------------------------------- semantic vectors (P3)
    # All six members are *optional* at the port level: a store that cannot hold
    # vectors simply omits them and recall stays lexical. This adapter has them,
    # and the cosine is computed in Python over rows SQL has already filtered by
    # namespace, trust, type and scenario. At this corpus size an exact scan of
    # the embedded subset beats an approximate index on both accuracy and
    # portability - no extension to build, no rebuild-on-write to get wrong -
    # and the callers do not change when a pgvector adapter replaces this
    # section. Every member swallows ``sqlite3.Error`` so a database whose
    # additive migration failed costs semantic recall rather than the run.

    def pending_embeddings(
        self, *, model: str, limit: int = 32
    ) -> list[PendingEmbedding]:
        """Records needing a vector: unembedded, or embedded by another recipe.

        ``embedding_model <> ?`` is what makes a model change a rebuild. The
        store cannot tell whether two recipes are comparable, and it must not
        guess: a stale vector silently mixed into a new corpus produces cosines
        that look plausible and mean nothing, which is worse than having no
        semantic channel at all.

        Curated and episodic rows are interleaved rather than concatenated. One
        table after the other would starve whichever came second on a corpus
        where the first never finishes, and the episodic channel is the one
        paraphrase queries need most.
        """
        budget = max(1, int(limit))
        conn = self._conn()
        memories = self._pending_rows(
            conn,
            "memories",
            model,
            budget,
            CuratedMemory,
            curated_index_text,
            order="updated_at DESC",
            extra=f"AND trust NOT IN ({_marks(_RETIRED_TRUSTS)})",
            extra_params=_RETIRED_TRUSTS,
        )
        episodes = self._pending_rows(
            conn,
            "episodes",
            model,
            budget,
            EpisodicMemory,
            episode_index_text,
            order="created_at DESC",
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

    def _pending_rows(
        self,
        conn: sqlite3.Connection,
        table: str,
        model: str,
        limit: int,
        model_class: type[Any],
        index_text: Any,
        *,
        order: str,
        extra: str = "",
        extra_params: Any = (),
    ) -> list[PendingEmbedding]:
        """One table's outstanding embedding work, newest first.

        Newest first because a fresh run is the record most likely to be
        recalled before the next beat, so it is also the one whose missing
        vector costs the most.
        """
        sql = (
            f"SELECT id, payload, embedding_model FROM {table} "
            "WHERE namespace = ? AND (embedding IS NULL "
            "OR embedding_model IS NULL OR embedding_model <> ?) "
            f"{extra}ORDER BY {order} LIMIT ?"
        )
        try:
            rows = conn.execute(
                sql, (self.namespace, model, *extra_params, limit)
            ).fetchall()
        except sqlite3.Error:
            return []
        out: list[PendingEmbedding] = []
        for row in rows:
            record = _validate(model_class, row["payload"])
            if record is None:
                # A row whose payload cannot be rebuilt has no text to embed.
                # Skipping it is consistent with every other reader here.
                continue
            text = str(index_text(record) or "").strip()
            if not text:
                continue
            out.append(
                PendingEmbedding(
                    record_id=str(row["id"]),
                    text=text,
                    episode=table == "episodes",
                    stored_model=(
                        str(row["embedding_model"]) if row["embedding_model"] else None
                    ),
                )
            )
        return out

    def set_embedding(
        self,
        record_id: str,
        vector: Any,
        *,
        model: str,
        episode: bool = False,
    ) -> bool:
        """Store one packed float32 vector; ``True`` only if a row was updated.

        Packing here rather than in the caller is what keeps the two adapters
        bit-compatible: the JSON sidecar stores ``round_trip`` numbers, this one
        stores the same values as a blob, and a cosine computed on either agrees.
        """
        values = [float(item) for item in (vector or ())]
        if not values or not record_id:
            return False
        table = "episodes" if episode else "memories"
        try:
            blob = pack_vector(values)
        except (OverflowError, TypeError, ValueError):
            return False
        with self._tx() as conn:
            try:
                cursor = conn.execute(
                    f"UPDATE {table} SET embedding = ?, embedding_model = ? "
                    "WHERE namespace = ? AND id = ?",
                    (blob, model, self.namespace, record_id),
                )
            except sqlite3.Error:
                return False
            return cursor.rowcount > 0

    def vectors_for(
        self, record_ids: Any, *, episode: bool = False
    ) -> dict[str, list[float]]:
        """Stored vectors for a set of ids; ids without one are absent."""
        ids = [str(item) for item in (record_ids or ()) if item]
        if not ids:
            return {}
        table = "episodes" if episode else "memories"
        try:
            rows = self._conn().execute(
                f"SELECT id, embedding FROM {table} "
                f"WHERE namespace = ? AND id IN ({_marks(ids)})",
                (self.namespace, *ids),
            ).fetchall()
        except sqlite3.Error:
            return {}
        out: dict[str, list[float]] = {}
        for row in rows:
            vector = unpack_vector(row["embedding"])
            if vector:
                out[str(row["id"])] = vector
        return out

    def vector_search(
        self, vector: Any, query: MemoryQuery
    ) -> list[ScoredMemory]:
        """Nearest records to ``vector``, under the query's own filters.

        The filters are the *same* predicates the lexical path uses, minus the
        FTS MATCH: a vector channel that ignored ``trust`` would hand recall
        candidates the reranker is not allowed to return, and one that ignored
        ``scenario_type`` would leak a cold-start pattern into a frame-drop
        query. Section 8's namespace rule is enforced by the WHERE clause, not by
        the caller.

        ``bm25`` is left ``None`` on these rows. A cosine and a bm25 rank are not
        comparable, and filling the field with one would let a caller sort a
        merged pool by a number that means two different things.
        """
        probe = [float(item) for item in (vector or ())]
        if not probe:
            return []
        namespace = query.namespace or self.namespace
        conn = self._conn()
        pool: list[ScoredMemory] = []
        pool.extend(
            self._vector_pool(
                self._memory_vector_rows(conn, query, namespace),
                CuratedMemory,
                probe,
            )
        )
        if query.include_episodic:
            pool.extend(
                self._vector_pool(
                    self._episode_vector_rows(conn, query, namespace),
                    EpisodicMemory,
                    probe,
                )
            )
        pool.sort(key=lambda item: (-(item.similarity or 0.0), _record_id(item.memory)))
        return pool[: max(1, query.limit)]

    def _memory_vector_rows(
        self, conn: sqlite3.Connection, query: MemoryQuery, namespace: str
    ) -> list[Any]:
        statuses = sorted(status.value for status in query.statuses)
        if not statuses:
            return []
        clauses = [
            "m.namespace = ?",
            "m.embedding IS NOT NULL",
            f"m.trust IN ({_marks(statuses)})",
        ]
        params: list[Any] = [namespace, *statuses]
        if query.types:
            types = sorted(item.value for item in query.types)
            clauses.append(f"m.type IN ({_marks(types)})")
            params.extend(types)
        # NULL scope means scenario-agnostic, exactly as in ``_search_memories``:
        # the two channels must admit the same records or a query's answer would
        # depend on which one happened to find it.
        clauses.append(
            "(? IS NULL OR json_extract(m.scope, '$.scenario_type') IS NULL "
            "OR json_extract(m.scope, '$.scenario_type') = ?)"
        )
        params.extend([query.scenario_type, query.scenario_type])
        sql = (
            "SELECT m.payload AS payload, m.embedding AS embedding "
            f"FROM memories m WHERE {' AND '.join(clauses)}"
        )
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error:
            return []

    def _episode_vector_rows(
        self, conn: sqlite3.Connection, query: MemoryQuery, namespace: str
    ) -> list[Any]:
        clauses = ["e.namespace = ?", "e.embedding IS NOT NULL"]
        params: list[Any] = [namespace]
        # Episodes always carry a scenario_type, so the filter is strict here
        # too - matching both ``_search_episodes`` and the scan path.
        clauses.append("(? IS NULL OR e.scenario_type = ?)")
        params.extend([query.scenario_type, query.scenario_type])
        sql = (
            "SELECT e.payload AS payload, e.embedding AS embedding "
            f"FROM episodes e WHERE {' AND '.join(clauses)}"
        )
        try:
            return conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error:
            return []

    @staticmethod
    def _vector_pool(
        rows: list[Any], model_class: type[Any], probe: list[float]
    ) -> list[ScoredMemory]:
        out: list[ScoredMemory] = []
        for row in rows:
            record = _validate(model_class, row["payload"])
            if record is None:
                continue
            stored = unpack_vector(row["embedding"])
            if not stored:
                # Truncated or wrong-width blob: treat as unembedded, which is
                # what ``unpack_vector`` documents and the next backfill repairs.
                continue
            out.append(
                ScoredMemory(
                    memory=record, bm25=None, similarity=cosine(probe, stored)
                )
            )
        return out

    def prune_embeddings(self, *, keep_model: str) -> int:
        """Delete vectors not produced by ``keep_model``; returns rows cleared.

        ``keep_model=""`` matches nothing and therefore clears everything, which
        is how :meth:`SemanticIndex.rebuild` turns a recipe change into a fresh
        corpus instead of a mixture.
        """
        kept = keep_model or ""
        pruned = 0
        with self._tx() as conn:
            for table in ("memories", "episodes"):
                try:
                    cursor = conn.execute(
                        f"UPDATE {table} SET embedding = NULL, "
                        "embedding_model = NULL "
                        "WHERE namespace = ? AND embedding IS NOT NULL "
                        "AND (embedding_model IS NULL OR embedding_model <> ?)",
                        (self.namespace, kept),
                    )
                except sqlite3.Error:
                    continue
                pruned += max(0, cursor.rowcount)
        return pruned

    def embedding_coverage(self) -> dict[str, Any]:
        """What is embedded, with which recipe, and what is still outstanding."""
        conn = self._conn()

        def count(sql: str, *extra: Any) -> int:
            try:
                row = conn.execute(sql, (self.namespace, *extra)).fetchone()
            except sqlite3.Error:
                return 0
            return int(row[0]) if row is not None else 0

        memories = count("SELECT COUNT(*) FROM memories WHERE namespace = ?")
        memories_embedded = count(
            "SELECT COUNT(*) FROM memories WHERE namespace = ? "
            "AND embedding IS NOT NULL"
        )
        episodes = count("SELECT COUNT(*) FROM episodes WHERE namespace = ?")
        episodes_embedded = count(
            "SELECT COUNT(*) FROM episodes WHERE namespace = ? "
            "AND embedding IS NOT NULL"
        )
        models: dict[str, int] = {}
        for table in ("memories", "episodes"):
            try:
                rows = conn.execute(
                    f"SELECT embedding_model AS model, COUNT(*) AS total "
                    f"FROM {table} WHERE namespace = ? AND embedding IS NOT NULL "
                    "GROUP BY model",
                    (self.namespace,),
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                key = str(row["model"] or "")
                models[key] = models.get(key, 0) + int(row["total"])
        return {
            "memories": memories,
            "memories_embedded": memories_embedded,
            "episodes": episodes,
            "episodes_embedded": episodes_embedded,
            "models": models,
            "unembedded": max(
                0,
                (memories - memories_embedded) + (episodes - episodes_embedded),
            ),
        }

    # ---------------------------------------------------- embedding hygiene
    @staticmethod
    def _indexed_text_changed(previous: Any, current: dict[str, Any]) -> bool:
        """Whether the columns the indexed text is built from changed.

        ``previous is None`` means an insert, where there is no stored vector to
        retire. Columns absent from the fetched row are skipped rather than
        treated as changed, so a future caller that selects less cannot silently
        invalidate the whole corpus on every write.
        """
        if previous is None:
            return False
        for column, value in current.items():
            try:
                stored = previous[column]
            except (IndexError, KeyError):
                continue
            if stored != value:
                return True
        return False

    def _clear_embedding(
        self, conn: sqlite3.Connection, table: str, record_id: str
    ) -> None:
        """Retire a vector whose source text no longer matches the record.

        Clearing rather than recomputing: a write happens inside a run, and
        calling a provider there would put embedding latency on the hot path for
        a vector the next dreaming beat produces anyway. In between, the record
        is simply absent from the vector channel, which degrades to lexical
        recall for that one row and nothing more.
        """
        try:
            conn.execute(
                f"UPDATE {table} SET embedding = NULL, embedding_model = NULL "
                "WHERE namespace = ? AND id = ?",
                (self.namespace, record_id),
            )
        except sqlite3.Error:
            return

    # ------------------------------------------------------- corpus epoch (P4)
    def corpus_epoch(self) -> int:
        """How many times this namespace's corpus has been written.

        One counter per namespace in ``store_meta``, read by the recall cache to
        build its key. It exists so invalidation is a property of the corpus
        rather than of every writer: nothing has to know which cached queries a
        consolidation affected, because a change in the epoch changes every key
        at once. Deliberately coarse - a cache miss is cheap, a stale prior in a
        prompt is not.

        Reads through :meth:`_conn`, so a missing database is created and
        answers ``0``, the same as an empty one. That conflation is safe for a
        local file, where "cannot be opened" and "does not exist yet" really are
        the same situation; the Postgres adapter has to be stricter because
        there a down server and an empty corpus are genuinely different states.
        """
        try:
            row = self._conn().execute(
                "SELECT value FROM store_meta WHERE key = ?",
                (_epoch_key(self.namespace),),
            ).fetchone()
        except sqlite3.Error:
            return 0
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError, IndexError, KeyError):
            return 0

    def bump_epoch(self) -> None:
        """Advance the epoch. Called by every write path, inside its transaction.

        Inside the transaction matters: a write that rolls back must not
        invalidate anything, and a write that commits must invalidate
        everything, in the same atomic step as the data it changed. It reads
        ``store_meta.value`` in the upsert's own expression rather than doing a
        read-modify-write in Python, so two writers cannot both compute the same
        successor.
        """
        try:
            self._conn().execute(
                "INSERT INTO store_meta (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST("
                "  CAST(store_meta.value AS INTEGER) + 1 AS TEXT)",
                (_epoch_key(self.namespace),),
            )
        except sqlite3.Error:
            # A failed counter write costs a stale cache entry for at most one
            # TTL, and the write it was attached to is the caller's to fail.
            return

    # ----------------------------------------------------- multi-tenancy (P4)
    def list_namespaces(self) -> list[dict[str, Any]]:
        """Every isolation domain in this database, with its record counts.

        Section 8's namespace rule is only enforceable if it is inspectable: an
        operator has to be able to see that ``eval:*`` domains exist, how big
        they got, and when they were last written, without querying the corpus
        they are supposed to be isolated from. Counts come from one pass per
        table over the namespace index rather than a join, because a namespace
        with episodes but no memories still has to appear.
        """
        sql = (
            "SELECT namespace, SUM(memories), SUM(episodes), SUM(sessions), "
            "MAX(last_updated) FROM ("
            "  SELECT namespace, COUNT(*) AS memories, 0 AS episodes, "
            "    0 AS sessions, MAX(updated_at) AS last_updated "
            "    FROM memories GROUP BY namespace"
            "  UNION ALL"
            "  SELECT namespace, 0, COUNT(*), 0, MAX(created_at) "
            "    FROM episodes GROUP BY namespace"
            "  UNION ALL"
            "  SELECT namespace, 0, 0, COUNT(*), MAX(updated_at) "
            "    FROM sessions GROUP BY namespace"
            ") AS tallies GROUP BY namespace ORDER BY namespace"
        )
        try:
            rows = self._conn().execute(sql).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "namespace": str(row[0]),
                "memories": int(row[1] or 0),
                "episodes": int(row[2] or 0),
                "sessions": int(row[3] or 0),
                "last_updated": str(row[4] or ""),
                "current": str(row[0]) == self.namespace,
            }
            for row in rows
        ]

    # ------------------------------------------------------------- migration
    def import_curated(
        self,
        memory: CuratedMemory,
        *,
        reason: str = "migrate",
        actor: str | None = None,
    ) -> None:
        """Write a record while preserving its stored ``updated_at``.

        Migration and restore must not rewrite history. Freshness is a ranking
        signal, so restamping every imported memory would flatten the corpus to
        "all equally new" and quietly reorder recall after a migration that was
        supposed to be transparent.
        """
        self._put_curated(
            memory, reason=reason, stamp=memory.updated_at, actor=actor
        )

    def import_session(self, session: SessionMemory) -> None:
        """Session counterpart of :meth:`import_curated`.

        Stores the session exactly as given: a migration must not restamp
        ``updated_at``, since that timestamp is the ordering key callers see.
        """
        self._put_session(session)


def _validate(model: type[Any], payload: str) -> Any | None:
    """Rebuild a model from its ``payload`` column, skipping corrupt rows.

    Mirrors the JSON backend, which ignores unreadable files instead of failing
    the whole listing. Memory is best-effort: one bad row must not empty recall.
    """
    try:
        data = json.loads(payload)
        return model.model_validate(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _marks(values: Any) -> str:
    """SQL placeholder list for an ``IN`` clause."""
    return ", ".join("?" for _ in values)


def _record_id(record: Any) -> str:
    """Stable tie-breaker for a scored row, whichever record shape it holds.

    Sorting by similarity alone leaves equal cosines in whatever order the scan
    produced, so a corpus edited between two runs would reorder its hits. Recall
    has to be deterministic, and the id is the one thing that always is.
    """
    return str(
        getattr(record, "memory_id", None)
        or getattr(record, "episode_id", "")
        or ""
    )


def _scored_rows(rows: Any, model: type[Any]) -> list[ScoredMemory]:
    """Turn FTS result rows into scored candidates, skipping corrupt payloads."""
    out: list[ScoredMemory] = []
    for row in rows:
        item = _validate(model, row["payload"])
        if item is None:
            continue
        try:
            rank = float(row["rank"])
        except (TypeError, ValueError):
            rank = 0.0
        out.append(ScoredMemory(memory=item, bm25=rank))
    return out
