from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trace_agent.models import AnalysisResult, AnalyzeRequest
from trace_agent.runtime import RunContext
from trace_agent.tools import ToolRegistry


class LocalAnalysisAgent:
    """Deterministic agent used to verify the outer application workflow."""

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult:
        del checkpoint, run_context
        overview = tools.last_result("get_trace_overview")
        if overview is None:
            overview = tools.invoke("get_trace_overview")
        evidence_ids = [overview["evidence_id"]]

        if "compare_traces" in tools.names():
            comparison = tools.invoke("compare_traces")
            evidence_ids.append(comparison["evidence_id"])

        return AnalysisResult(
            summary=(
                f"Trace \u5df2\u63a5\u5165\u5206\u6790\u6846\u67b6\uff0c\u5e76\u5b8c\u6210 {len(evidence_ids)} \u9879\u6587\u4ef6\u7ea7\u6570\u636e\u63d0\u53d6\u3002"
                "\u5f53\u524d\u7ed3\u679c\u4ec5\u7528\u4e8e\u9a8c\u8bc1\u7aef\u5230\u7aef\u5de5\u4f5c\u6d41\u3002"
            ),
            findings=[],
            limitations=[
                "\u672c\u5730 smoke Agent \u4e0d\u6267\u884c\u4e8b\u4ef6\u7ea7 CPU\u3001\u8c03\u5ea6\u3001I/O\u3001Perf \u6216\u5173\u952e\u8def\u5f84\u5206\u6790\u3002",
                "\u672c\u5730 Agent \u4e0d\u8fdb\u884c\u6a21\u578b\u63a8\u7406\uff0c\u4e5f\u4e0d\u4f1a\u6839\u636e\u6587\u4ef6\u5927\u5c0f\u63a8\u65ad\u6027\u80fd\u7ed3\u8bba\u3002",
            ],
        )
