from __future__ import annotations

from trace_agent.progress import (
    ProgressEvent,
    ProgressStatus,
    ProgressToAgentBridge,
)
from trace_agent.runtime import (
    EventType,
    RunContext,
    SqliteEventStore,
    TraceAgentEventBus,
)
from trace_agent.errors import RunInterrupted


def test_run_context_publishes_monotonic_sequence() -> None:
    bus = TraceAgentEventBus()
    context_a = RunContext("run-a", bus)
    context_b = RunContext("run-b", bus)

    context_a.publish(EventType.RUN_STARTED)
    context_b.publish(EventType.RUN_STARTED)
    context_a.publish(EventType.PHASE_STARTED, phase="prepare")

    assert [event.seq for event in bus.replay("run-a")] == [1, 2]
    assert [event.seq for event in bus.replay("run-b")] == [1]


def test_subscription_replays_without_gap() -> None:
    bus = TraceAgentEventBus()
    context = RunContext("run-a", bus)
    context.publish(EventType.RUN_STARTED)
    context.publish(EventType.PHASE_STARTED, phase="prepare")

    subscription = bus.subscribe("run-a", from_seq=2)
    events = [subscription.queue.get_nowait()]
    assert events[0].type is EventType.PHASE_STARTED

    context.publish(EventType.PHASE_COMPLETED, phase="prepare")
    events.append(subscription.queue.get_nowait())
    assert [event.seq for event in events] == [2, 3]


def test_progress_bridge_maps_tool_and_evidence_events() -> None:
    bus = TraceAgentEventBus()
    bridge = ProgressToAgentBridge("run-a", bus)
    bridge(
        ProgressEvent(
            "agent.tool",
            ProgressStatus.STARTED,
            "start",
            details={"tool": "get_trace_overview", "arguments": {"x": 1}},
        )
    )
    bridge(
        ProgressEvent(
            "agent.tool",
            ProgressStatus.COMPLETED,
            "done",
            details={"tool": "get_trace_overview", "evidence_id": "evt-1"},
        )
    )

    assert [event.type for event in bus.replay("run-a")] == [
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.EVIDENCE_CREATED,
    ]


def test_cancel_raises_run_interrupted() -> None:
    context = RunContext("run-a", TraceAgentEventBus())
    context.request_cancel()
    try:
        context.raise_if_cancelled()
    except RunInterrupted as exc:
        assert exc.run_id == "run-a"
    else:
        raise AssertionError("expected RunInterrupted")


def test_sqlite_store_replays_events(tmp_path) -> None:
    store = SqliteEventStore(tmp_path / "events.sqlite3")
    bus = TraceAgentEventBus(store=store)
    context = RunContext("run-a", bus)
    context.publish(EventType.RUN_STARTED)
    context.publish(EventType.PHASE_STARTED, phase="prepare")
    rows = store.replay("run-a")
    assert [row["seq"] for row in rows] == [1, 2]
    assert [row["type"] for row in rows] == ["run.started", "phase.started"]
    store.close()
