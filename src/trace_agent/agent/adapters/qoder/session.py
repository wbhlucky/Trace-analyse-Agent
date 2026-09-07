"""Qoder SDK session handling: message classification and stream iteration.

This module is the Qoder-specific half of the turn loop.  The generic retry,
repair, parsing and diagnostics stay in :mod:`trace_agent.agent.core`; this
module only knows how to interpret Qoder's ``AssistantMessage`` /
``ResultMessage`` / ``TextBlock`` stream objects.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from trace_agent.agent.adapters.qoder.client import ensure_sdk
from trace_agent.runtime import EventType, RunContext


def _import_sdk_types() -> dict[str, Any]:
    ensure_sdk()
    from qoder_agent_sdk import (  # local import keeps the SDK optional
        AssistantMessage,
        ResultMessage,
        TextBlock,
    )

    return {
        "AssistantMessage": AssistantMessage,
        "ResultMessage": ResultMessage,
        "TextBlock": TextBlock,
    }


async def collect_text_blocks(
    messages: AsyncIterator[Any],
    *,
    run_context: RunContext | None = None,
) -> tuple[list[str], Any | None]:
    """Extract text blocks and the final result from a Qoder response stream."""
    types = _import_sdk_types()
    AssistantMessage = types["AssistantMessage"]
    ResultMessage = types["ResultMessage"]
    TextBlock = types["TextBlock"]

    text_blocks: list[str] = []
    result_message: Any | None = None

    async for message in messages:
        if run_context is not None:
            run_context.raise_if_cancelled()
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_blocks.append(block.text)
                    if run_context is not None:
                        run_context.publish(
                            EventType.MODEL_MESSAGE_DELTA,
                            data={"delta": block.text},
                        )
        elif isinstance(message, ResultMessage):
            result_message = message
            if message.result:
                text_blocks.append(message.result)
            if run_context is not None:
                run_context.publish(
                    EventType.MODEL_MESSAGE_COMPLETED,
                    data={
                        "result": message.result,
                        "subtype": getattr(message, "subtype", None),
                    },
                )

    return text_blocks, result_message
