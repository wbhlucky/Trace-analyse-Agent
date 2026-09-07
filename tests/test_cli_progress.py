from __future__ import annotations

from io import StringIO

from rich.console import Console

from trace_agent.cli_progress import CliProgressRenderer
from trace_agent.progress import ProgressEvent, ProgressStatus
from trace_agent.runtime.events import AgentEvent, EventType


def test_cli_progress_renders_activity_rows_and_escapes_markup() -> None:
    output = StringIO()
    renderer = CliProgressRenderer(
        Console(file=output, force_terminal=False, color_system=None)
    )

    renderer(
        ProgressEvent(
            stage="agent.tool",
            status=ProgressStatus.COMPLETED,
            message="fallback",
            elapsed_ms=55,
            details={
                "tool": "query_trace_sql",
                "activity": "补充取证：[red]核对主线程唤醒关系[/red]",
                "returned_rows": 18,
                "truncated": True,
                "evidence_id": "ev-0005",
            },
        )
    )

    rendered = output.getvalue()
    assert "补充取证：[red]核对主线程唤醒关系[/red]" in rendered
    assert "55ms · 18行 · 结果已截断 · ev-0005" in rendered


def test_cli_progress_keeps_static_label_without_dynamic_activity() -> None:
    event = ProgressEvent(
        stage="agent.tool",
        status=ProgressStatus.STARTED,
        message="正在调用 get_trace_overview",
        details={"tool": "get_trace_overview", "activity": None},
    )

    assert CliProgressRenderer._label(event) == "读取 Trace 概览"


def test_cli_progress_streams_model_delta_and_escapes_markup() -> None:
    output = StringIO()
    renderer = CliProgressRenderer(
        Console(file=output, force_terminal=False, color_system=None)
    )

    renderer.render_event(
        AgentEvent(
            event_id="evt-1",
            run_id="run-1",
            seq=1,
            timestamp=0.0,
            type=EventType.MODEL_MESSAGE_DELTA,
            data={"delta": "[red]main thread[/red] is blocked"},
        )
    )

    rendered = output.getvalue()
    # Raw Rich markup from the model must not become terminal styling.
    assert "[red]main thread[/red] is blocked" in rendered


def test_cli_progress_streams_tool_started() -> None:
    output = StringIO()
    renderer = CliProgressRenderer(
        Console(file=output, force_terminal=False, color_system=None)
    )

    renderer.render_event(
        AgentEvent(
            event_id="evt-2",
            run_id="run-1",
            seq=2,
            timestamp=0.0,
            type=EventType.TOOL_STARTED,
            tool_name="query_trace_sql",
            data={"arguments": {"sql": "select 1"}},
        )
    )

    rendered = output.getvalue()
    assert "query_trace_sql" in rendered
    assert "select 1" in rendered
