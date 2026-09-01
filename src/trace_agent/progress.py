from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


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
