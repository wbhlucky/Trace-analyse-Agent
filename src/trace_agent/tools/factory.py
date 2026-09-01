from __future__ import annotations

from typing import Protocol

from trace_agent.evidence import EvidenceStore
from trace_agent.models import TraceHandle
from trace_agent.tools.registry import ToolRegistry
from trace_agent.tools.trace_tools import TraceToolset


class ToolRegistryFactory(Protocol):
    def build(
        self,
        trace: TraceHandle,
        evidence: EvidenceStore,
    ) -> ToolRegistry:
        """Build the run-scoped tools supported by this Trace."""


class DefaultToolRegistryFactory:
    def build(
        self,
        trace: TraceHandle,
        evidence: EvidenceStore,
    ) -> ToolRegistry:
        return TraceToolset(trace, evidence).build_registry()
