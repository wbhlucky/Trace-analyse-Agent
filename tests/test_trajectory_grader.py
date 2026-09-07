"""Tests for the deterministic TrajectoryGrader V1 layer."""

from __future__ import annotations

from trace_agent.eval.trajectory import TrajectoryConfig, TrajectoryGrader


def _call(tool, *, arguments=None, status="success", evidence_id=None, error=None):
    return {
        "tool": tool,
        "arguments": arguments if arguments is not None else {},
        "status": status,
        "evidence_id": evidence_id,
        "error": error,
    }


def _config(**kwargs) -> TrajectoryConfig:
    defaults = dict(
        max_tool_errors=1,
        max_turns=20,
        max_tool_calls=20,
        max_repeated_calls=0,
        min_evidence_retrievals=1,
        forbidden_tools=[],
        required_evidence_tools=[],
        known_tools=[],
    )
    defaults.update(kwargs)
    return TrajectoryConfig(**defaults)


def _graded(tool_calls, config=None, turns=None):
    grader = TrajectoryGrader(config or _config())
    return grader.grade("case", "trial", tool_calls, turns=turns)


def test_clean_trace_scores_full() -> None:
    calls = [
        _call("get_trace_overview", evidence_id="ev-1"),
        _call("inspect_cold_start_candidates", arguments={"max_candidates": 20}, evidence_id="ev-2"),
        _call("inspect_cold_start_timeline", evidence_id="ev-3"),
    ]
    result = _graded(calls)
    assert result.score == 1.0
    assert result.tool_errors == 0
    assert result.repeated_calls == 0
    assert result.forbidden_calls == 0


def test_tool_errors_are_penalized() -> None:
    calls = [
        _call("query_trace_sql", status="error", error="bad sql"),
        _call("query_trace_sql", status="error", error="bad sql"),
        _call("get_trace_overview", evidence_id="ev-1"),
    ]
    result = _graded(calls, _config(max_tool_errors=1))
    assert result.tool_errors == 2
    assert result.score < 1.0
    codes = {check.code for check in result.checks if not check.passed}
    assert "tool.errors" in codes


def test_repeated_calls_are_penalized() -> None:
    args = {"sql": "select 1"}
    calls = [
        _call("query_trace_sql", arguments=args, evidence_id="ev-1"),
        _call("query_trace_sql", arguments=args, evidence_id="ev-2"),
        _call("get_trace_overview", evidence_id="ev-3"),
    ]
    result = _graded(calls, _config(max_repeated_calls=0))
    assert result.repeated_calls == 1
    assert result.score < 1.0


def test_different_args_are_not_repeated() -> None:
    calls = [
        _call("query_trace_sql", arguments={"sql": "a"}, evidence_id="ev-1"),
        _call("query_trace_sql", arguments={"sql": "b"}, evidence_id="ev-2"),
    ]
    result = _graded(calls)
    assert result.repeated_calls == 0


def test_budget_exceeded_is_penalized() -> None:
    calls = [_call("query_trace_sql", evidence_id=f"ev-{i}") for i in range(25)]
    result = _graded(calls, _config(max_tool_calls=20))
    assert result.tool_calls == 25
    codes = {check.code for check in result.checks if not check.passed}
    assert "trajectory.budget" in codes


def test_forbidden_tool_is_penalized() -> None:
    calls = [
        _call("get_trace_overview", evidence_id="ev-1"),
        _call("modify_trace", evidence_id="ev-2"),
    ]
    result = _graded(calls, _config(forbidden_tools=["modify_trace"]))
    assert result.forbidden_calls == 1
    assert any(check.code == "trajectory.actions" and not check.passed for check in result.checks)


def test_required_evidence_tools_missing() -> None:
    calls = [
        _call("get_trace_overview", evidence_id="ev-1"),
    ]
    result = _graded(
        calls,
        _config(required_evidence_tools=["inspect_cold_start_timeline"]),
    )
    assert result.score < 1.0
    assert any(
        check.code == "trajectory.required_evidence" and not check.passed
        for check in result.checks
    )


def test_evidence_check_does_not_require_fixed_order() -> None:
    # Different call order but same required evidence tools still passes:
    # the grader must not prescribe a fixed tool path.
    required = ["get_trace_overview", "inspect_cold_start_candidates"]
    calls_a = [
        _call("get_trace_overview", evidence_id="ev-1"),
        _call("inspect_cold_start_candidates", evidence_id="ev-2"),
    ]
    calls_b = [
        _call("inspect_cold_start_candidates", evidence_id="ev-2"),
        _call("get_trace_overview", evidence_id="ev-1"),
    ]
    assert _graded(calls_a, _config(required_evidence_tools=required)).score == 1.0
    assert _graded(calls_b, _config(required_evidence_tools=required)).score == 1.0


def test_evidence_check_uses_any_evidence_when_no_required_tools() -> None:
    calls = [
        _call("query_trace_sql", evidence_id="ev-1"),
    ]
    result = _graded(calls, _config(min_evidence_retrievals=1))
    assert result.score == 1.0
    assert result.evidence_retrievals == 1