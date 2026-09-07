"""Example replacement SDK adapter (OpenAI).

Demonstrates the pluggable-adapter contract: importing this module must not
import the optional SDK at module load time.  The SDK is required lazily inside
``analyze`` through :func:`trace_agent.agent.sdk_guard.require_sdk`, so a
missing extra surfaces as a uniform :class:`ProviderUnavailable` error.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trace_agent.agent.adapters.openai.client import ensure_sdk
from trace_agent.agent.sdk_guard import require_sdk
from trace_agent.models import AnalysisResult, AnalyzeRequest
from trace_agent.runtime import RunContext
from trace_agent.tools import ToolRegistry


class OpenAIAnalysisAgent:
    """Minimal AnalysisAgent port implementation for the OpenAI provider.

    This is intentionally a thin skeleton: it proves that adding a second
    provider requires only this adapter module plus one registry entry, not a
    change to the application/validation layers.
    """

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: ToolRegistry,
        *,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
        run_context: RunContext | None = None,
    ) -> AnalysisResult:
        del checkpoint, run_context
        ensure_sdk()
        # Imported lazily so production imports never touch the optional SDK.
        from openai import AsyncOpenAI  # type: ignore

        client = AsyncOpenAI()
        overview = tools.last_result("get_trace_overview")
        if overview is None:
            overview = tools.invoke("get_trace_overview")

        del client
        return AnalysisResult(
            summary="OpenAI analysis placeholder",
            findings=[],
            limitations=["OpenAI adapter is a pluggability example."],
        )
