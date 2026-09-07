"""Tests for trial failure taxonomy and harness trajectory/operational wiring.

These cover the Multi-Trial V1 hardening added after the v1 grader review:

* agent vs infra failure classification,
* trajectory grading wired into ``EvalHarness`` (still independent of outcome),
* operational tokens / ttft extraction from persisted LLM telemetry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from trace_agent.errors import (
    AgentFailure,
    AgentRetryExhausted,
    ErrorCategory,
    RunInterrupted,
)
from trace_agent.eval import TrialFailure
from trace_agent.eval.harness import EvalHarness
from trace_agent.eval.runner import TrialRunner
from trace_agent.models import RunResult


def test_runner_classifies_environment_errors_as_infra() -> None:
    assert (
        TrialRunner._classify_error(FileNotFoundError("missing trace"))
        is TrialFailure.INFRA_FAILURE
    )
    assert (
        TrialRunner._classify_error(PermissionError("denied"))
        is TrialFailure.INFRA_FAILURE
    )
    assert (
        TrialRunner._classify_error(ModuleNotFoundError("no module"))
        is TrialFailure.INFRA_FAILURE
    )


def test_runner_classifies_agent_boundary_failures_as_agent() -> None:
    assert (
        TrialRunner._classify_error(
            AgentFailure("no answer", category=ErrorCategory.FATAL)
        )
        is TrialFailure.AGENT_FAILURE
    )
    assert (
        TrialRunner._classify_error(
            AgentRetryExhausted("exhausted", category=ErrorCategory.RETRYABLE)
        )
        is TrialFailure.AGENT_FAILURE
    )
    assert (
        TrialRunner._classify_error(RunInterrupted(run_id="run-1"))
        is TrialFailure.AGENT_FAILURE
    )


def test_runner_classifies_invalid_inputs_as_invalid() -> None:
    assert (
        TrialRunner._classify_error(ValueError("bad schema"))
        is TrialFailure.INVALID
    )
    assert (
        TrialRunner._classify_error(KeyError("missing"))
        is TrialFailure.INVALID
    )
    assert (
        TrialRunner._classify_error(TypeError("wrong type"))
        is TrialFailure.INVALID
    )


def test_runner_classifies_unexpected_errors_as_eval() -> None:
    assert (
        TrialRunner._classify_error(RuntimeError("grader bug"))
        is TrialFailure.EVAL_FAILURE
    )
    assert (
        TrialRunner._classify_error(AssertionError("harness bug"))
        is TrialFailure.EVAL_FAILURE
    )


def test_extract_llm_metrics_reads_ttft_and_tokens() -> None:
    raw = {
        "status": "completed",
        "llm_metrics": {
            "ttft_ms": 321.5,
            "total_tokens": 1200,
        },
    }
    metrics = EvalHarness._extract_llm_metrics(raw)
    assert metrics["ttft_ms"] == 321.5
    assert metrics["tokens"] == 1200


def test_extract_llm_metrics_falls_back_to_usage() -> None:
    raw = {
        "llm_metrics": {
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }
    metrics = EvalHarness._extract_llm_metrics(raw)
    assert metrics["tokens"] == 100


def test_extract_llm_metrics_handles_missing_fields() -> None:
    assert EvalHarness._extract_llm_metrics({}) == {}
    assert EvalHarness._extract_llm_metrics({"llm_metrics": None}) == {}


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

    @staticmethod
    def _write_agent_result(request) -> None:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        import json

        (request.output_dir / "agent-result.json").write_text(
            json.dumps(
                {
                    "llm_metrics": {"ttft_ms": 100.0, "total_tokens": 42},
                }
            ),
            encoding="utf-8",
        )


def _result():
    from trace_agent.models import AnalysisResult

    return AnalysisResult(
        summary="cold start",
        findings=[],
        limitations=[],
    )


def test_harness_wires_trajectory_and_operational(tmp_path: Path) -> None:
    from trace_agent.eval import load_case

    case = load_case(Path("evals/cases/cold_start_001/case.yaml"))

    class App:
        async def run(self, request):
            _FakeApp._write_agent_result(request)
            return RunResult(
                run_id="run-1",
                output_dir=request.output_dir,
                report_path=request.output_dir / "report.html",
                analysis=_result(),
            )

    harness = EvalHarness(
        application_factory=lambda: App(),
        output_root=tmp_path,
    )
    run = asyncio.run(harness.run([case]))

    assert len(run.trials) == 1
    trial = run.trials[0]
    assert trial.failure is TrialFailure.SUCCESS

    multi = run.multi_trial_results["cold_start_001"]
    assert len(multi.trial_results) == 1
    result = multi.trial_results[0]
    assert result.failure is TrialFailure.SUCCESS
    assert result.trajectory is not None
    assert result.trajectory.case_id == "cold_start_001"
    assert result.operational is not None
    assert result.operational.tokens == 42
    assert result.operational.ttft_ms == 100.0
