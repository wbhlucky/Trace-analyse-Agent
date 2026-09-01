from __future__ import annotations

from trace_agent.models import AnalysisResult, AnalyzeRequest
from trace_agent.tools import ToolRegistry


class LocalAnalysisAgent:
    """Deterministic agent used to verify the outer application workflow."""

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
    ) -> AnalysisResult:
        overview = tools.last_result("get_trace_overview")
        if overview is None:
            overview = tools.invoke("get_trace_overview")
        evidence_ids = [overview["evidence_id"]]

        if "compare_traces" in tools.names():
            comparison = tools.invoke("compare_traces")
            evidence_ids.append(comparison["evidence_id"])

        return AnalysisResult(
            summary=(
                f"Trace 已接入分析框架，并完成 {len(evidence_ids)} 项文件级数据提取。"
                "当前结果仅用于验证端到端工作流。"
            ),
            findings=[],
            limitations=[
                "本地 smoke Agent 不执行事件级 CPU、调度、I/O、Perf 或关键路径分析。",
                "本地 Agent 不进行模型推理，也不会根据文件大小推断性能结论。",
            ],
        )
