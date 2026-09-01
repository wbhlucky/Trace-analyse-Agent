from __future__ import annotations

from trace_agent.models import AnalyzeRequest, ScenarioType
from trace_agent.scenarios import scenario_definition


def test_scenario_catalog_is_complete_and_drives_preflight(tmp_path) -> None:
    expected_tools = {
        ScenarioType.COLD_START: "inspect_cold_start_candidates",
        ScenarioType.RESPONSE_LATENCY: "inspect_problem_window_candidates",
        ScenarioType.COMPLETION_LATENCY: (
            "inspect_completion_latency_candidates"
        ),
        ScenarioType.FRAME_JANK: "inspect_problem_window_candidates",
    }

    for scenario_type, expected_tool in expected_tools.items():
        request = AnalyzeRequest(
            trace_id=scenario_type.value,
            trace_path=tmp_path / "trace.htrace",
            scenario_type=scenario_type,
            scenario="scenario",
            symptom="symptom",
            output_dir=tmp_path,
        )
        definition = scenario_definition(scenario_type)

        assert definition.scenario_type is scenario_type
        assert definition.skill_name.endswith("-analysis")
        assert definition.report_title
        assert definition.preflight(request).tool == expected_tool


def test_completion_preflight_parses_explicit_time_range(tmp_path) -> None:
    request = AnalyzeRequest(
        trace_id="completion",
        trace_path=tmp_path / "trace.htrace",
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="scenario",
        symptom="symptom",
        output_dir=tmp_path,
        time_range="788297524337656ns-788298373582031ns",
    )

    invocation = scenario_definition(request.scenario_type).preflight(request)

    assert invocation.arguments["interval_start_ns"] == 788_297_524_337_656
    assert invocation.arguments["interval_end_ns"] == 788_298_373_582_031
