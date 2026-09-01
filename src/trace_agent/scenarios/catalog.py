from __future__ import annotations

from dataclasses import dataclass

from trace_agent.time_range import parse_time_range
from trace_agent.models import AnalyzeRequest, ScenarioType


@dataclass(frozen=True, slots=True)
class PreflightInvocation:
    tool: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_type: ScenarioType
    skill_name: str
    report_title: str
    result_field: str | None

    def preflight(self, request: AnalyzeRequest) -> PreflightInvocation:
        if self.scenario_type is ScenarioType.COMPLETION_LATENCY:
            explicit_range = parse_time_range(request.time_range)
            return PreflightInvocation(
                tool="inspect_completion_latency_candidates",
                arguments={
                    "target_ipid": 0,
                    "target_process": request.target_process or "",
                    "operation_marker": request.operation_marker or "",
                    "start_marker": request.start_marker or "",
                    "end_marker": request.end_marker or "",
                    "response_marker": request.response_marker or "",
                    "completion_marker": request.completion_marker or "",
                    "problem_duration_ms": request.problem_duration_ms or 0,
                    "interval_start_ns": (
                        explicit_range.start_ns if explicit_range else 0
                    ),
                    "interval_end_ns": (
                        explicit_range.end_ns if explicit_range else 0
                    ),
                    "lookahead_ms": self._lookahead_ms(request),
                    "max_candidates": 80,
                },
            )
        if self.scenario_type is ScenarioType.COLD_START:
            return PreflightInvocation(
                tool="inspect_cold_start_candidates",
                arguments={
                    "target_process": request.target_process or "",
                    "max_candidates": 20,
                },
            )
        return PreflightInvocation(
            tool="inspect_problem_window_candidates",
            arguments={
                "target_process": request.target_process or "",
                "start_marker": request.start_marker or "",
                "end_marker": (
                    request.end_marker
                    or request.response_marker
                    or request.completion_marker
                    or ""
                ),
                "problem_duration_ms": request.problem_duration_ms or 0,
                "max_candidates": 50,
            },
        )

    @staticmethod
    def _lookahead_ms(request: AnalyzeRequest) -> int:
        if request.problem_duration_ms is None:
            return 5000
        return min(
            max(int(request.problem_duration_ms) + 1000, 5000),
            120_000,
        )


SCENARIOS: dict[ScenarioType, ScenarioDefinition] = {
    ScenarioType.COLD_START: ScenarioDefinition(
        scenario_type=ScenarioType.COLD_START,
        skill_name="cold-start-analysis",
        report_title="冷启动性能分析",
        result_field="cold_start",
    ),
    ScenarioType.RESPONSE_LATENCY: ScenarioDefinition(
        scenario_type=ScenarioType.RESPONSE_LATENCY,
        skill_name="response-latency-analysis",
        report_title="响应时延分析",
        result_field=None,
    ),
    ScenarioType.COMPLETION_LATENCY: ScenarioDefinition(
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        skill_name="completion-latency-analysis",
        report_title="完成时延分析",
        result_field="completion_latency",
    ),
    ScenarioType.FRAME_JANK: ScenarioDefinition(
        scenario_type=ScenarioType.FRAME_JANK,
        skill_name="frame-jank-analysis",
        report_title="帧率与丢帧分析",
        result_field=None,
    ),
}


def scenario_definition(scenario_type: ScenarioType) -> ScenarioDefinition:
    return SCENARIOS[scenario_type]
