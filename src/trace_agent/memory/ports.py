"""Storage contracts for the long-term memory corpus.

Before P1 every memory service took a concrete :class:`JsonMemoryBackend`, so
retrieval was an O(N) walk over JSON files and the on-disk layout leaked into
the whole module. This package defines the port those services should depend on
instead, plus the small value objects that cross it.

Two deliberate choices keep the change safe:

* The port **includes** the legacy backend surface (``write_curated``,
  ``list_episodes``, ...). Adapters stay drop-in for the existing services, so
  ``TRACE_AGENT_MEMORY_STORE=json`` is a genuine one-variable rollback rather
  than a second code path that drifts.
* ``search`` is the only *optional* member. A store that can filter and rank
  candidates itself (SQLite + FTS5) exposes it; a store that cannot (JSON) does
  not, and the caller falls back to scanning. Scoring stays in the reranker
  either way, so both adapters must return the same hits for the same query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from collections.abc import Sequence
from typing import Any, Protocol

from trace_agent.memory.models import (
    TRUSTED_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    ExecutionCheckpointSnapshot,
    MemoryKind,
    MemoryStatus,
    SessionMemory,
)

# The design doc calls the recall-facing trust axis ``MemoryTrust``. It is an
# alias, not a second enum: P0 already added ``verified`` to ``MemoryStatus``,
# and a parallel enum would mean two sources of truth for one lifecycle. The
# ``trust`` column stores ``MemoryStatus`` values verbatim.
MemoryTrust = MemoryStatus


class MemoryRelation(StrEnum):
    """Typed edges between two memories.

    One vocabulary for both adapters, because ``contradicts`` is not just a
    label: it is the evidence the quality gate counts before archiving a
    memory, and a string that differs by adapter would make the gate
    adapter-dependent.
    """

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"
    DUPLICATE_OF = "duplicate-of"
    DERIVED_FROM = "derived-from"


class MemoryType(StrEnum):
    """What job a memory does, orthogonal to its content shape.

    ``MemoryKind`` (pattern/fact/procedure/anti-pattern) describes the *shape*
    of a record and is what existing data carries. ``MemoryType`` describes its
    *function* in recall, which is what namespace filtering, per-type top-k
    quotas and prompt partitioning need. Both axes are kept.
    """

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"
    PROFILE = "profile"
    PROJECT = "project"
    SUMMARY = "summary"


ALL_MEMORY_TYPES: frozenset[MemoryType] = frozenset(MemoryType)

# A ``fact`` is stable domain knowledge and a ``pattern`` is a distilled
# regularity, so both are semantic. ``procedure``/``anti-pattern`` tell the
# agent how to investigate, so they are procedural. The remaining three are the
# dreaming beat's and the extractor's own products: ``summary`` is a session
# digest, ``profile`` is the device/build metadata a run reported about itself,
# and ``project`` is the rolling per-namespace state digest.
#
# ``episodic`` is the one type with no kind, and that is structural rather than
# a missing producer: episodes live in their own table and reach recall as
# :class:`~trace_agent.memory.models.EpisodicMemory`, never as a
# ``CuratedMemory``.
#
# **Deviation from section 2.3.** The plan maps ``device/build 事实 → (FACT,
# PROFILE)``, i.e. one kind carrying two types. Type is *derived* from kind here
# so a record cannot disagree with itself, which makes that pair inexpressible:
# a ``FACT`` is always ``SEMANTIC``. Giving the two functions their own kinds is
# the same call the plan's own ``SUMMARY`` row already made - it lists
# ``SUMMARY`` as a type whose source is ``SessionMemory.summary``, and the
# implementation shipped it as ``MemoryKind.SUMMARY``.
TYPE_FOR_KIND: dict[MemoryKind, MemoryType] = {
    MemoryKind.FACT: MemoryType.SEMANTIC,
    MemoryKind.PATTERN: MemoryType.SEMANTIC,
    MemoryKind.PROCEDURE: MemoryType.PROCEDURAL,
    MemoryKind.ANTI_PATTERN: MemoryType.PROCEDURAL,
    MemoryKind.SUMMARY: MemoryType.SUMMARY,
    MemoryKind.PROFILE: MemoryType.PROFILE,
    MemoryKind.PROJECT: MemoryType.PROJECT,
}


def memory_type_for(
    kind: MemoryKind,
    *,
    default: MemoryType = MemoryType.SEMANTIC,
) -> MemoryType:
    """Functional type for a content kind, falling back to ``semantic``."""
    return TYPE_FOR_KIND.get(kind, default)


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """A structured recall request.

    Replaces the old "join every field with a space" query string. Declaring
    filters as data is what lets a store push them into SQL (``WHERE namespace =
    ? AND trust IN (...)``) instead of deserialising the whole corpus and
    discarding most of it.

    ``limit`` sizes the *candidate pool*, not the final answer: the caller still
    reranks and truncates to ``top_k``. It must stay generous enough that
    nothing which could clear the relevance floor is left behind.

    **Deviation from the plan.** Section 6.1 sketches a query carrying ``scope``,
    ``trusts``, ``top_k``, ``min_score`` and ``budget_chars``. What ships is
    narrower, and the difference is deliberate:

    * ``statuses`` is the plan's ``trusts``. The corpus says ``status``, the SQL
      column says ``trust``; the query keeps the domain name so it round-trips
      through :attr:`CuratedMemory.trust` without a translation table.
    * ``scenario_type`` stands in for a general ``scope`` object. It is the only
      scope dimension recall actually filters on today; adding the rest to the
      Port before a caller needs them would freeze a shape nobody has exercised.
    * ``top_k``, ``min_score`` and ``budget_chars`` live on the *reranker* and
      the *context composer*, not here. A store that knew the prompt budget
      would be a store that had to change when the prompt did. ``limit`` is the
      only sizing knob the store gets, and it sizes the candidate pool.
    """

    text: str = ""
    namespace: str | None = None
    scenario_type: str | None = None
    statuses: frozenset[MemoryStatus] = field(
        default_factory=lambda: TRUSTED_STATUSES
    )
    types: frozenset[MemoryType] | None = None
    include_episodic: bool = True
    limit: int = 50


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """One store-level candidate, before the reranker has scored it.

    ``bm25`` is the store's own lexical rank, kept for observability and pool
    ordering. It is *not* the final score: ``RecallService`` recomputes
    ``lexical_score`` so a hit means the same thing on every adapter.
    """

    memory: CuratedMemory | EpisodicMemory
    bm25: float | None = None
    #: Cosine against the query vector, present only on candidates that came
    #: from :meth:`MemoryStore.vector_search`. Kept separate from ``bm25``
    #: because the two are not comparable and a merged pool must be able to say
    #: which channel produced a row.
    similarity: float | None = None


@dataclass(frozen=True, slots=True)
class PendingEmbedding:
    """One record the store reports as needing a vector.

    The store produces these because only the store knows which rows are missing
    an embedding or carry a stale ``embedding_model``. The text is the same
    concatenation the FTS index uses (:mod:`trace_agent.memory.search_text`), so
    a field added to lexical relevance is embedded too rather than silently
    invisible to the semantic channel.
    """

    record_id: str
    text: str
    episode: bool = False
    #: Model the stored vector was produced with, if any. Reporting only.
    stored_model: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryStats:
    """Corpus shape, surfaced by ``memory stats`` and the dashboard."""

    store: str = ""
    namespace: str = ""
    memories: int = 0
    episodes: int = 0
    sessions: int = 0
    by_trust: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    contradictions: int = 0
    versions: int = 0
    links: int = 0
    expired: int = 0
    duplicates: int = 0
    superseded: int = 0


class MemoryStore(Protocol):
    """Everything the memory services may ask a store to do."""

    root: Any

    # ------------------------------------------------------------ episodes
    def write_episode(self, episode: EpisodicMemory) -> Any: ...

    def load_episode(self, episode_id: str) -> EpisodicMemory | None: ...

    def list_episodes(self) -> list[EpisodicMemory]: ...

    def mark_episode_graded(
        self,
        episode_id: str,
        *,
        graded_pass: bool,
        graded_score: float | None = None,
        eval_run_id: str | None = None,
    ) -> bool:
        """Stamp a deterministic grade onto one episode (section 8.5).

        Returns ``False`` when the episode is unknown, so a caller cannot
        mistake "no such run" for "graded". The grade is evidence for the
        quality gate's ``active -> verified`` path, which is why it lives on the
        episode rather than on the memory: one run's correctness says something
        about that run, and only the gate may generalise it.
        """
        ...

    # ------------------------------------------------------------- curated
    def write_curated(self, memory: CuratedMemory) -> Any: ...

    def load_curated(self, memory_id: str) -> CuratedMemory | None: ...

    def list_curated(self, *, active_only: bool = False) -> list[CuratedMemory]: ...

    def remove_curated(self, memory_id: str) -> bool: ...

    # ------------------------------------------------------------ sessions
    def write_session(self, session: SessionMemory) -> Any: ...

    def load_session(self, session_id: str) -> SessionMemory | None: ...

    def list_sessions(self) -> list[SessionMemory]: ...

    # ------------------------------------------------------- dream queue
    def append_dream_job(self, payload: dict[str, object]) -> None: ...

    def read_dream_jobs(self, *, limit: int = 100) -> list[dict[str, object]]: ...

    def clear_dream_jobs(self) -> None: ...

    # -------------------------------------------------------- checkpoint
    def execution_snapshot(
        self, output_dir: Any
    ) -> ExecutionCheckpointSnapshot: ...

    # ------------------------------------------------- indexed retrieval
    def search(self, query: MemoryQuery) -> list[ScoredMemory]: ...

    # --------------------------------------------------------- governance
    def add_source(
        self, memory_id: str, *, run_id: str, episode_id: str = ""
    ) -> bool: ...

    def distinct_runs(self, memory_id: str) -> int: ...

    def set_trust(
        self,
        memory_id: str,
        trust: MemoryTrust,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> bool: ...

    # ``write_curated`` records its version as ``"consolidate"`` because that is
    # what it is for. Governance edits - a conflict detector halving confidence,
    # retention stamping a deadline - need to say who changed what and why, so
    # they go through ``revise`` instead and land in ``memory_versions`` with a
    # real reason.
    def revise(
        self,
        memory: CuratedMemory,
        *,
        reason: str,
        actor: str | None = None,
    ) -> bool: ...

    def link(
        self,
        left: str,
        right: str,
        relation: str,
        *,
        evidence: str | None = None,
    ) -> None: ...

    def touch(self, memory_id: str) -> None: ...

    def expire(self, now: datetime | None = None) -> int: ...

    def count_links(self, memory_id: str, relation: str | None = None) -> int: ...

    def linked_ids(
        self, memory_id: str, relation: str | None = None
    ) -> list[str]: ...

    def list_links(
        self, relation: str | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    def recent_versions(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    # ``import_curated`` is the one write that must not restamp ``updated_at``.
    # It was added for migration, and retention reuses it for the same reason:
    # stamping a deadline is bookkeeping, and a sweep that refreshed freshness
    # on every pass would make the whole corpus look brand new and silently
    # reorder recall.
    # ``actor`` is here for the same reason ``revise``/``update_trust`` carry it:
    # an import that changes trust has to be attributable in ``memory_versions``,
    # and section 14 counts "every transition carries reason + actor" as a hard
    # metric. Adapters without a version table accept it for parity and drop it.
    def import_curated(
        self,
        memory: CuratedMemory,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> Any: ...

    def stats(self) -> MemoryStats: ...

    def reindex(self) -> int: ...

    # ------------------------------------------------------- semantic (P3)
    # Optional, like ``search``: a store that cannot hold vectors simply does
    # not implement these and recall stays lexical. Probed with
    # :func:`store_supports_vectors` rather than assumed.
    def pending_embeddings(
        self, *, model: str, limit: int = 32
    ) -> list[PendingEmbedding]: ...

    def set_embedding(
        self,
        record_id: str,
        vector: Sequence[float],
        *,
        model: str,
        episode: bool = False,
    ) -> bool: ...

    def vectors_for(
        self, record_ids: Sequence[str], *, episode: bool = False
    ) -> dict[str, list[float]]: ...

    def vector_search(
        self, vector: Sequence[float], query: MemoryQuery
    ) -> list[ScoredMemory]: ...

    #: Delete vectors that were not produced by ``keep_model``. This is what
    #: makes a model change a rebuild instead of a corpus of incomparable
    #: vectors, and it is why the identity travels with the value.
    def prune_embeddings(self, *, keep_model: str) -> int: ...

    def embedding_coverage(self) -> dict[str, Any]: ...

    # ------------------------------------------------------- cache & tenancy (P4)
    # Both are optional like ``search``: they exist so the recall cache and the
    # ``memory namespaces`` CLI can work against any adapter, and a store that
    # cannot answer them degrades to "never cache" and "one namespace" rather
    # than to a wrong answer.
    def corpus_epoch(self) -> int:
        """Monotonic counter of writes to this store's namespace.

        The cache keys itself on this value, which is what makes invalidation
        implicit: a writer never has to know which queries it affected, because
        advancing the epoch retires every cached answer at once. Implementations
        must bump it in the same transaction as the data they write, so a rolled
        back write cannot invalidate anything and a committed one cannot fail to.
        """
        ...

    def list_namespaces(self) -> list[dict[str, Any]]:
        """Isolation domains present in this store, each with record counts.

        Returns dicts rather than models because this is an inspection surface
        for the CLI and the dashboard, not a domain object: the shape is
        ``namespace / memories / episodes / sessions / last_updated / current``,
        and a caller that renders it should tolerate an adapter adding keys.
        """
        ...


def store_supports_pushdown(store: object) -> bool:
    """Whether a store can filter and rank candidates itself.

    ``search`` is the one optional port member, so callers probe for it instead
    of assuming. The JSON adapter deliberately does not implement it: a scan is
    already what it does, and pretending otherwise would hide the difference
    the SQLite index buys.
    """
    return callable(getattr(store, "search", None))


def store_supports_vectors(store: object) -> bool:
    """Whether a store can hold and search embeddings.

    Both members are required because half a vector store is worse than none: a
    store that can write vectors but not search them would accumulate a corpus
    nobody reads, and one that can search but not report pending work would
    never be filled.
    """
    return callable(getattr(store, "vector_search", None)) and callable(
        getattr(store, "pending_embeddings", None)
    )


def store_supports_epoch(store: object) -> bool:
    """Whether a store can report a write counter for its namespace.

    The recall cache refuses to cache without one. Caching against a store that
    cannot say when it changed would mean guessing an invalidation rule per
    writer, and the first writer added later would silently start serving stale
    priors - which is the failure mode section 1.2 defect A showed is expensive
    to notice.
    """
    return callable(getattr(store, "corpus_epoch", None))
