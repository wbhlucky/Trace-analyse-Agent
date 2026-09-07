"""Multi-trial aggregation for a single eval case.

A trial is one isolated run of one case.  ``MultiTrialAggregator`` consumes the
per-trial observations (outcome gates, trajectory quality, operational cost)
and produces a :class:`MultiTrialResult` with:

* ``pass_count`` / ``pass_at_k`` / ``pass_power_k`` semantics
* outcome ``mean_score`` and ``score_variance``
* independent ``trajectory_pass_rate`` and operational aggregates

Trajectory data is intentionally an input here, not produced by the harness;
that keeps process/quality signals separate from outcome correctness.
"""

from __future__ import annotations

import statistics
from typing import Iterable

from trace_agent.eval.models import (
    ComponentScore,
    GateCheck,
    GateThresholds,
    GraderResult,
    MultiTrialResult,
    OperationalMetrics,
    TrialOutcome,
    TrialResult,
    TrialStatus,
)


def _component_map(grade: GraderResult) -> dict[str, ComponentScore]:
    return {item.component: item for item in grade.components}


def score_trial_outcome(
    case_id: str,
    trial_id: str,
    grade: GraderResult | None,
    thresholds: GateThresholds | None = None,
) -> TrialOutcome:
    """Convert a weighted grade into an outcome with deterministic hard gates.

    A trial passes only when every configured gate passes.  The aggregate
    graded score is preserved verbatim; gates can downgrade ``passed`` but
    never modify ``graded_score``.
    """
    thresholds = thresholds or GateThresholds()
    if grade is None:
        return TrialOutcome(
            case_id=case_id,
            trial_id=trial_id,
            graded_score=0.0,
            passed=False,
            gates=[
                GateCheck(
                    component="grading",
                    passed=False,
                    score=0.0,
                    minimum=thresholds.overall,
                    message="no grade produced for trial",
                )
            ],
        )

    components = _component_map(grade)
    gates: list[GateCheck] = []
    for component in ("diagnosis", "evidence", "metrics"):
        score = components.get(component, ComponentScore(
            component=component, score=0.0, weight=0.0
        )).score
        minimum = getattr(thresholds, component)
        gates.append(
            GateCheck(
                component=component,
                passed=score >= minimum,
                score=score,
                minimum=minimum,
                message=(
                    ""
                    if score >= minimum
                    else f"{component}={score} below {minimum}"
                ),
            )
        )

    gates.append(
        GateCheck(
            component="overall",
            passed=grade.overall >= thresholds.overall,
            score=grade.overall,
            minimum=thresholds.overall,
            message=(
                ""
                if grade.overall >= thresholds.overall
                else f"overall={grade.overall} below {thresholds.overall}"
            ),
        )
    )

    return TrialOutcome(
        case_id=case_id,
        trial_id=trial_id,
        graded_score=grade.overall,
        passed=all(gate.passed for gate in gates),
        gates=gates,
    )


class MultiTrialAggregator:
    """Aggregates ``k`` isolated trial results into one case observation.

    Scores are the outcome graded scores, and a trial is a pass only when its
    outcome gates all pass.  Trajectory pass rate is computed over trials that
    have a trajectory result, independent of outcome correctness.
    """

    def __init__(
        self,
        thresholds: GateThresholds | None = None,
        k: int = 3,
    ) -> None:
        if k < 1:
            raise ValueError("k must be >= 1")
        self._thresholds = thresholds or GateThresholds()
        self._k = k

    @property
    def k(self) -> int:
        return self._k

    def trial_outcome(
        self,
        grade: GraderResult | None,
        *,
        case_id: str,
        trial_id: str,
    ) -> TrialOutcome:
        return score_trial_outcome(
            case_id,
            trial_id,
            grade,
            self._thresholds,
        )

    def aggregate(
        self,
        case_id: str,
        trial_results: Iterable[TrialResult],
    ) -> MultiTrialResult:
        """Aggregate trial observations into a :class:`MultiTrialResult`."""

        results = list(trial_results)
        scores: list[float] = []
        pass_count = 0
        trajectory_passes: list[bool] = []
        operational = _operational(case_id, results)

        for result in results:
            if (
                result.outcome is not None
                and result.status is TrialStatus.COMPLETED
            ):
                scores.append(result.outcome.graded_score)
                if result.outcome.passed:
                    pass_count += 1
            if result.trajectory is not None:
                trajectory_passes.append(
                    result.trajectory.score >= self._thresholds.overall
                )

        k = max(1, len(results))
        passes = [1 if r.outcome and r.outcome.passed else 0 for r in results]
        pass_at_1 = 1.0 if passes and passes[0] == 1 else 0.0
        pass_at_k = 1.0 if any(passes) else 0.0
        pass_power_k = 1.0 if passes and all(passes) else 0.0
        mean_score = round(statistics.fmean(scores), 4) if scores else 0.0
        score_variance = round(statistics.pvariance(scores), 4) if scores else 0.0
        trajectory_pass_rate = (
            round(sum(trajectory_passes) / len(trajectory_passes), 4)
            if trajectory_passes
            else None
        )

        return MultiTrialResult(
            case_id=case_id,
            k=k,
            trial_results=results,
            scores=scores,
            pass_count=pass_count,
            pass_at_1=pass_at_1,
            pass_at_k=pass_at_k,
            pass_power_k=pass_power_k,
            mean_score=mean_score,
            score_variance=score_variance,
            trajectory_pass_rate=trajectory_pass_rate,
            operational=operational,
        )


def _operational(
    case_id: str,
    results: list[TrialResult],
) -> OperationalMetrics | None:
    metrics = [
        result.operational
        for result in results
        if result.operational is not None
    ]
    if not metrics:
        return None
    latencies = [m.latency_ms for m in metrics if m.latency_ms is not None]
    tokens = [m.tokens for m in metrics if m.tokens is not None]
    costs = [m.cost for m in metrics if m.cost is not None]
    ttfts = [m.ttft_ms for m in metrics if m.ttft_ms is not None]
    return OperationalMetrics(
        case_id=case_id,
        trial_id="aggregate",
        turns=sum(m.turns for m in metrics),
        tool_calls=sum(m.tool_calls for m in metrics),
        tool_errors=sum(m.tool_errors for m in metrics),
        tokens=round(statistics.fmean(tokens), 1) if tokens else None,
        cost=round(statistics.fmean(costs), 6) if costs else None,
        ttft_ms=round(statistics.fmean(ttfts), 3) if ttfts else None,
        latency_ms=round(statistics.fmean(latencies), 3) if latencies else None,
    )
