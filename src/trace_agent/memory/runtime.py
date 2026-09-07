from __future__ import annotations

from pathlib import Path

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.consolidation import ConsolidationService
from trace_agent.memory.deep_recall import (
    DeepRecallAgent,
    DeepRecallService,
)
from trace_agent.memory.dreaming import DreamingService
from trace_agent.memory.execution import CheckpointResumeService
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
from trace_agent.memory.recall import RecallService
from trace_agent.memory.session import SessionService


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
    ) -> None:
        self._backend = JsonMemoryBackend(root)
        self._recall_service = RecallService(self._backend)
        self._intent = RecallIntentDetector()
        self._deep_recall = DeepRecallService(
            self._backend,
            recall_agent=deep_recall_agent,
        )
        self._consolidation_service = ConsolidationService(self._backend)
        self._session_service = SessionService(self._backend)
        self._resume_service = CheckpointResumeService(self._backend)
        self._dreaming_service = DreamingService(self._backend)

    @property
    def backend(self) -> JsonMemoryBackend:
        return self._backend

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
) -> MemoryRuntime:
    """Build the project-scoped memory runtime without touching disk yet."""
    return MemoryRuntime(
        resolve_memory_root(project_root),
        deep_recall_agent=deep_recall_agent,
    )
