from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from trace_agent.models import FindingSeverity, utc_now
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.models import (
    CORROBORATED_FINDING_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    MemoryConfidence,
    MemoryKind,
    MemoryStatus,
    Provenance,
)
from trace_agent.memory.lifecycle.promoter import commit_gate
from trace_agent.memory.quality_gate import apply_trust_gate
from trace_agent.memory.rerank import max_severity
from trace_agent.memory.text import tokenize

# The evidence vocabulary is shared with ``episode`` and ``rerank``; see
# ``models.CORROBORATED_FINDING_STATUSES`` for why there is only one copy.
_CONFIRMED_STATUSES = CORROBORATED_FINDING_STATUSES

_KEYWORD_LIMIT = 16

#: Section 2.3's ``device/build 事实``. Fixed rather than derived, because there
#: is nothing to derive it from: the run reported these attributes about itself,
#: so the observation is exactly as reliable as the request metadata that carried
#: it - not more, and not less. 0.75 sits above the gate's ``min_confidence``
#: (0.6) on purpose, so what promotes a profile record to ``active`` is a second
#: distinct run observing the same environment rather than a bigger number here.
_PROFILE_CONFIDENCE = 0.75

#: Topic for the environment record. Synthesised rather than taken from the
#: episode, so it stays identical across runs - the id is a hash of topic plus
#: content, and a topic that drifted with the symptom would defeat the merge.
_PROFILE_TOPIC = "Runtime environment profile"

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "is", "was", "were",
        "to", "of", "in", "on", "for", "with", "this", "that",
        "by", "as", "at",
    }
)


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

    Trust is never decided here. Consolidation only accumulates *evidence*
    (distinct run references, confirmed runs, confidence); the quality gate
    turns that evidence into a status. That separation is what makes
    re-consolidating one run a no-op instead of a fake second confirmation.
    """

    def __init__(self, backend: MemoryStore) -> None:
        self._backend = backend

    def consolidate(self, episode: EpisodicMemory) -> list[CuratedMemory]:
        created: list[CuratedMemory] = []
        candidates = self._extract_candidates(episode)

        for candidate in candidates:
            memory_id = self._dedupe(candidate)
            existing = self._backend.load_curated(memory_id)
            if existing is None:
                gated = apply_trust_gate(candidate)
                self._backend.write_curated(gated)
                created.append(gated)
            else:
                created.append(self._reconsolidate(existing, candidate))
        return created

    def _reconsolidate(
        self,
        existing: CuratedMemory,
        incoming: CuratedMemory,
    ) -> CuratedMemory:
        """Merge new evidence, then let the gate - not this service - move trust.

        Two writes with two honest reasons, instead of one write whose version
        row says ``"consolidate"`` while actually changing ``status``. Section 7
        requires a trust transition to be attributable, and a merge that folds
        in a second run is exactly the moment a transition is most likely to be
        legitimate, so it is the one that most needs its own audit entry.
        """
        merged = self._merge(existing, incoming)
        self._backend.write_curated(merged)
        current = self._backend.load_curated(existing.memory_id) or merged
        commit_gate(self._backend, current)
        return self._backend.load_curated(existing.memory_id) or current

    @staticmethod
    def _merge(
        existing: CuratedMemory,
        incoming: CuratedMemory,
    ) -> CuratedMemory:
        """Fold one run's evidence into an existing memory, leaving trust alone.

        Merging is set-union over provenance, so replaying the same run adds no
        new ``run:`` reference and therefore cannot raise ``distinct_runs``.
        Only a genuinely different run moves the needle.
        """
        references = list(
            dict.fromkeys(
                [*existing.provenance.references, *incoming.provenance.references]
            )
        )
        confirmed_runs = list(
            dict.fromkeys([*existing.confirmed_run_ids, *incoming.confirmed_run_ids])
        )
        validated_by = (
            existing.provenance.validated_by or incoming.provenance.validated_by
        )

        return existing.model_copy(
            update={
                "provenance": Provenance(
                    source_type=existing.provenance.source_type,
                    references=references,
                    created_by=existing.provenance.created_by,
                    validated_by=validated_by,
                ),
                "confirmed_run_ids": confirmed_runs,
                "confidence": max(existing.confidence, incoming.confidence),
                "content": incoming.content,
                "topic": incoming.topic,
                "severity": max_severity(existing.severity, incoming.severity),
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

            confirmed = (
                [episode.run_id]
                if episode.run_id
                and str(finding.get("status") or "") in _CONFIRMED_STATUSES
                else []
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
                    confirmed_run_ids=confirmed,
                    provenance=Provenance(
                        source_type="finding",
                        references=references,
                        # A sentinel string here used to read like a pending
                        # expert sign-off. Only a real identity may occupy it,
                        # because the gate treats any value as verification.
                        validated_by=None,
                    ),
                )
            )

        if not candidates and (episode.summary or episode.root_causes):
            candidates.extend(self._extract_episode_candidates(episode))
        # Last and unconditional, and the order matters: the fallback above is
        # guarded on ``not candidates``, so appending here first would suppress
        # the root-cause notes for every run that reported an environment.
        candidates.extend(self._extract_profile_candidates(episode, seen))
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
                    # ``root_causes`` is built only from confirmed/observed
                    # findings, so the run does count as evidence here. The low
                    # confidence still keeps the memory below the gate.
                    confirmed_run_ids=(
                        [episode.run_id] if episode.run_id else []
                    ),
                    provenance=Provenance(
                        source_type="episode",
                        references=[
                            episode.episode_id,
                            f"run:{episode.run_id}",
                        ],
                        validated_by=None,
                    ),
                )
            )
        return candidates

    def _extract_profile_candidates(
        self,
        episode: EpisodicMemory,
        seen: set[str],
    ) -> list[CuratedMemory]:
        """One ``profile`` record for the environment this run reported.

        Section 2.3 asks for ``device/build 事实`` to be remembered, and until
        now nothing produced them: the attributes rode along on the episode and
        on the keyword list, so "have we seen this device before" was only
        answerable by scanning every episode.

        Three properties do the work here.

        *The id is stable on the attribute tuple, not on the episode.* Two runs
        on the same device/build/process therefore land on one record and its
        ``distinct_runs`` accrues, which is the evidence the gate needs; an id
        keyed on ``episode_id`` would mint a fresh single-run record every time
        and the profile would stay ``candidate`` forever.

        *``scenario_type`` is ``None``.* An environment is not a property of one
        scenario, and both adapters treat a null scenario as matching every
        query, so a cold-start run still recalls the device profile recorded by
        a jank run. Pinning it to ``episode.scenario_type`` would silo exactly
        the record whose value is that it is shared.

        *The run counts as confirmed evidence.* Unlike a finding - where
        ``confirmed`` depends on the finding's own status - the run did observe
        its own environment, so there is nothing to hedge about.
        """
        attributes = [
            (label, value.strip())
            for label, value in (
                ("device", episode.device),
                ("build", episode.build),
                ("target_process", episode.target_process),
            )
            if isinstance(value, str) and value.strip()
        ]
        if not attributes:
            return []
        content = "; ".join(f"{label}={value}" for label, value in attributes)
        digest = _canonical_content(_PROFILE_TOPIC, content)
        if digest in seen:
            return []
        seen.add(digest)

        references = [episode.episode_id]
        if episode.run_id:
            references.append(f"run:{episode.run_id}")

        return [
            CuratedMemory(
                memory_id=f"mem-profile-{digest}",
                kind=MemoryKind.PROFILE,
                scenario_type=None,
                topic=_PROFILE_TOPIC,
                content=content,
                # Deliberately *not* :meth:`_keywords`: that folds in the
                # episode's scenario and symptom, which would make a
                # scenario-agnostic record match on symptom text and inject
                # environment metadata into queries that never asked for it.
                keywords=self._keyword_list(f"{_PROFILE_TOPIC} {content}"),
                confidence=_PROFILE_CONFIDENCE,
                status=MemoryStatus.CANDIDATE,
                confirmed_run_ids=[episode.run_id] if episode.run_id else [],
                provenance=Provenance(
                    source_type="episode",
                    references=references,
                    validated_by=None,
                ),
            )
        ]

    @staticmethod
    def _keyword_list(raw: str) -> list[str]:
        """Tokenise ``raw`` into keywords, in first-seen order.

        Tokenize with the same function recall uses. Splitting on whitespace
        left every Chinese symptom as one opaque token, so keywords could never
        match a re-worded query.
        """
        ordered = list(dict.fromkeys(tokenize(raw)))
        filtered = [word for word in ordered if word not in _STOPWORDS]
        return filtered[:_KEYWORD_LIMIT]

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
        return ConsolidationService._keyword_list(raw)
