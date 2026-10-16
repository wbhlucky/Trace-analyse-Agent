from __future__ import annotations

from typing import Any, Callable

from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.flags import memory_include_candidate, memory_min_score
from trace_agent.memory.models import (
    TRUSTED_STATUSES,
    CuratedMemory,
    DeepRecallResult,
    EpisodicMemory,
    MemoryStatus,
    RecallResult,
    SessionMemory,
)
from trace_agent.memory.session import SessionService
from trace_agent.memory.rerank import episode_trust, lexical_score


DeepRecallAgent = Callable[[str, list[dict[str, Any]]], str]


class DeepRecallService:
    """Two-tier recall fallback for history/comparison intent.

    By default this is deterministic: it broadens retrieval over sessions,
    episodes, and curated records and produces a facet-labelled context. A
    caller may inject ``recall_agent`` to add a model-generated summary, but
    the service must never block the main task waiting for it.
    """

    def __init__(
        self,
        backend: MemoryStore,
        *,
        recall_agent: DeepRecallAgent | None = None,
    ) -> None:
        self._backend = backend
        self._recall_agent = recall_agent
        self._session_service = SessionService(backend)

    def recall(
        self,
        query: str,
        *,
        fast_result: RecallResult | None = None,
        scenario_type: str | None = None,
        top_k: int = 8,
    ) -> DeepRecallResult:
        try:
            # ``(score, rendered line, source id)``. The id rides along so a deep
            # recall prompt is as attributable as a fast one: section 9 asks
            # which memories were in front of the model, and a line of text with
            # no handle cannot answer that.
            items: list[tuple[float, str, str]] = []
            floor = memory_min_score()
            allowed = set(TRUSTED_STATUSES)
            include_candidate = memory_include_candidate()
            if include_candidate:
                allowed.add(MemoryStatus.CANDIDATE)

            for memory in self._backend.list_curated(active_only=True):
                if memory.status not in allowed:
                    continue
                if scenario_type and memory.scenario_type and memory.scenario_type != scenario_type:
                    continue
                score = lexical_score(query, self._curated_text(memory), memory.keywords)
                if score >= floor:
                    items.append(
                        (
                            score,
                            self._curated_line(memory, include_candidate),
                            memory.memory_id,
                        )
                    )

            for episode in self._backend.list_episodes():
                if scenario_type and episode.scenario_type != scenario_type:
                    continue
                # Same rule as fast recall: an episode whose findings are all
                # ``suspected`` is one run's guess, and widening the net is not a
                # reason to relax the trust gate. Without this the deep path was
                # the one door left open into the prompt for an unverified episode.
                if episode_trust(episode) not in allowed:
                    continue
                score = lexical_score(query, self._episode_text(episode), episode.tags)
                if score >= floor:
                    items.append(
                        (score, self._episode_line(episode, include_candidate), episode.episode_id)
                    )

            for session in self._backend.list_sessions():
                score = lexical_score(query, self._session_text(session))
                if score >= floor:
                    items.append(
                        (score, self._session_line(session), session.session_id)
                    )

            items.sort(key=lambda item: (item[0], item[2]), reverse=True)
            chosen = items[:top_k]
            context = "\n".join(f"- {line}" for _, line, _ in chosen)

            if self._recall_agent is not None and context:
                try:
                    source = [
                        {"score": score, "text": line, "id": source_id}
                        for score, line, source_id in chosen
                    ]
                    context = self._recall_agent(query, source) or context
                except Exception:
                    pass

            return DeepRecallResult(
                query=query,
                context_prompt=context,
                source_count=len(chosen),
                source_ids=[source_id for _, _, source_id in chosen],
            )
        except Exception:
            return DeepRecallResult(query=query)

    @staticmethod
    def _curated_text(item: CuratedMemory) -> str:
        return " ".join([item.topic, item.content, " ".join(item.keywords)])

    @staticmethod
    def _curated_line(item: CuratedMemory, include_candidate: bool = False) -> str:
        # Deep recall widens the net, so it needs the trust label even more than
        # fast recall: an unverified guess must not read like project knowledge.
        marker = item.kind.value
        if item.status is MemoryStatus.CANDIDATE and include_candidate:
            marker = f"{marker} | unverified candidate"
        return f"[{marker}] {item.topic}: {item.content}"

    @staticmethod
    def _episode_text(item: EpisodicMemory) -> str:
        return " ".join([
            item.scenario_type,
            item.scenario,
            item.symptom,
            item.summary,
            " ".join(item.root_causes),
            item.target_process or "",
            item.device or "",
            item.build or "",
        ])

    @staticmethod
    def _episode_line(item: EpisodicMemory, include_candidate: bool = False) -> str:
        marker = f"episode {item.scenario_type}"
        if episode_trust(item) is MemoryStatus.CANDIDATE and include_candidate:
            marker = f"{marker} | unverified candidate"
        return (
            f"[{marker}] "
            f"{item.target_process or item.device or ''}: "
            f"{item.root_causes[0] if item.root_causes else item.summary}"
        )

    @staticmethod
    def _session_text(item: SessionMemory) -> str:
        return " ".join([item.title, item.topic, *[m.content for m in item.messages]])

    @staticmethod
    def _session_line(item: SessionMemory) -> str:
        last = item.messages[-1].content if item.messages else ""
        return f"[session] {item.title or item.topic}: {last[:160]}"
