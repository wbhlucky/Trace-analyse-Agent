from __future__ import annotations

from typing import Protocol

from trace_agent.models import AnalysisResult, AnalyzeRequest
from trace_agent.tools import ToolRegistry


class AnalysisAgent(Protocol):
    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
    ) -> AnalysisResult:
        """Analyze a registered trace using only the provided read-only tools."""
