from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from trace_agent.models import AnalysisResult, AnalyzeRequest
from trace_agent.runtime import RunContext
from trace_agent.tools import ToolRegistry


class AnalysisAgent(Protocol):
    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult:
        """Analyze a registered trace using only the provided read-only tools."""


class CheckpointedAnalysisAgent(AnalysisAgent, Protocol):
    """Analysis agents that support the full checkpoint/run-context surface."""

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult: ...
