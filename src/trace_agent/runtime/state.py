from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuntimeState(StrEnum):
    """Lifecycle states owned exclusively by :class:`AgentRuntime`.

    States and transitions mirror the provider-agnostic phase model used by
    mature agent harnesses (Claude Code, OpenAI Agents): the transport and
    tool layers observe state, they never set it.
    """

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    REPAIRING = "repairing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# State -> valid next states. Anything else is an invariant violation.
_TRANSITIONS: dict[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.CREATED: frozenset({RuntimeState.STARTING}),
    RuntimeState.STARTING: frozenset({RuntimeState.RUNNING}),
    RuntimeState.RUNNING: frozenset(
        {
            RuntimeState.WAITING_INPUT,
            RuntimeState.WAITING_APPROVAL,
            RuntimeState.REPAIRING,
            RuntimeState.CANCELLING,
            RuntimeState.COMPLETED,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.WAITING_INPUT: frozenset({RuntimeState.RUNNING}),
    RuntimeState.WAITING_APPROVAL: frozenset({RuntimeState.RUNNING}),
    RuntimeState.REPAIRING: frozenset({RuntimeState.RUNNING}),
    RuntimeState.CANCELLING: frozenset(
        {RuntimeState.CANCELLED, RuntimeState.FAILED}
    ),
    RuntimeState.COMPLETED: frozenset(),
    RuntimeState.FAILED: frozenset(),
    RuntimeState.CANCELLED: frozenset(),
}

_TERMINAL_STATES = frozenset(
    {RuntimeState.COMPLETED, RuntimeState.FAILED, RuntimeState.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class RuntimeStateMachine:
    """Small self-contained state machine with strict transition rules.

    Instances are immutable value objects; :class:`AgentRuntime` keeps the
    authoritative current state and calls :meth:`transition` when it owns a
    lifecycle change. This keeps the transition table testable in isolation
    while making illegal transitions fail loudly instead of silently.
    """

    state: RuntimeState = RuntimeState.CREATED

    def transition(self, to: RuntimeState) -> "RuntimeStateMachine":
        if to not in _TRANSITIONS[self.state]:
            raise RuntimeStateTransitionError(
                f"invalid transition: {self.state.value} -> {to.value}"
            )
        return RuntimeStateMachine(state=to)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


class RuntimeStateTransitionError(RuntimeError):
    """Raised when a state transition violates the runtime lifecycle."""
