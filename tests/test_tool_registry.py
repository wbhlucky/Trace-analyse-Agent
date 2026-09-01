from __future__ import annotations

import sqlite3

import pytest

from trace_agent.evidence import EvidenceStore
from trace_agent.models import TraceCapability, TraceHandle
from trace_agent.progress import ProgressStatus
from trace_agent.tools import (
    ToolBudgetExceeded,
    ToolDefinition,
    ToolRegistry,
    TraceToolset,
)


def test_registry_only_exposes_tools_supported_by_trace_capabilities():
    registry = ToolRegistry({TraceCapability.FILE_METADATA})
    registry.register(
        ToolDefinition(
            name="overview",
            description="overview",
            input_schema={},
            required_capabilities=frozenset(
                {TraceCapability.FILE_METADATA}
            ),
            handler=lambda _: {"ok": True},
        )
    )
    registry.register(
        ToolDefinition(
            name="frames",
            description="frames",
            input_schema={},
            required_capabilities=frozenset(
                {TraceCapability.FRAME_EVENTS}
            ),
            handler=lambda _: {"frames": []},
        )
    )

    assert registry.names() == ["overview"]
    assert registry.invoke("overview") == {"ok": True}
    with pytest.raises(RuntimeError, match="frame-events"):
        registry.invoke("frames")


def test_registry_rejects_duplicate_tool_names():
    registry = ToolRegistry(set())
    definition = ToolDefinition(
        name="duplicate",
        description="duplicate",
        input_schema={},
        required_capabilities=frozenset(),
        handler=lambda _: {},
    )

    registry.register(definition)
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(definition)


def test_registry_hard_limits_evidence_calls_and_can_seal():
    registry = ToolRegistry(
        set(),
        max_invocations=2,
        per_tool_limits={"query": 1},
    )
    registry.register(
        ToolDefinition(
            name="query",
            description="query",
            input_schema={},
            required_capabilities=frozenset(),
            handler=lambda _: {"ok": True},
        )
    )
    registry.register(
        ToolDefinition(
            name="overview",
            description="overview",
            input_schema={},
            required_capabilities=frozenset(),
            handler=lambda _: {"ok": True},
        )
    )

    assert registry.invoke("query") == {"ok": True}
    with pytest.raises(ToolBudgetExceeded, match="query 调用预算"):
        registry.invoke("query")
    assert registry.invoke("overview") == {"ok": True}
    with pytest.raises(ToolBudgetExceeded, match="总调用预算"):
        registry.invoke("overview")

    snapshot = registry.budget_snapshot()
    assert snapshot["invocation_count"] == 2
    assert snapshot["tool_counts"] == {"query": 1, "overview": 1}

    registry.seal("finalizing")
    with pytest.raises(ToolBudgetExceeded, match="finalizing"):
        registry.invoke("query")


def test_reserved_tool_remains_available_after_general_budget() -> None:
    registry = ToolRegistry(
        {TraceCapability.PERF_SAMPLES},
        max_invocations=1,
        reserved_max_invocations=1,
    )
    registry.register(
        ToolDefinition(
            name="overview",
            description="overview",
            input_schema={},
            required_capabilities=frozenset(),
            handler=lambda _: {"kind": "general"},
        )
    )
    registry.register(
        ToolDefinition(
            name="inspect_perf_profile",
            description="perf",
            input_schema={},
            required_capabilities=frozenset(
                {TraceCapability.PERF_SAMPLES}
            ),
            handler=lambda _: {"kind": "reserved"},
            reserved_budget=True,
        )
    )

    assert registry.invoke("overview") == {"kind": "general"}
    with pytest.raises(ToolBudgetExceeded, match="inspect_perf_profile"):
        registry.invoke("overview")
    assert registry.invoke("inspect_perf_profile") == {"kind": "reserved"}
    assert registry.last_result("inspect_perf_profile") == {
        "kind": "reserved"
    }
    snapshot = registry.budget_snapshot()
    assert snapshot["invocation_count"] == 1
    assert snapshot["reserved_invocation_count"] == 1


