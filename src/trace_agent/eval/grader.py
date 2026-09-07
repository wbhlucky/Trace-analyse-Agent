from __future__ import annotations

from typing import Any

from trace_agent.eval.models import (
    AgentOutput,
    ComponentScore,
    Gold,
    GraderResult,
)
from trace_agent.models import AnalysisResult


DEFAULT_WEIGHTS: dict[str, float] = {
    "diagnosis": 0.35,
    "evidence": 0.25,
    "metrics": 0.15,
    "reasoning": 0.10,
    "uncertainty": 0.05,
    "report": 0.10,
}

class DeterministicGrader:
    """Deterministic outcome-focused grader with explicit partial credit.

    It scores evidence and quality rather than requiring a specific tool-call
    sequence.  Tool calls are recorded only, never used as the primary signal.
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
    ) -> None:
        resolved = dict(DEFAULT_WEIGHTS)
        if weights:
            unknown = set(weights) - set(resolved)
            if unknown:
                raise ValueError(f"unknown grader components: {sorted(unknown)}")
            resolved.update(weights)
        total = sum(resolved.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError("grader weights must sum to 1.0")
        self._weights = resolved

    def grade(
        self,
        case_id: str,
        trial_id: str,
        gold: Gold,
        output: AgentOutput,
    ) -> GraderResult:
        result = output.result
        components = [
            self._diagnosis(gold, result),
            self._evidence(gold, result),
            self._metrics(gold, result),
            self._reasoning(result),
            self._uncertainty(gold, result),
            self._report(result),
        ]
        overall = round(
            sum(
                item.score * self._weights[item.component]
                for item in components
            ),
            4,
        )
        return GraderResult(
            case_id=case_id,
            trial_id=trial_id,
            overall=overall,
            components=components,
        )

    def _component(
        self,
        name: str,
        score: float,
        notes: list[str] | None = None,
    ) -> ComponentScore:
        return ComponentScore(
            component=name,
            score=max(0.0, min(1.0, round(score, 4))),
            weight=self._weights[name],
            notes=list(notes or []),
        )

    def _diagnosis(
        self,
        gold: Gold,
        result: AnalysisResult,
    ) -> ComponentScore:
        notes: list[str] = []
        score = 0.0

        found_causes: set[str] = set()
        for cause in gold.root_causes:
            if _labels_intersect(cause.match_terms, result):
                found_causes.add(cause.key)
        if gold.root_causes:
            score = len(found_causes) / len(gold.root_causes)
            for cause in gold.root_causes:
                if cause.key not in found_causes:
                    notes.append(f"missing root cause {cause.key!r}")

        if gold.bottlenecks:
            bottleneck_score = 0.0
            for bottleneck in gold.bottlenecks:
                if _labels_intersect(bottleneck.match_terms, result):
                    bottleneck_score += 1.0 / len(gold.bottlenecks)
            score = (score + bottleneck_score) / 2

        forbidden_hits = _match_forbidden(gold.forbidden_conclusions, result)
        if forbidden_hits:
            score = 0.0
            notes.append(
                "forbidden conclusion: " + ", ".join(forbidden_hits)
            )

        return self._component("diagnosis", score, notes)

    def _evidence(
        self,
        gold: Gold,
        result: AnalysisResult,
    ) -> ComponentScore:
        notes: list[str] = []
        if not gold.evidence:
            return self._component("evidence", 1.0)

        haystack = _searchable_result(result)
        matched = 0
        for expected in gold.evidence:
            if expected.subject and expected.subject in haystack:
                matched += 1
            else:
                notes.append(f"missing evidence {expected.subject!r}")
        return self._component(
            "evidence",
            matched / len(gold.evidence),
            notes,
        )

    def _metrics(
        self,
        gold: Gold,
        result: AnalysisResult,
    ) -> ComponentScore:
        notes: list[str] = []
        if not gold.metrics:
            return self._component("metrics", 1.0)

        values = _metric_values(result)
        matched = 0
        for name, expected in gold.metrics.items():
            actual = values.get(name)
            if isinstance(actual, (int, float)):
                if abs(actual - expected.value) <= expected.effective_tolerance:
                    matched += 1
                else:
                    notes.append(
                        f"metric {name}={actual} outside "
                        f"{expected.value}+/-{expected.effective_tolerance}"
                    )
            else:
                notes.append(f"metric {name!r} missing or non-numeric")
        return self._component(
            "metrics",
            matched / len(gold.metrics),
            notes,
        )

    def _reasoning(self, result: AnalysisResult) -> ComponentScore:
        # Reasoning quality is proxied deterministically by non-empty
        # structural reasoning fields; LLM judgment is deliberately deferred.
        findings = result.findings
        if not findings:
            return self._component(
                "reasoning", 0.0, ["no findings to reason over"]
            )
        reasoned = sum(
            1
            for finding in findings
            if bool(finding.analysis) and bool(finding.recommendation)
        )
        return self._component(
            "reasoning", reasoned / len(findings)
        )

    def _uncertainty(
        self,
        gold: Gold,
        result: AnalysisResult,
    ) -> ComponentScore:
        notes: list[str] = []
        if gold.uncertainty_topics:
            haystack = _limitations_text(result)
            matched = 0
            for topic in gold.uncertainty_topics:
                if any(
                    term and term.lower() in haystack
                    for term in topic.match_terms
                ):
                    matched += 1
                else:
                    notes.append(f"missing uncertainty topic {topic.key!r}")
            return self._component(
                "uncertainty",
                matched / len(gold.uncertainty_topics),
                notes,
            )

        if not gold.uncertainty:
            return self._component("uncertainty", 1.0)

        haystack = _limitations_text(result)
        matched = 0
        for expected in gold.uncertainty:
            if expected.lower() in haystack:
                matched += 1
            else:
                notes.append(f"missing uncertainty {expected!r}")
        return self._component(
            "uncertainty",
            matched / len(gold.uncertainty),
            notes,
        )

    def _report(self, result: AnalysisResult) -> ComponentScore:
        has_summary = bool(result.summary.strip())
        has_findings = bool(result.findings)
        score = (1.0 if has_summary else 0.0)
        if not has_findings:
            score *= 0.5
        return self._component("report", score)

def _searchable_result(result: AnalysisResult) -> str:
    parts = [result.summary]
    if result.cold_start is not None:
        parts.append(result.cold_start.classification_reason)
        parts.append(result.cold_start.critical_path_summary)
        parts.extend(stage.assessment for stage in result.cold_start.stages)
    if result.completion_latency is not None:
        parts.append(result.completion_latency.completion_semantics)
        parts.append(result.completion_latency.critical_path_summary)
        parts.extend(phase.assessment for phase in result.completion_latency.phases)
    parts.extend(
        f"{finding.title} {finding.analysis}"
        for finding in result.findings
    )
    return " ".join(parts).lower()


def _labels_intersect(labels: list[str], result: AnalysisResult) -> bool:
    haystack = _searchable_result(result)
    for label in labels:
        if label and label.lower() in haystack:
            return True
    return False


def _match_forbidden(
    forbidden: list[str],
    result: AnalysisResult,
) -> list[str]:
    haystack = _searchable_result(result)
    return [
        item for item in forbidden
        if item and item.lower() in haystack
    ]


def _limitations_text(result: AnalysisResult) -> str:
    return " ".join(result.limitations).lower()


def _metric_values(result: AnalysisResult) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if result.cold_start is not None:
        values["startup_duration_ms"] = result.cold_start.total_duration_ms
        if result.cold_start.presentation_duration_ms is not None:
            values["presentation_duration_ms"] = (
                result.cold_start.presentation_duration_ms
            )
    if result.completion_latency is not None:
        if result.completion_latency.response_latency_ms is not None:
            values["response_latency_ms"] = (
                result.completion_latency.response_latency_ms
            )
        if result.completion_latency.completion_latency_ms is not None:
            values["completion_latency_ms"] = (
                result.completion_latency.completion_latency_ms
            )
    return values
