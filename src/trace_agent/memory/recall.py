from __future__ import annotations

import hashlib
from typing import Any

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    RecallHit,
    RecallResult,
)
from trace_agent.memory.text import _keyword_score, _phrase_bonus


def _hash_text(*parts: str) -> str:
    seed = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(seed).hexdigest()[:16]


class RecallService:
    """Deterministic conditional recall over curated and episodic memory.

    The service never raises and never performs blocking I/O outside the
    caller-provided backend reads. A failed lookup returns an empty result so
    memory recall can only degrade analysis quality, never stop the task.
    """

    def __init__(self, backend: JsonMemoryBackend) -> None:
        self._backend = backend

    def recall(
        self,
        query: str,
        *,
        scenario_type: str | None = None,
        top_k: int = 5,
        include_episodic: bool = True,
    ) -> RecallResult:
        hits: list[RecallHit] = []
        try:
            memories = self._backend.list_curated(active_only=True)
            for memory in memories:
                if (
                    scenario_type
                    and memory.scenario_type
                    and memory.scenario_type != scenario_type
                ):
                    continue
                hits.append(self._score_curated(query, memory))

            if include_episodic:
                for episode in self._backend.list_episodes():
                    if (
                        scenario_type
                        and episode.scenario_type != scenario_type
                    ):
                        continue
                    hits.append(self._score_episode(query, episode))
        except Exception:
            return RecallResult(query=query, hits=[])

        hits = [hit for hit in hits if hit.score > 0.0]
        hits.sort(key=lambda item: item.score, reverse=True)
        top = hits[:top_k]
        return RecallResult(
            query=query,
            hits=top,
            context_prompt=self.render_context(top),
        )

    @staticmethod
    def _score_curated(query: str, memory: CuratedMemory) -> RecallHit:
        text = "\n".join(
            [
                memory.topic,
                memory.content,
                " ".join(memory.keywords),
                memory.kind.value,
                memory.scenario_type or "",
            ]
        )
        score = _keyword_score(query, text) + _phrase_bonus(query, text)
        score += 0.3 if any(kw in query.lower() for kw in memory.keywords) else 0
        return RecallHit(
            memory=memory,
            score=min(score, 1.0),
            why="curated keyword match",
        )

    @staticmethod
    def _score_episode(query: str, episode: EpisodicMemory) -> RecallHit:
        text = "\n".join(
            [
                episode.scenario_type,
                episode.scenario,
                episode.symptom,
                episode.summary,
                " ".join(episode.root_causes),
                episode.target_process or "",
                episode.device or "",
                episode.build or "",
            ]
        )
        score = _keyword_score(query, text) + _phrase_bonus(query, text)
        score += 0.1 if episode.target_process and (
            episode.target_process.lower() in query.lower()
        ) else 0
        return RecallHit(
            memory=episode,
            score=min(score, 1.0),
            why="episode keyword match",
        )

    @staticmethod
    def render_context(hits: list[RecallHit]) -> str:
        """Render compact, faceted memory context for the main analysis path."""
        if not hits:
            return ""

        curated_lines: list[str] = []
        episodic_lines: list[str] = []
        for hit in hits:
            memory = hit.memory
            if isinstance(memory, CuratedMemory):
                role = {
                    "pattern": "Pattern",
                    "fact": "Fact",
                    "procedure": "Procedure",
                    "anti-pattern": "Anti-pattern",
                }.get(memory.kind.value, memory.kind.value)
                curated_lines.append(
                    f"- [{role}] {memory.topic}: {memory.content}"
                )
            else:
                episode_info = (
                    f"{memory.scenario_type} / {memory.target_process}"
                    if memory.target_process
                    else memory.scenario_type
                )
                root_cause = (
                    memory.root_causes[0]
                    if memory.root_causes
                    else memory.summary or "no structured cause"
                )
                episodic_lines.append(
                    f"- [{episode_info}] {root_cause}"
                )

        blocks: list[str] = []
        if curated_lines:
            blocks.append(
                "Prior curated project knowledge:\n" + "\n".join(curated_lines)
            )
        if episodic_lines:
            blocks.append(
                "Similar prior analyses:\n" + "\n".join(episodic_lines)
            )
        return "\n\n".join(blocks)


def memory_id_for(content: str, *, scenario_type: str | None) -> str:
    scenario = scenario_type or "general"
    return f"mem-{scenario}-{_hash_text(content)}"
