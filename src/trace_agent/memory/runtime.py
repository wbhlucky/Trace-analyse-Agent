from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any

from trace_agent.memory.cache import RecallCache, build_recall_cache
from trace_agent.memory.consolidation import ConsolidationService
from trace_agent.memory.deep_recall import (
    DeepRecallAgent,
    DeepRecallService,
)
from trace_agent.memory.dreaming import DreamingService
from trace_agent.memory.embedding.factory import (
    EmbeddingSetup,
    build_embedding_provider,
)
from trace_agent.memory.embedding.index import SemanticIndex
from trace_agent.memory.execution import CheckpointResumeService
from trace_agent.memory.flags import MEMORY_CACHE_OFF, memory_cache_mode
from trace_agent.memory.intent import RecallIntentDetector
from trace_agent.memory.models import (
    CuratedMemory,
    DeepRecallResult,
    EpisodicMemory,
    ExecutionCheckpointSnapshot,
    RecallResult,
    SessionMemory,
)
from trace_agent.memory.paths import resolve_memory_root
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.recall import RecallService
from trace_agent.memory.session import SessionService
from trace_agent.memory.store import build_memory_store


class MemoryRuntime:
    """Customer-facing memory facade for one project.

    This is the single boundary the application uses for conditional recall,
    episodic capture, session memory, resume projection, and background
    consolidation. Every public method is best-effort: an empty hit, a skipped
    write, or a failed deep recall must never surface an exception to the
    analysis worker.
    """

    def __init__(
        self,
        root: Path,
        *,
        deep_recall_agent: DeepRecallAgent | None = None,
        store: MemoryStore | None = None,
        namespace: str | None = None,
        embedding: EmbeddingSetup | None = None,
        semantic: SemanticIndex | None = None,
        cache: RecallCache | None = None,
    ) -> None:
        # The store is injectable so tests can pin an adapter and so the
        # dashboard can share one connection pool across requests. Namespace is
        # resolved inside the factory when not given, which is what keeps eval
        # runs from reading the production corpus.
        self._backend: MemoryStore = (
            store if store is not None else build_memory_store(root, namespace=namespace)
        )
        # One index for the whole runtime, built here rather than inside the
        # services that use it: recall, the dreaming beat's backfill and the
        # dashboard's coverage read must share a single query-vector cache and a
        # single answer to "which recipe is in force". Three indexes would be
        # three caches and three chances to disagree.
        #
        # ``available`` is decided inside :class:`SemanticIndex` from both halves
        # - a provider and a store that can hold vectors - so constructing it
        # unconditionally is safe: with either half missing every method returns
        # an empty answer and recall is exactly the lexical recall it was before.
        self._embedding: EmbeddingSetup = (
            embedding if embedding is not None else build_embedding_provider()
        )
        self._semantic: SemanticIndex = (
            semantic
            if semantic is not None
            else SemanticIndex(self._backend, self._embedding.provider)
        )
        self._cache = cache if cache is not None else self._default_cache()
        self._recall_service = RecallService(
            self._backend, semantic=self._semantic, cache=self._cache
        )
        self._intent = RecallIntentDetector()
        self._deep_recall = DeepRecallService(
            self._backend,
            recall_agent=deep_recall_agent,
        )
        self._consolidation_service = ConsolidationService(self._backend)
        self._session_service = SessionService(self._backend)
        self._resume_service = CheckpointResumeService(self._backend)
        self._dreaming_service = DreamingService(self._backend, semantic=self._semantic)

    @property
    def backend(self) -> MemoryStore:
        return self._backend

    @property
    def store(self) -> MemoryStore:
        """Alias for :attr:`backend` using the section-3.2 vocabulary."""
        return self._backend

    @property
    def semantic(self) -> SemanticIndex:
        """The vector side of hybrid retrieval.

        Always present; check :attr:`~SemanticIndex.available` before relying on
        it. Exposed because backfilling, rebuilding after a recipe change and
        reporting coverage are all operator actions, and each of them needs the
        same object recall is using rather than a second one built from config.
        """
        return self._semantic

    @property
    def embedding(self) -> EmbeddingSetup:
        """What was configured for embeddings, and why it is or is not usable."""
        return self._embedding

    def _default_cache(self) -> RecallCache | None:
        """The configured recall cache, or ``None`` when caching is off.

        ``None`` rather than a :class:`~trace_agent.memory.cache.NullCache` for
        the off case: a null cache still has to be handed a key, and building
        one reads ``corpus_epoch`` from the store. The default deployment should
        not pay a query per recall for a feature it did not turn on.
        """
        if memory_cache_mode() == MEMORY_CACHE_OFF:
            return None
        return build_recall_cache(self._backend)

    @property
    def cache(self) -> RecallCache | None:
        """The recall cache in effect, for diagnostics and explicit flushing."""
        return self._cache

    def cache_stats(self) -> dict[str, Any]:
        """Cache counters, including why caching is not in effect if it is not."""
        if self._cache is None:
            return {"kind": MEMORY_CACHE_OFF, "entries": 0, "hits": 0, "misses": 0}
        try:
            return dict(self._cache.snapshot())
        except Exception:
            return {"kind": getattr(self._cache, "kind", "unknown"), "error": True}

    @property
    def sessions(self) -> SessionService:
        return self._session_service

    @property
    def dreaming(self) -> DreamingService:
        return self._dreaming_service

    def recall(
        self,
        query: str,
        *,
        scenario_type: str | None = None,
        top_k: int = 5,
        include_episodic: bool = True,
        allow_deep: bool = True,
    ) -> RecallResult:
        started = perf_counter()
        fast = self._recall_service.recall(
            query,
            scenario_type=scenario_type,
            top_k=top_k,
            include_episodic=include_episodic,
        )
        if not allow_deep or fast.hits:
            return fast

        best_score = max((hit.score for hit in fast.hits), default=0.0)
        intent = self._intent.detect(query, best_score=best_score)
        if not intent.deep:
            return fast

        deep = self.deep_recall(
            query,
            fast_result=fast,
            scenario_type=scenario_type,
            top_k=top_k,
        )
        if not deep.context_prompt:
            return fast
        return RecallResult(
            query=query,
            hits=fast.hits,
            context_prompt=deep.context_prompt,
            # The deep renderer builds its own text, so the ids that reached
            # the prompt come from it and not from the fast pass. Reporting the
            # fast ids here would claim an injection that never happened.
            injected_ids=deep.source_ids,
            # Deep recall only runs when the fast pass came up empty, so the
            # stamp on ``fast`` covers a search that found nothing and says
            # nothing about the work behind this answer. Measuring the whole
            # call keeps ``latency_ms`` meaning "how long the caller waited" on
            # both paths rather than on the cheap one only.
            latency_ms=round((perf_counter() - started) * 1000.0, 3),
        )

    def deep_recall(
        self,
        query: str,
        *,
        fast_result: RecallResult | None = None,
        scenario_type: str | None = None,
        top_k: int = 8,
    ) -> DeepRecallResult:
        return self._deep_recall.recall(
            query,
            fast_result=fast_result,
            scenario_type=scenario_type,
            top_k=top_k,
        )

    def remember_episode(
        self,
        episode: EpisodicMemory,
        *,
        consolidate: bool = True,
        dream: bool = False,
    ) -> list[CuratedMemory]:
        """Persist an episode and promote candidate curated memories.

        When ``dream`` is True the episode is only enqueued for the delayed
        dreaming pass; otherwise consolidation runs immediately (best-effort).
        """
        self._backend.write_episode(episode)
        if dream:
            self._dreaming_service.enqueue_episode(episode.episode_id)
            return []
        if not consolidate:
            return []
        return self._consolidation_service.consolidate(episode)

    def append_session_message(
        self,
        session_id: str,
        content: str,
        *,
        role: str = "user",
        references: list[str] | None = None,
    ) -> SessionMemory | None:
        from trace_agent.memory.models import SessionRole

        try:
            role_value = SessionRole(role)
        except ValueError:
            role_value = SessionRole.USER
        return self._session_service.append_message(
            session_id,
            content,
            role=role_value,
            references=references,
        )

    def resume_plan(
        self,
        output_dir: str | Path,
    ) -> ExecutionCheckpointSnapshot:
        return self._resume_service.snapshot(output_dir)

    def list_episodes(self) -> list[EpisodicMemory]:
        return self._backend.list_episodes()

    def list_curated(self) -> list[CuratedMemory]:
        return self._backend.list_curated()


def default_memory_runtime(
    project_root: Path,
    *,
    deep_recall_agent: DeepRecallAgent | None = None,
    namespace: str | None = None,
    store: MemoryStore | None = None,
    embedding: EmbeddingSetup | None = None,
    semantic: SemanticIndex | None = None,
) -> MemoryRuntime:
    """Build the project-scoped memory runtime for one project root.

    Choosing a store does open the database file, because the factory probes it
    in order to fall back to the file adapter when SQLite or FTS5 is unusable.
    Everything else stays lazy: no record is read or written until asked.
    """
    return MemoryRuntime(
        resolve_memory_root(project_root),
        deep_recall_agent=deep_recall_agent,
        namespace=namespace,
        store=store,
        embedding=embedding,
        semantic=semantic,
    )
