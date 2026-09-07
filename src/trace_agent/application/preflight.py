from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from trace_agent.models import AnalyzeRequest, ScenarioType
from trace_agent.scenarios import scenario_definition
from trace_agent.tools import ToolRegistry


class InputGap(BaseModel):
    """A deterministic input-completeness gap found before orchestration."""

    model_config = ConfigDict(extra="forbid")

    field: str
    severity: str = Field(default="warning")
    message: str
    default_applied: bool = False
    suggestion: str | None = None


class PreflightReport(BaseModel):
    """Deterministic, judgment-free readiness contract for one run.

    This mirrors the "Preflight / Validator" boundary in a mature Agent
    runtime: it fills defaults, surfaces gaps and refuses to treat an
    under-specified request as if the model can recover from missing input.
    """

    model_config = ConfigDict(extra="forbid")

    ready: bool
    scenario_type: ScenarioType
    tool: str | None = None
    gaps: list[InputGap] = Field(default_factory=list)
    tool_available: bool = False
    preloaded_results: list[dict[str, object]] = Field(default_factory=list)

    @property
    def blocking(self) -> list[InputGap]:
        return [gap for gap in self.gaps if gap.severity == "error"]


class DeterministicPreflight:
    """Run fixed, judgment-free discovery before model orchestration."""

    def run(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
    ) -> list[dict[str, object]]:
        report = self.assess(request, tools)
        return report.preloaded_results

    def assess(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
    ) -> PreflightReport:
        """Execute preflight discovery and produce a readiness contract."""
        gaps = self._completeness_gaps(request)
        names = set(tools.names())
        if "get_trace_overview" in names:
            tools.preload("get_trace_overview")

        invocation = scenario_definition(request.scenario_type).preflight(
            request
        )
        tool_available = invocation.tool in names
        if tool_available:
            tools.preload(invocation.tool, invocation.arguments)
        else:
            gaps.append(
                InputGap(
                    field="trace_capability",
                    severity="error",
                    message=(
                        f"Trace 缺少要求工具 {invocation.tool} 所需的 Trace 能力"
                    ),
                    default_applied=False,
                    suggestion="开启 HTrace 完整转换或补充相应 Slice/Marker 数据",
                )
            )

        return PreflightReport(
            ready=bool(tool_available) and not self.blocking(gaps),
            scenario_type=request.scenario_type,
            tool=invocation.tool if tool_available else None,
            gaps=gaps,
            tool_available=tool_available,
            preloaded_results=tools.preloaded_results(),
        )

    @staticmethod
    def _completeness_gaps(request: AnalyzeRequest) -> list[InputGap]:
        """Surface under-specified user inputs instead of hiding them."""
        gaps: list[InputGap] = []
        if not request.target_process:
            gaps.append(
                InputGap(
                    field="target_process",
                    severity="info",
                    message="未指定 --target-process，由工具和 Agent 联合判断",
                    default_applied=False,
                    suggestion="优先调度唯一进程候选以提高确定性",
                )
            )
        if request.scenario_type is ScenarioType.FRAME_JANK and (
            request.refresh_rate_hz is None
        ):
            gaps.append(
                InputGap(
                    field="refresh_rate_hz",
                    severity="warning",
                    message="帧率/丢帧场景缺少 --refresh-rate",
                    default_applied=True,
                    suggestion="默认 60Hz，实际显示刷新率建议输入",
                )
            )
        if (
            request.scenario_type is not ScenarioType.FRAME_JANK
            and not any(
                (
                    request.time_range,
                    request.operation_marker,
                    request.start_marker,
                    request.end_marker,
                    request.response_marker,
                    request.completion_marker,
                    request.problem_duration_ms,
                )
            )
        ):
            gaps.append(
                InputGap(
                    field="problem_interval",
                    severity="warning",
                    message="未指定问题区间，通过最后有效输入推导",
                    default_applied=True,
                    suggestion="考虑给出 --time-range 或 --problem-duration-ms",
                )
            )
        return gaps

    @staticmethod
    def blocking(gaps: list[InputGap]) -> list[InputGap]:
        return [gap for gap in gaps if gap.severity == "error"]

