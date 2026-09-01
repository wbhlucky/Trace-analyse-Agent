from __future__ import annotations

from pathlib import Path
from typing import Protocol

from trace_agent.models import AnalyzeRequest, TraceHandle


class TraceAdapter(Protocol):
    async def prepare(
        self,
        request: AnalyzeRequest,
        workspace: Path,
    ) -> TraceHandle:
        """Validate a trace and build its read-only analysis database."""
