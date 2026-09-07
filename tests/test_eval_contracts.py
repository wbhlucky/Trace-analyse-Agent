from __future__ import annotations

import asyncio
from pathlib import Path

from trace_agent.eval import (
    CaseLoadError,
    DeterministicGrader,
    EvalHarness,
    Gold,
    load_case,
    save_run,
    summarize,
)
from trace_agent.eval.models import (
    AgentOutput,
    BottleneckGold,
    EvidenceGold,
    MetricGold,
    RootCauseGold,
    TrialStatus,
)
from trace_agent.models import (
    AnalysisResult,
    Finding,
    RunResult,
)


def _result(*, duration_ms: float = 1250.0) -> AnalysisResult:
    finding = Finding(
        title="vsync_distribution Root Cause",
        severity="medium",
        status="confirmed",
        confidence=0.85,
        evidence_ids=["ev-1"],
        analysis=(
            "RenderService SendVsyncTo distribution caused startup "
            "latency increase"
        ),
        recommendation="isolate RenderService",
        verification="verified via schedule slices",
    )
    return AnalysisResult(
        summary="Trace analyzed for cold start latency",
        findings=[finding],
        limitations=[
            "不足以证明 Flutter 业务代码本身是主要根因"
        ],
    )


def _output(duration_ms: float = 1250.0) -> AgentOutput:
    return AgentOutput(
        result=_result(duration_ms=duration_ms),
        raw={"tool_calls": ["get_trace_overview", "analyze_vsync"]},
    )


def _gold() -> Gold:
    return Gold(
        root_causes=[
            RootCauseGold(id="RC001", type="vsync_distribution",
                          component="RenderService")
        ],
        bottlenecks=[
            BottleneckGold(type="vsync", component="RenderService")
        ],
        evidence=[
            EvidenceGold(event="SendVsyncTo", thread="RenderService")
        ],
        metrics={"startup_duration_ms": MetricGold(value=1250, tolerance=20)},
        uncertainty=["不足以证明 Flutter 业务代码本身是主要根因"],
    )


def test_grader_scores_all_components() -> None:
    grade = DeterministicGrader().grade(
        "cold_start_001", "trial-1", _gold(), _output()
    )
    assert grade.overall >= 0.0
    assert len(grade.components) == 6
    names = {item.component for item in grade.components}
    assert names == {
        "diagnosis",
        "evidence",
        "metrics",
        "reasoning",
        "uncertainty",
        "report",
    }
    assert all(0.0 <= item.score <= 1.0 for item in grade.components)


def test_grader_rejects_bad_weights() -> None:
    try:
        DeterministicGrader(weights={"bogus": 0.5})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown component should raise")


def test_load_case_discovers_golden_case() -> None:
    case = load_case(
        Path("evals/cases/cold_start_001/case.yaml")
    )
    assert case.id == "cold_start_001"
    assert case.metadata.suite == "regression"
    assert len(case.gold.root_causes) == 1


def test_load_case_missing_raises() -> None:
    try:
        load_case(Path("missing/case.yaml"))
    except CaseLoadError:
        pass
    else:
        raise AssertionError("missing case should raise")


class _FakeApp:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    async def run(self, request):
        return RunResult(
            run_id="run-1",
            output_dir=request.output_dir,
            report_path=request.output_dir / "report.html",
            analysis=_result(),
        )


def test_harness_runs_case_and_persists(tmp_path: Path) -> None:
    case = load_case(Path("evals/cases/cold_start_001/case.yaml"))
    harness = EvalHarness(
        application_factory=lambda: _FakeApp(tmp_path),
        output_root=tmp_path,
    )
    run = asyncio.run(harness.run([case]))
    assert run.case_scores["cold_start_001"] >= 0.0
    assert len(run.trials) == 1
    assert run.trials[0].status is TrialStatus.COMPLETED

    saved = save_run(run, tmp_path / "run.json")
    assert saved.is_file()


def test_summary_aggregates_case_and_suite(tmp_path: Path) -> None:
    case = load_case(Path("evals/cases/cold_start_001/case.yaml"))
    harness = EvalHarness(
        application_factory=lambda: _FakeApp(tmp_path),
        output_root=tmp_path,
    )
    run = asyncio.run(harness.run([case]))
    summary = summarize(run)
    assert summary["total_cases"] == 1
    assert summary["suite_scores"]["regression"] >= 0.0
    assert summary["overall_score"] >= 0.0
