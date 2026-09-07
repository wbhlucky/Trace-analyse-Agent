"""Tests for Multi-Trial V1 aggregation (outcome gates + pass@k/pass^k)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from trace_agent.eval import (
    EvalHarness,
    GateThresholds,
    MultiTrialAggregator,
    TrialOutcome,
    TrialResult,
    score_trial_outcome,
)
from trace_agent.eval.models import (
    ComponentScore,
    GraderResult,
    OperationalMetrics,
    TrajectoryResult,
)


def _grade(
    *,
    case_id: str = "case",
    trial_id: str = "trial",
    overall: float,
    diagnosis: float = 1.0,
    evidence: float = 1.0,
    metrics: float = 1.0,
) -> GraderResult:
    return GraderResult(
        case_id=case_id,
        trial_id=trial_id,
        overall=overall,
        components=[
            ComponentScore(component="diagnosis", score=diagnosis, weight=0.35),
            ComponentScore(component="evidence", score=evidence, weight=0.25),
            ComponentScore(component="metrics", score=metrics, weight=0.15),
            ComponentScore(component="reasoning", score=1.0, weight=0.10),
            ComponentScore(component="uncertainty", score=1.0, weight=0.05),
            ComponentScore(component="report", score=1.0, weight=0.10),
        ],
    )


def _thresholds(**kwargs) -> GateThresholds:
    defaults = dict(diagnosis=0.5, evidence=0.5, metrics=0.5, overall=0.75)
    defaults.update(kwargs)
    return GateThresholds(**defaults)


def test_outcome_passes_when_all_gates_met() -> None:
    outcome = score_trial_outcome(
        "case",
        "trial",
        _grade(overall=0.9),
        _thresholds(),
    )
    assert outcome.passed is True
    assert outcome.graded_score == 0.9
    assert len(outcome.gates) == 4
    assert all(gate.passed for gate in outcome.gates)


def test_outcome_fails_on_component_gate_but_preserves_score() -> None:
    outcome = score_trial_outcome(
        "case",
        "trial",
        _grade(overall=0.9, diagnosis=0.2),
        _thresholds(),
    )
    assert outcome.passed is False
    assert outcome.graded_score == 0.9
    failed = [gate.component for gate in outcome.gates if not gate.passed]
    assert "diagnosis" in failed


def test_outcome_fails_on_overall_gate() -> None:
    outcome = score_trial_outcome(
        "case",
        "trial",
        _grade(overall=0.6),
        _thresholds(),
    )
    assert outcome.passed is False
    assert any(
        gate.component == "overall" and not gate.passed
        for gate in outcome.gates
    )


def _trial(
    *,
    index: int,
    passed: bool,
    score: float,
    trajectory_score: float | None = None,
) -> TrialResult:
    trial_id = f"trial-{index}"
    outcome = TrialOutcome(
        case_id="case",
        trial_id=trial_id,
        graded_score=score,
        passed=passed,
    )
    trajectory = None
    if trajectory_score is not None:
        trajectory = TrajectoryResult(
            case_id="case",
            trial_id=trial_id,
            score=trajectory_score,
            tool_calls=0,
            tool_errors=0,
            turns=0,
            invalid_actions=0,
            evidence_retrievals=0,
        )
    return TrialResult(
        case_id="case",
        trial_id=trial_id,
        index=index,
        outcome=outcome,
        trajectory=trajectory,
        operational=OperationalMetrics(
            case_id="case",
            trial_id=trial_id,
            turns=1,
            tool_calls=2,
            tool_errors=0,
            latency_ms=10.0,
        ),
    )


def test_aggregator_pass_at_k_any_pass() -> None:
    results = [
        _trial(index=0, passed=False, score=0.4),
        _trial(index=1, passed=True, score=0.8),
        _trial(index=2, passed=False, score=0.5),
    ]
    aggregated = MultiTrialAggregator(k=3).aggregate("case", results)
    assert aggregated.k == 3
    assert aggregated.pass_count == 1
    assert aggregated.pass_at_1 == 0.0
    assert aggregated.pass_at_k == 1.0
    assert aggregated.pass_power_k == 0.0


def test_aggregator_pass_power_k_requires_all() -> None:
    results = [
        _trial(index=0, passed=True, score=0.8),
        _trial(index=1, passed=True, score=0.9),
        _trial(index=2, passed=True, score=0.85),
    ]
    aggregated = MultiTrialAggregator(k=3).aggregate("case", results)
    assert aggregated.pass_count == 3
    assert aggregated.pass_at_1 == 1.0
    assert aggregated.pass_at_k == 1.0
    assert aggregated.pass_power_k == 1.0


def test_aggregator_all_fail() -> None:
    results = [
        _trial(index=0, passed=False, score=0.4),
        _trial(index=1, passed=False, score=0.5),
    ]
    aggregated = MultiTrialAggregator(k=2).aggregate("case", results)
    assert aggregated.pass_count == 0
    assert aggregated.pass_at_1 == 0.0
    assert aggregated.pass_at_k == 0.0
    assert aggregated.pass_power_k == 0.0


def test_aggregator_mean_and_variance() -> None:
    results = [
        _trial(index=0, passed=True, score=0.6),
        _trial(index=1, passed=True, score=0.8),
    ]
    aggregated = MultiTrialAggregator(k=2).aggregate("case", results)
    assert aggregated.scores == [0.6, 0.8]
    assert aggregated.mean_score == round((0.6 + 0.8) / 2, 4)
    assert aggregated.score_variance == round(((0.6 - 0.7) ** 2 + (0.8 - 0.7) ** 2) / 2, 4)


def test_aggregator_trajectory_pass_rate_independent() -> None:
    results = [
        _trial(index=0, passed=True, score=0.8, trajectory_score=0.9),
        _trial(index=1, passed=False, score=0.4, trajectory_score=0.3),
        _trial(index=2, passed=True, score=0.9, trajectory_score=None),
    ]
    aggregated = MultiTrialAggregator(k=3).aggregate("case", results)
    assert aggregated.trajectory_pass_rate == 0.5


def test_aggregator_rejects_zero_k() -> None:
    try:
        MultiTrialAggregator(k=0)
    except ValueError:
        pass
    else:
        raise AssertionError("k=0 should raise")


def test_harness_produces_multi_trial_results(tmp_path: Path) -> None:
    from trace_agent.eval import load_case
    from trace_agent.models import AnalysisResult, Finding, RunResult

    def _result() -> AnalysisResult:
        finding = Finding(
            title="vsync_distribution Root Cause",
            severity="medium",
            status="confirmed",
            confidence=0.85,
            evidence_ids=["ev-1"],
            analysis="RenderService SendVsyncTo distribution caused latency",
            recommendation="isolate RenderService",
            verification="verified via schedule slices",
        )
        return AnalysisResult(
            summary="Trace analyzed for cold start latency",
            findings=[finding],
            limitations=["not enough evidence"],
        )

    class _FakeApp:
        async def run(self, request):
            return RunResult(
                run_id="run-1",
                output_dir=request.output_dir,
                report_path=request.output_dir / "report.html",
                analysis=_result(),
            )

    case = load_case(Path("evals/cases/cold_start_001/case.yaml"))
    harness = EvalHarness(
        application_factory=lambda: _FakeApp(),
        output_root=tmp_path,
        trials=3,
    )
    run = asyncio.run(harness.run([case]))
    assert case.id in run.multi_trial_results
    aggregated = run.multi_trial_results[case.id]
    assert aggregated.k == 3
    assert len(aggregated.trial_results) == 3
    assert len(aggregated.scores) == 3
    assert 0.0 <= aggregated.pass_at_k <= 1.0
    assert 0.0 <= aggregated.pass_power_k <= 1.0
