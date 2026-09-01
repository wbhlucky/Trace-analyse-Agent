from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import time
from typing import Any
from uuid import uuid4


class EventType(StrEnum):
    """Wire-stable event vocabulary shared by CLI, web and persistence."""

    RUN_STARTED = "run.started"
    PHASE_STARTED = "phase.started"
    PHASE_COMPLETED = "phase.completed"
    MODEL_MESSAGE_DELTA = "model.message.delta"
    MODEL_MESSAGE_COMPLETED = "model.message.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    EVIDENCE_CREATED = "evidence.created"
    APPROVAL_REQUIRED = "approval.required"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_FAILED = "run.failed"
    RUN_COMPLETED = "run.completed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Immutable, JSON-serialisable event envelope.

    ``seq`` is strictly monotonic per ``run_id`` and is the only durable
    cursor used by SSE replay and SQLite persistence.  ``event_id`` stays
    unique across the whole system for observability tooling.
    """

    event_id: str
    run_id: str
    seq: int
    timestamp: float
    type: EventType
    phase: str | None = None
    tool_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        type: EventType,
        phase: str | None = None,
        tool_name: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent:
        """Create an event; the bus replaces ``seq=0`` with the real cursor."""
        return cls(
            event_id=f"evt-{uuid4().hex}",
            run_id=run_id,
            seq=0,
            timestamp=time(),
            type=type,
            phase=phase,
            tool_name=tool_name,
            data=dict(data or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "type": self.type.value,
            "phase": self.phase,
            "tool_name": self.tool_name,
            "data": dict(self.data),
        }
