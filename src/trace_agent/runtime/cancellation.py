from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Deadline:
    """A monotonic deadline used for hard cancellation propagation.

    Unlike a passive boundary check, code that enters a long-running awaitable
    can query ``remaining_seconds`` and pass it to ``asyncio.timeout`` so the
    in-flight operation is actually cancelled, not merely regretted later.
    """

    monotonic_deadline: float

    @classmethod
    def after_seconds(cls, seconds: float | None) -> Deadline | None:
        if seconds is None or seconds <= 0:
            return None
        return cls(time.monotonic() + seconds)

    def remaining_seconds(self) -> float:
        return self.monotonic_deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def clamp(self, seconds: float) -> float:
        """Return the tighter of ``seconds`` and this deadline."""
        remaining = self.remaining_seconds()
        if remaining <= 0:
            return 0.0
        return min(seconds, remaining)


class CancellationToken:
    """Cooperative cancellation signal compatible with ``threading.Event``.

    The underlying ``threading.Event`` remains the source of truth because it
    is shared with the web cancellation path. ``request_cancel`` sets it and
    ``raise_if_requested`` converts it into ``RunInterrupted`` at the next
    cooperative boundary.
    """

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event: Final[threading.Event] = event or threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    def request_cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self, run_id: str | None = None) -> None:
        if self.is_cancelled():
            from trace_agent.errors import RunInterrupted

            raise RunInterrupted(run_id=run_id)
