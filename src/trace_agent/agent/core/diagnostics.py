"""SDK-independent module shared by all agent adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trace_agent.agent.core.helpers import bounded_error, bounded_preview, extract_json
from trace_agent.models import (
    AgentAnalysisDraft,
    AnalysisResult,
    AnalyzeRequest,
)
from trace_agent.tools import ToolRegistry


def result_diagnostic(
    *,
    attempt: str,
    result_message: Any | None,
    text_blocks: list[str],
    parse_source: str | None,
    parse_errors: list[str],
) -> dict[str, Any]:
    if result_message is None:
        message_data: dict[str, Any] | None = None
    else:
        structured = getattr(
            result_message,
            "structured_output",
            None,
        )
        message_data = {
            "subtype": getattr(result_message, "subtype", None),
            "is_error": getattr(result_message, "is_error", None),
            "num_turns": getattr(result_message, "num_turns", None),
            "session_id": getattr(
                result_message,
                "session_id",
                None,
            ),
            "stop_reason": getattr(
                result_message,
                "stop_reason",
                None,
            ),
            "terminal_reason": getattr(
                result_message,
                "terminal_reason",
                None,
            ),
            "errors": getattr(result_message, "errors", None),
            "structured_output_type": type(structured).__name__,
            "structured_output_preview": bounded_preview(
                structured,
                50_000,
            ),
            "result_preview": bounded_preview(
                getattr(result_message, "result", None),
                20_000,
            ),
        }
    return {
        "attempt": attempt,
        "parse_source": parse_source,
        "parse_errors": parse_errors,
        "result_message": message_data,
        "text_block_count": len(text_blocks),
        "text_previews": [
            bounded_preview(item, 10_000)
            for item in text_blocks[-3:]
        ],
    }



def write_agent_result(
    request: AnalyzeRequest,
    *,
    status: str,
    attempts: list[dict[str, Any]],
    tools: ToolRegistry,
) -> Path:
    path = request.output_dir.resolve() / "agent-result.json"
    payload = {
        "status": status,
        "tool_budget": tools.budget_snapshot(),
        "attempts": attempts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


