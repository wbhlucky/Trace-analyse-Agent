"""Stable, provider-agnostic contracts for agent and transport adapters.

The runtime and application layers must depend only on the contracts and
metadata defined here. Concrete SDK adapters (Qoder, OpenAI, Claude, local)
implement these interfaces and register themselves through
:mod:`trace_agent.agent.registry`. Adding a provider must never require
editing business logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from trace_agent.models import AnalysisResult, AnalyzeRequest
from trace_agent.runtime import RunContext
from trace_agent.tools import ToolRegistry


class AnalysisAgent(Protocol):
    """Provider-independent entry point for one analysis task."""

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult:
        """Analyze a registered trace using only the read-only tools."""
        ...


class CheckpointedAnalysisAgent(AnalysisAgent, Protocol):
    """The full surface that SDK adapters that support checkpoints provide."""

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult: ...


class RunCapableTransport(Protocol):
    """High-level, provider-agnostic boundary for one agent invocation.

    The runtime depends on this interface only; concrete SDK adapters own the
    wire protocol, streaming, authentication, and session lifecycle.
    """

    async def run(
        self,
        request: Any,
        *,
        session: Any | None = None,
        timeout: float | None = None,
    ) -> Any:
        ...

    async def cancel(self, session_id: str) -> None: ...

    async def close(self, session_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Declarative capabilities a provider advertises to the application."""

    name: str
    requires_model_auth: bool = False
    requires_preflight: bool = False
    supports_checkpoint: bool = False
    requires_perf_evidence: bool = False
    strict_result_validation: bool = False
    extra_label: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class AgentSelection:
    """Agent instance plus the skills and provider metadata in play."""

    agent: AnalysisAgent
    skills: list[Any] = field(default_factory=list)
    provider: ProviderMetadata | None = None
