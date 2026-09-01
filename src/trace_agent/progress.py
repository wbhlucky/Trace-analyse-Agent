from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

from trace_agent.runtime.events import AgentEvent, EventType
from trace_agent.runtime.event_bus import TraceAgentEventBus


class ProgressStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One user-facing lifecycle update for a trace analysis run."""

    stage: str
    status: ProgressStatus
    message: str
    step: int | None = None
    total_steps: int | None = None
    elapsed_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[ProgressEvent], None]


def emit_progress(
    callback: ProgressCallback | None,
    event: ProgressEvent,
) -> None:
    """Progress is observational and must never break an analysis run."""

    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        return


class ProgressToAgentBridge:
    """Translate legacy ``ProgressEvent`` values into runtime ``AgentEvent``s.

    This is a compatibility adapter rather than a new protocol: existing
    ``emit_progress`` call sites keep working, while the same update now also
    enters the event bus for SSE, auditing and replay.
    """

    def __init__(
        self,
        run_id: str,
        event_bus: TraceAgentEventBus,
    ) -> None:
        self._run_id = run_id
        self._event_bus = event_bus

    def __call__(self, event: ProgressEvent) -> None:
        tool_name = (
            event.details.get("tool")
            if isinstance(event.details, dict)
            else None
        )
        evidence_id = (
            event.details.get("evidence_id")
            if isinstance(event.details, dict)
            else None
        )
        if isinstance(tool_name, str) and not tool_name:
            tool_name = None
        if not isinstance(evidence_id, str):
            evidence_id = None

        is_tool_stage = event.stage == "agent.tool"
        event_type = self._event_type(
            event.status,
            is_tool_stage=is_tool_stage,
            tool_name=tool_name,
        )
        if event_type is not None:
            self._publish(
                event_type,
                event,
                tool_name=tool_name,
                evidence_id=evidence_id,
            )
        if (
            is_tool_stage
            and event.status is ProgressStatus.COMPLETED
            and evidence_id
        ):
            self._publish(
                EventType.EVIDENCE_CREATED,
                event,
                tool_name=tool_name,
                evidence_id=evidence_id,
            )

    @staticmethod
    def _event_type(
        status: ProgressStatus,
        *,
        is_tool_stage: bool,
        tool_name: str | None,
    ) -> EventType | None:
        if is_tool_stage:
            if status is ProgressStatus.STARTED:
                return EventType.TOOL_STARTED
            if status is ProgressStatus.COMPLETED:
                return EventType.TOOL_COMPLETED
            if status is ProgressStatus.FAILED:
                return EventType.TOOL_FAILED
            return None
        if status is ProgressStatus.STARTED:
            return EventType.PHASE_STARTED
        if status is ProgressStatus.COMPLETED:
            return EventType.PHASE_COMPLETED
        if status is ProgressStatus.FAILED:
            return EventType.RUN_FAILED
        return None

    def _publish(
        self,
        event_type: EventType,
        event: ProgressEvent,
        *,
        tool_name: str | None,
        evidence_id: str | None,
    ) -> None:
        data: dict[str, Any] = {
            "message": event.message,
            "stage": event.stage,
        }
        if event.step is not None:
            data["step"] = event.step
        if event.total_steps is not None:
            data["total_steps"] = event.total_steps
        if event.elapsed_ms is not None:
            data["elapsed_ms"] = event.elapsed_ms
        if evidence_id:
            data["evidence_id"] = evidence_id
        for key in ("activity", "arguments", "returned_rows", "truncated", "error"):
            if key in event.details:
                data[key] = event.details[key]
        self._event_bus.publish(
            AgentEvent.new(
                run_id=self._run_id,
                type=event_type,
                phase=event.stage,
                tool_name=tool_name,
                data=data,
            )
        )
