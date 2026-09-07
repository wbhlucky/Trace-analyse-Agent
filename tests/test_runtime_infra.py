from __future__ import annotations

from trace_agent.runtime import (
    JsonlEventSink,
    JsonlSessionStore,
    RuntimeCommand,
    RuntimeCommandType,
    RuntimeState,
    RuntimeStateMachine,
    RuntimeStateTransitionError,
)
from trace_agent.runtime.sessions import AgentSessionRecord
from trace_agent.runtime.events import AgentEvent, EventType


def test_state_machine_happy_path() -> None:
    machine = RuntimeStateMachine()
    assert machine.state is RuntimeState.CREATED
    machine = machine.transition(RuntimeState.STARTING)
    machine = machine.transition(RuntimeState.RUNNING)
    machine = machine.transition(RuntimeState.COMPLETED)
    assert machine.state is RuntimeState.COMPLETED
    assert machine.is_terminal


def test_state_machine_rejects_invalid_transition() -> None:
    machine = RuntimeStateMachine()
    try:
        machine.transition(RuntimeState.RUNNING)
    except RuntimeStateTransitionError:
        pass
    else:
        raise AssertionError("expected RuntimeStateTransitionError")


def test_state_machine_cancel_path() -> None:
    machine = RuntimeStateMachine()
    machine = machine.transition(RuntimeState.STARTING)
    machine = machine.transition(RuntimeState.RUNNING)
    machine = machine.transition(RuntimeState.CANCELLING)
    machine = machine.transition(RuntimeState.CANCELLED)
    assert machine.state is RuntimeState.CANCELLED
    assert machine.is_terminal


def test_state_machine_repair_path() -> None:
    machine = RuntimeStateMachine()
    machine = machine.transition(RuntimeState.STARTING)
    machine = machine.transition(RuntimeState.RUNNING)
    machine = machine.transition(RuntimeState.REPAIRING)
    machine = machine.transition(RuntimeState.RUNNING)
    assert machine.state is RuntimeState.RUNNING


def test_runtime_command_serialization_round_trip() -> None:
    command = RuntimeCommand.new(
        command_id="cmd-1",
        run_id="run-1",
        type=RuntimeCommandType.CANCEL_RUN,
        data={"reason": "user"},
    )
    payload = command.to_dict()
    assert payload["type"] == "command.cancel_run"
    assert payload["run_id"] == "run-1"
    reloaded = RuntimeCommand(**payload)
    assert reloaded.command_id == "cmd-1"
    assert reloaded.type is RuntimeCommandType.CANCEL_RUN


def test_jsonl_event_sink_appends_json() -> None:
    import json
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "events.jsonl"
        sink = JsonlEventSink(path)
        event = AgentEvent.new(
            run_id="run-a",
            type=EventType.RUN_STARTED,
            data={"hello": "world"},
        )
        sink.emit(event)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["run_id"] == "run-a"
        assert payload["type"] == "run.started"
        assert payload["data"] == {"hello": "world"}


def test_jsonl_session_store_load_save_and_close(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions.jsonl")
    session = AgentSessionRecord(
        session_id="sess-1",
        task_id="task-1",
    )
    store.create(session)

    loaded = store.load("sess-1")
    assert loaded is not None
    assert loaded.session_id == "sess-1"
    assert loaded.task_id == "task-1"

    loaded.runtime_state = RuntimeState.RUNNING  # type: ignore[assignment]
    store.save(loaded)

    reloaded = store.load("sess-1")
    assert reloaded is not None
    assert reloaded.runtime_state is RuntimeState.RUNNING

    event = AgentEvent.new(run_id="sess-1", type=EventType.TOOL_STARTED)
    store.append_event("sess-1", event)
    assert store.last_event_id("sess-1") == event.event_id

    store.close("sess-1")
    assert store.load("sess-1") is None
