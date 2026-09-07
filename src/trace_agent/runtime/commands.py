from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeCommandType(StrEnum):
    """Wire-stable vocabulary for control-plane commands.

    Control commands are the inbound counterpart of outbound ``AgentEvent``
    values: they express intent (cancel, resume, approve) without carrying SDK
    objects or transport-specific details.
    """

    CANCEL_RUN = "command.cancel_run"
    RESUME_RUN = "command.resume_run"
    APPROVE_TOOL = "command.approve_tool"
    DENY_TOOL = "command.deny_tool"


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    """A request from UI/API/system into the runtime.

    ``command_id`` stays unique for idempotency and audit; ``run_id`` routes
    it to exactly one owned lifecycle. ``type`` accepts either the enum value
    or its string form so wire payloads round-trip without a separate mapper.
    """

    command_id: str
    run_id: str
    type: RuntimeCommandType
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, RuntimeCommandType):
            object.__setattr__(self, "type", RuntimeCommandType(self.type))

    @classmethod
    def new(
        cls,
        *,
        command_id: str,
        run_id: str,
        type: RuntimeCommandType,
        data: dict[str, Any] | None = None,
    ) -> "RuntimeCommand":
        return cls(
            command_id=command_id,
            run_id=run_id,
            type=type,
            data=dict(data or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "run_id": self.run_id,
            "type": self.type.value,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Deterministic acknowledgement from the runtime for one command."""

    command_id: str
    accepted: bool
    reason: str | None = None
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "accepted": self.accepted,
            "reason": self.reason,
            "state": self.state,
        }
