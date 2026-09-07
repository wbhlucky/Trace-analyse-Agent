from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from trace_agent.models import FindingSeverity, utc_now
from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryConfidence,
    MemoryKind,
    MemoryStatus,
    Provenance,
)

_SEVERITY_RANK = {
    FindingSeverity.LOW.value: 0,
    FindingSeverity.MEDIUM.value: 1,
    FindingSeverity.HIGH.value: 2,
    FindingSeverity.CRITICAL.value: 3,
}


def _canonical_content(topic: str, content: str) -> str:
    text = f"{topic}\n{content}"
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _finding_name(finding: dict[str, Any]) -> str:
    for key in ("title", "analysis", "recommendation"):
        value = finding.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().split("\n")[0][:120]
    return "unnamed finding"


def _finding_memory_kind(finding: dict[str, Any]) -> MemoryKind:
    status = finding.get("status")
    recommendation = str(finding.get("recommendation", ""))
    if status == "suspected":
        return MemoryKind.PATTERN
    if recommendation.strip():
        return MemoryKind.PROCEDURE
    return MemoryKind.FACT


class ConsolidationService:
    """Turn episodes into reusable curated memory via background compaction.

    The service is deliberately conservative: it only promotes content that is
    already stored in the episode projection, and it writes candidate memories
    idempotently so the same run can be consolidated more than once.
    """

    def __init__(self, backend: JsonMemoryBackend) -> None:
        self._backend = backend

    def consolidate(self, episode: EpisodicMemory) -> list[CuratedMemory]:
        created: list[CuratedMemory] = []
        candidates = self._extract_candidates(episode)

        for candidate in candidates:
            memory_id = self._dedupe(candidate)
            existing = self._backend.load_curated(memory_id)
            if existing is None:
                self._backend.write_curated(candidate)
                created.append(candidate)
            else:
                updated = self._promote(existing, candidate)
                self._backend.write_curated(updated)
                created.append(updated)
        return created

    @staticmethod
    def _promote(
        existing: CuratedMemory,
        incoming: CuratedMemory,
    ) -> CuratedMemory:
        should_consolidate = True
        status = existing.status
        if status in {
            MemoryStatus.ACTIVE,
            MemoryStatus.CANDIDATE,
        }:
            status = MemoryStatus.ACTIVE
        elif status is MemoryStatus.CONSOLIDATED:
            should_consolidate = False

        confidence = min(1.0, existing.confidence)
        occurrences = existing.occurrences + 1
        references = list(existing.provenance.references)
        references.extend(incoming.provenance.references)
        deduped_references = list(dict.fromkeys(references))

        return existing.model_copy(
            update={
                "status": status,
                "occurrences": occurrences,
                "provenance": Provenance(
                    source_type=existing.provenance.source_type,
                    references=deduped_references,
                    created_by=existing.provenance.created_by,
                    validated_by=existing.provenance.validated_by,
                ),
                "content": incoming.content,
                "topic": incoming.topic,
                "severity": _max_severity(existing.severity, incoming.severity),
                "updated_at": utc_now(),
            }
        )

    @staticmethod
    def _dedupe(item: CuratedMemory) -> str:
        return item.memory_id

    def _extract_candidates(
        self,
        episode: EpisodicMemory,
    ) -> list[CuratedMemory]:
        candidates: list[CuratedMemory] = []
        seen: set[str] = set()

        for finding in episode.findings:
            name = _finding_name(finding)
            content = str(
                finding.get("analysis") or finding.get("recommendation") or ""
            ).strip()
            topic = name
            if not content:
                continue
            digest = _canonical_content(topic, content)
            if digest in seen:
                continue
            seen.add(digest)

            kind = _finding_memory_kind(finding)
            severity_raw = finding.get("severity")
            severity = None
            if isinstance(severity_raw, str):
                try:
                    severity = FindingSeverity(severity_raw)
                except ValueError:
                    severity = None

            confidence = float(finding.get("confidence") or 0.0)
            confidence = max(0.0, min(1.0, confidence))

            references: list[str] = [episode.episode_id]
            if episode.run_id:
                references.append(f"run:{episode.run_id}")
            if isinstance(finding.get("evidence_ids"), list):
                references.extend(
                    f"evidence:{item}" for item in finding["evidence_ids"]
                )

            candidates.append(
                CuratedMemory(
                    memory_id=f"mem-{episode.scenario_type}-{digest}",
                    kind=kind,
                    scenario_type=episode.scenario_type,
                    topic=topic,
                    content=content,
                    keywords=self._keywords(topic, content, episode),
                    severity=severity,
                    confidence=confidence,
                    status=MemoryStatus.CANDIDATE,
                    provenance=Provenance(
                        source_type="finding",
                        references=references,
                        validated_by=None if confidence >= 0.6 else "pending",
                    ),
                )
            )

        if not candidates and (episode.summary or episode.root_causes):
            candidates.extend(self._extract_episode_candidates(episode))
        return candidates

    @staticmethod
    def _extract_episode_candidates(
        episode: EpisodicMemory,
    ) -> list[CuratedMemory]:
        candidates: list[CuratedMemory] = []
        for root_cause in episode.root_causes[:3]:
            content = root_cause.strip()
            if not content:
                continue
            topic = (
                episode.symptom[:80] if episode.symptom else "Root cause note"
            )
            digest = _canonical_content(topic, content)
            candidates.append(
                CuratedMemory(
                    memory_id=f"mem-{episode.scenario_type}-{digest}",
                    kind=MemoryKind.PATTERN,
                    scenario_type=episode.scenario_type,
                    topic=topic,
                    content=content,
                    keywords=ConsolidationService._keywords(
                        topic, content, episode
                    ),
                    confidence=0.35,
                    status=MemoryStatus.CANDIDATE,
                    provenance=Provenance(
                        source_type="episode",
                        references=[
                            episode.episode_id,
                            f"run:{episode.run_id}",
                        ],
                        validated_by="pending",
                    ),
                )
            )
        return candidates

    @staticmethod
    def _keywords(
        topic: str,
        content: str,
        episode: EpisodicMemory,
    ) -> list[str]:
        raw = " ".join(
            [
                topic,
                content,
                episode.scenario,
                episode.symptom,
                episode.target_process or "",
                episode.device or "",
                episode.build or "",
            ]
        )
        words = raw.lower().split()
        stopwords = {
            "the", "a", "an", "and", "or", "is", "was", "were",
            "to", "of", "in", "on", "for", "with", "this", "that",
            "by", "as", "at",
        }
        ordered = list(dict.fromkeys(words))
        filtered = [
            word.strip(".,;:。，；：") for word in ordered
            if word.strip(".,;:。，；：") and word not in stopwords
        ]
        return filtered[:12]


def _max_severity(
    left: FindingSeverity | None,
    right: FindingSeverity | None,
) -> FindingSeverity | None:
    if left is None:
        return right
    if right is None:
        return left
    if _SEVERITY_RANK[right.value] > _SEVERITY_RANK[left.value]:
        return right
    return left
