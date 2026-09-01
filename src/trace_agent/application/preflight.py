from __future__ import annotations

from trace_agent.models import AnalyzeRequest
from trace_agent.scenarios import scenario_definition
from trace_agent.tools import ToolRegistry


class DeterministicPreflight:
    """Run fixed, judgment-free discovery before model orchestration."""

    def run(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
    ) -> list[dict[str, object]]:
        names = set(tools.names())
        if "get_trace_overview" in names:
            tools.preload("get_trace_overview")

        invocation = scenario_definition(request.scenario_type).preflight(
            request
        )
        if invocation.tool in names:
            tools.preload(invocation.tool, invocation.arguments)
        return tools.preloaded_results()
