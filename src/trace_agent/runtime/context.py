from __future__ import annotations

import threading
from dataclasses import dataclass, field

from trace_agent.errors import RunInterrupted
from trace_agent.runtime.cancellation import CancellationToken, Deadline
from trace_agent.runtime.event_bus import TraceAgentEventBus, get_event_bus
from trace_agent.runtime.events import EventType, AgentEvent


@dataclass(frozen=True, slots=True)
class RunContext:
    """Per-run runtime carrier handed to the agent and application layers.

    It owns no transport.  ``publish`` is the only entry point; production code
    never talks to SSE, SQLite or any subscriber from the analysis path.
    """

    run_id: str
    event_bus: TraceAgentEventBus = field(default_factory=get_event_bus)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    deadline: Deadline | None = None

    def publish(
        self,
        event_type: EventType,
        *,
        phase: str | None = None,
        tool_name: str | None = None,
        data: dict | None = None,
    ) -> AgentEvent:
        event = AgentEvent.new(
            run_id=self.run_id,
            type=event_type,
            phase=phase,
            tool_name=tool_name,
            data=data,
        )
        return self.event_bus.publish(event)

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def remaining_seconds(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline.remaining_seconds()

    def deadline_expired(self) -> bool:
        return self.deadline is not None and self.deadline.expired()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled() or self.deadline_expired():
            raise RunInterrupted(run_id=self.run_id)

    def cancellation_token(self) -> CancellationToken:
        return CancellationToken(self.cancel_event)
