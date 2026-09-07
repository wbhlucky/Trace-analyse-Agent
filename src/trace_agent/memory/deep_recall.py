from __future__ import annotations

from typing import Any, Callable

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import (
    CuratedMemory,
    DeepRecallResult,
    EpisodicMemory,
    RecallResult,
    SessionMemory,
)
from trace_agent.memory.session import SessionService
from trace_agent.memory.text import _keyword_score, _phrase_bonus


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
        backend: JsonMemoryBackend,
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
            items: list[tuple[float, str]] = []

            for memory in self._backend.list_curated(active_only=True):
                if scenario_type and memory.scenario_type and memory.scenario_type != scenario_type:
                    continue
                text = self._curated_text(memory)
                score = _keyword_score(query, text) + _phrase_bonus(query, text)
                if score > 0:
                    items.append((score, self._curated_line(memory)))

            for episode in self._backend.list_episodes():
                if scenario_type and episode.scenario_type != scenario_type:
                    continue
                text = self._episode_text(episode)
                score = _keyword_score(query, text) + _phrase_bonus(query, text)
                if score > 0:
                    items.append((score, self._episode_line(episode)))

            for session in self._backend.list_sessions():
                text = self._session_text(session)
                score = _keyword_score(query, text) + _phrase_bonus(query, text)
                if score > 0:
                    items.append((score, self._session_line(session)))

            items.sort(key=lambda item: item[0], reverse=True)
            chosen = items[:top_k]
            context = "\n".join(f"- {line}" for _, line in chosen)

            if self._recall_agent is not None and context:
                try:
                    source = [{"score": score, "text": line} for score, line in chosen]
                    context = self._recall_agent(query, source) or context
                except Exception:
                    pass

            return DeepRecallResult(
                query=query,
                context_prompt=context,
                source_count=len(chosen),
            )
        except Exception:
            return DeepRecallResult(query=query)

    @staticmethod
    def _curated_text(item: CuratedMemory) -> str:
        return " ".join([item.topic, item.content, " ".join(item.keywords)])

    @staticmethod
    def _curated_line(item: CuratedMemory) -> str:
        return f"[{item.kind.value}] {item.topic}: {item.content}"

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
    def _episode_line(item: EpisodicMemory) -> str:
        return (
            f"[episode {item.scenario_type}] "
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