def test_invalid_tool_arguments_do_not_consume_success_budget() -> None:
    def handler(arguments):
        if not arguments.get("valid"):
            raise ValueError("invalid arguments")
        return {"ok": True}

    registry = ToolRegistry(
        set(),
        max_invocations=1,
        per_tool_limits={"bounded": 1},
    )
    registry.register(
        ToolDefinition(
            name="bounded",
            description="bounded",
            input_schema={"valid": bool},
            required_capabilities=frozenset(),
            handler=handler,
        )
    )

    with pytest.raises(ValueError, match="invalid arguments"):
        registry.invoke("bounded", {"valid": False})
    assert registry.budget_snapshot()["tool_counts"] == {}
    assert registry.invoke("bounded", {"valid": True}) == {"ok": True}
    assert registry.budget_snapshot()["tool_counts"] == {"bounded": 1}


def test_registry_reports_tool_progress_without_changing_budget() -> None:
    events = []
    registry = ToolRegistry(set(), max_invocations=2)
    registry.set_progress_callback(events.append)
    registry.register(
        ToolDefinition(
            name="ok",
            description="ok",
            input_schema={},
            required_capabilities=frozenset(),
            handler=lambda _: {"evidence_id": "ev-0001"},
        )
    )

    registry.invoke("ok")

    assert [event.status for event in events] == [
        ProgressStatus.STARTED,
        ProgressStatus.COMPLETED,
    ]
    assert events[-1].details["evidence_id"] == "ev-0001"
    assert events[-1].details["activity"] is None
    assert registry.budget_snapshot()["invocation_count"] == 1


def test_registry_reports_dynamic_tool_activity_and_result_metadata() -> None:
    events = []
    registry = ToolRegistry(set(), max_invocations=1)
    registry.set_progress_callback(events.append)
    registry.register(
        ToolDefinition(
            name="query",
            description="query",
            input_schema={"purpose": str},
            required_capabilities=frozenset(),
            handler=lambda _: {
                "evidence_id": "ev-0002",
                "data": {"returned_rows": 18, "truncated": True},
            },
            progress_formatter=lambda arguments, _: (
                f"补充取证：{arguments['purpose']}"
            ),
        )
    )

    registry.invoke("query", {"purpose": "核对主线程唤醒关系"})

    assert [event.message for event in events] == [
        "补充取证：核对主线程唤醒关系",
        "补充取证：核对主线程唤醒关系",
    ]
    assert events[-1].details["returned_rows"] == 18
    assert events[-1].details["truncated"] is True


def test_trace_sql_progress_uses_agent_purpose_and_query_metadata(
    tmp_path,
) -> None:
    trace_path = tmp_path / "current.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "current.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE sample(id INT, name TEXT);
            INSERT INTO sample VALUES (1, 'main'), (2, 'worker');
            """
        )
    registry = TraceToolset(
        TraceHandle(
            trace_id="progress-query",
            trace_path=trace_path,
            format="htrace",
            size_bytes=5,
            database_path=database_path,
            capabilities=[TraceCapability.TRACE_DATABASE],
        ),
        EvidenceStore("progress-query"),
    ).build_registry()
    events = []
    registry.set_progress_callback(events.append)

    registry.invoke(
        "query_trace_sql",
        {
            "sql": "SELECT id, name FROM sample ORDER BY id",
            "parameters": [],
            "purpose": "核对主线程与工作线程身份",
            "max_rows": 10,
        },
    )

    assert events[0].details["activity"] == (
        "补充取证：核对主线程与工作线程身份"
    )
    assert events[-1].details["returned_rows"] == 2
    assert events[-1].details["truncated"] is False


def test_registry_reports_failed_tool_without_consuming_budget() -> None:
    events = []

    def fail(_):
        raise ValueError("bad input")

    registry = ToolRegistry(set(), max_invocations=1)
    registry.set_progress_callback(events.append)
    registry.register(
        ToolDefinition(
            name="fail",
            description="fail",
            input_schema={},
            required_capabilities=frozenset(),
            handler=fail,
        )
    )

    with pytest.raises(ValueError, match="bad input"):
        registry.invoke("fail")

    assert [event.status for event in events] == [
        ProgressStatus.STARTED,
        ProgressStatus.FAILED,
    ]
    assert registry.budget_snapshot()["invocation_count"] == 0
