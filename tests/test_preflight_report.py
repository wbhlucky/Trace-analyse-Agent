from __future__ import annotations

from trace_agent.application import DeterministicPreflight, InputGap
from trace_agent.models import AnalyzeRequest, ScenarioType, TraceCapability
from trace_agent.tools import ToolDefinition, ToolRegistry


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry(set(TraceCapability))

    def handler(arguments):
        return {"evidence_id": "ev-1", "data": arguments}

    for name in names:
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                input_schema={},
                required_capabilities=frozenset(),
                handler=handler,
            )
        )
    return registry


def _request(tmp_path, *, scenario_type=ScenarioType.COLD_START):
    return AnalyzeRequest(
        trace_id="preflight",
        trace_path=tmp_path / "trace.htrace",
        scenario_type=scenario_type,
        scenario="scenario",
        symptom="symptom",
        output_dir=tmp_path,
    )


def test_preflight_reports_readiness_with_required_tool(tmp_path) -> None:
    request = _request(tmp_path)
    report = DeterministicPreflight().assess(
        request,
        _registry("get_trace_overview", "inspect_cold_start_candidates"),
    )
    assert report.ready is True
    assert report.tool_available is True
    assert report.tool == "inspect_cold_start_candidates"


def test_preflight_flags_missing_required_tool(tmp_path) -> None:
    report = DeterministicPreflight().assess(
        _request(tmp_path),
        _registry("get_trace_overview"),
    )
    assert report.ready is False
    assert report.tool_available is False
    assert any(gap.severity == "error" for gap in report.gaps)


def test_preflight_gap_defaults_for_missing_target_process(
    tmp_path,
) -> None:
    report = DeterministicPreflight().assess(
        _request(tmp_path),
        _registry("get_trace_overview", "inspect_cold_start_candidates"),
    )
    fields = {gap.field for gap in report.gaps}
    assert "target_process" in fields
    assert all(
        gap.severity != "error" for gap in report.gaps
    )
