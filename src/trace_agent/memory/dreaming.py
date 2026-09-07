from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.consolidation import ConsolidationService
from trace_agent.memory.models import CuratedMemory, EpisodicMemory


@dataclass(slots=True)
class DreamBatch:
    processed: int = 0
    created: list[str] = field(default_factory=list)
    skipped: int = 0
    errors: int = 0


class DreamingService:
    """Delayed background consolidation for the memory loop.

    Analysis completion enqueues a durable dream job, and a later maintenance
    beat consumes the queue. It separates candidate production from long-term
    promotion so a deep consolidation pass cannot add latency on the hot path.
    """

    def __init__(self, backend: JsonMemoryBackend) -> None:
        self._backend = backend
        self._consolidation = ConsolidationService(backend)

    def enqueue_episode(self, episode_id: str) -> None:
        self._backend.append_dream_job(
            {
                "kind": "episode",
                "episode_id": episode_id,
            }
        )

    def enqueue_deep_promote(self, *, scenario_type: str | None = None) -> None:
        self._backend.append_dream_job(
            {
                "kind": "deeppromote",
                "scenario_type": scenario_type,
            }
        )

    def run_once(self, *, limit: int = 20) -> DreamBatch:
        batch = DreamBatch()
        jobs = self._backend.read_dream_jobs(limit=limit)
        if not jobs:
            return batch
        self._backend.clear_dream_jobs()

        for job in jobs:
            try:
                kind = job.get("kind")
                if kind == "episode":
                    episode_id = str(job.get("episode_id") or "")
                    episode = self._backend.load_episode(episode_id)
                    if episode is not None:
                        created = self._consolidation.consolidate(episode)
                        batch.created.extend(item.memory_id for item in created)
                        batch.processed += 1
                    else:
                        batch.skipped += 1
                elif kind == "deeppromote":
                    scenario_type = job.get("scenario_type")
                    promoted = self._deep_promote(scenario_type=scenario_type)
                    batch.created.extend(promoted)
                    batch.processed += 1
                else:
                    batch.skipped += 1
            except Exception:
                batch.errors += 1
        return batch

    def _deep_promote(self, *, scenario_type: str | None) -> list[str]:
        memories = self._backend.list_curated(active_only=False)
        promoted: list[str] = []
        for memory in memories:
            if scenario_type and memory.scenario_type != scenario_type:
                continue
            if memory.status.value == "candidate" and memory.occurrences >= 2:
                updated = memory.model_copy(update={"status": "active"})
                self._backend.write_curated(updated)
                promoted.append(memory.memory_id)
        return promoted
