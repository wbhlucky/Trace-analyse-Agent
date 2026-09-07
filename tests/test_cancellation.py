from __future__ import annotations

import threading

from trace_agent.errors import RunInterrupted
from trace_agent.runtime import CancellationToken, Deadline, RunContext


def test_deadline_clamp_respects_remaining_time() -> None:
    deadline = Deadline.after_seconds(5)
    assert deadline is not None
    assert deadline.remaining_seconds() > 0
    assert deadline.clamp(60) <= 5
    assert deadline.clamp(1) == 1


def test_deadline_expires_after_short_window() -> None:
    deadline = Deadline.after_seconds(0.05)
    assert deadline is not None
    assert not deadline.expired()
    import time

    time.sleep(0.1)
    assert deadline.expired()


def test_cancellation_token_sets_shared_threading_event() -> None:
    event = threading.Event()
    token = CancellationToken(event)
    assert not token.is_cancelled()
    token.request_cancel()
    assert event.is_set()
    assert token.is_cancelled()


def test_run_context_deadline_raises_on_cancel() -> None:
    event = threading.Event()
    context = RunContext(
        "run-a",
        cancel_event=event,
        deadline=Deadline.after_seconds(0.01),
    )
    import time

    time.sleep(0.05)
    assert context.deadline_expired()
    try:
        context.raise_if_cancelled()
    except RunInterrupted as exc:
        assert exc.run_id == "run-a"
    else:
        raise AssertionError("expected RunInterrupted on expired deadline")
