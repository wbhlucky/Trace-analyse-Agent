from __future__ import annotations

import threading
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from trace_agent.progress import ProgressEvent, ProgressStatus
from trace_agent.runtime.events import AgentEvent, EventType
from trace_agent.runtime.event_bus import (
    EventSubscription,
    TraceAgentEventBus,
)


_TOOL_LABELS = {
    "get_trace_overview": "读取 Trace 概览",
    "compare_traces": "对比基线 Trace",
    "query_trace_sql": "执行补充只读查询",
    "inspect_problem_window_candidates": "识别问题时间窗口",
    "inspect_cold_start_candidates": "发现冷启动候选",
    "inspect_cold_start_timeline": "提取冷启动时间线",
    "inspect_completion_latency_candidates": "发现完成时延边界候选",
    "inspect_completion_latency_phases": "拆解响应与完成阶段",
    "inspect_thread_execution": "分析 CPU、调度与唤醒链",
    "inspect_perf_profile": "分析相关线程的 Perf 热点",
    "submit_analysis_result": "校验并提交结构化结论",
}


class CliProgressRenderer:
    """Render one live current operation plus durable completion records."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        event_bus: TraceAgentEventBus | None = None,
        run_id: str | None = None,
    ) -> None:
        self._console = console or Console(stderr=True)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            TimeElapsedColumn(),
            console=self._console,
            transient=True,
        )
        self._task_id = self._progress.add_task(
            "准备分析任务",
            total=None,
        )
        self._started = False
        self._event_bus = event_bus
        self._run_id = run_id
        self._subscription: EventSubscription | None = None
        self._drain_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._started:
            return
        self._progress.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._progress.stop()
        self._started = False

    def subscribe_events(
        self,
        event_bus: TraceAgentEventBus,
        run_id: str,
    ) -> None:
        """Begin streaming runtime AgentEvents to the same console.

        Model deltas, tool chips and phase transitions are rendered as
        multi-line output alongside the transient progress spinner. The
        drain thread owns nothing and only performs console writes, so a
        slow terminal can neither block nor break the analysis worker.
        """
        self._event_bus = event_bus
        self._run_id = run_id
        self._subscription = self._event_bus.subscribe(run_id)
        self._drain_thread = threading.Thread(
            target=self._drain_events,
            name="cli-event-renderer",
            daemon=True,
        )
        self._drain_thread.start()

    def _drain_events(self) -> None:
        if self._subscription is None:
            return
        while True:
            event = self._subscription.queue.get()
            if event is None:
                return
            try:
                self.render_event(event)
            except Exception:
                continue

    def render_event(self, event: AgentEvent) -> None:
        if event.type is EventType.MODEL_MESSAGE_DELTA:
            text = str(event.data.get("delta", "")).rstrip()
            if text:
                self._console.print(
                    f"[dim bold]{'模型'}[/dim bold] {escape(text)}"
                )
            return

        if event.type is EventType.TOOL_STARTED:
            detail = (
                event.data.get("arguments")
                or event.data.get("activity")
                or ""
            )
            self._render_tool_line(
                "",
                f"{'调用'} {event.tool_name or '工具'}",
                detail,
            )
            return

        if event.type is EventType.TOOL_COMPLETED:
            self._render_tool_line(
                "✓",
                f"{'完成'} {event.tool_name or '工具'}",
                "",
            )
            return

        if event.type is EventType.TOOL_FAILED:
            self._render_tool_line(
                "✗",
                f"{'失败'} {event.tool_name or '工具'}",
                str(event.data.get("error") or ""),
            )
            return

        if event.type is EventType.EVIDENCE_CREATED:
            self._render_tool_line(
                "◇",
                f"{'证据'} {event.data.get('evidence_id', '')}",
                str(event.data.get("message") or ""),
            )
            return

        if event.type is EventType.PHASE_STARTED:
            self._console.print(
                f"[cyan]{'阶段'}[/cyan] "
                f"{escape(str(event.data.get('message') or event.phase or ''))}"
            )
            return

        if event.type is EventType.PHASE_COMPLETED:
            self._console.print(
                f"[green]{'阶段完成'}[/green] "
                f"{escape(str(event.data.get('message') or event.phase or ''))}"
            )
            return

        if event.type is EventType.RUN_FAILED:
            self._console.print(
                f"[red]{'运行失败'}[/red] "
                f"{escape(str(event.data.get('error') or ''))}"
            )
            return

        if event.type is EventType.RUN_INTERRUPTED:
            self._console.print(
                f"[yellow]{'运行已中断'}[/yellow] "
                f"{escape(str(event.data.get('error') or ''))}"
            )
            return

        if event.type is EventType.RUN_COMPLETED:
            self._console.print(f"[green]{'运行完成'}[/green]")
            return

    def _render_tool_line(
        self,
        marker: str,
        label: str,
        detail: Any | None,
    ) -> None:
        suffix = ""
        if detail:
            detail_text = str(detail).strip()
            if detail_text:
                suffix = f" {chr(183)} {escape(detail_text)}"
        marker_segment = f"{marker} " if marker else ""
        self._console.print(
            f"[cyan]{marker_segment}[/cyan]{escape(label)}{suffix}"
        )

    def _stop_drain(self) -> None:
        if self._subscription is not None:
            self._subscription.unsubscribe()
            self._subscription = None
        self._drain_thread = None

    def __call__(self, event: ProgressEvent) -> None:
        label = self._label(event)
        position = self._position(event)
        description = f"{position}{escape(label)}"

        if event.status is ProgressStatus.STARTED:
            self._progress.reset(
                self._task_id,
                total=None,
                completed=0,
                description=description,
                start=True,
            )
            return

        if event.status is ProgressStatus.COMPLETED:
            duration = self._duration(event.elapsed_ms)
            evidence_id = event.details.get("evidence_id")
            metadata: list[str] = []
            returned_rows = event.details.get("returned_rows")
            if isinstance(returned_rows, int) and not isinstance(
                returned_rows, bool
            ):
                metadata.append(f"{returned_rows}行")
            if event.details.get("truncated") is True:
                metadata.append("结果已截断")
            if evidence_id:
                metadata.append(str(evidence_id))
            suffix = f" · {' · '.join(metadata)}" if metadata else ""
            self._console.print(
                f"[green]✓[/green] {description}{duration}{suffix}"
            )
            return

        if event.status is ProgressStatus.FAILED:
            duration = self._duration(event.elapsed_ms)
            self._console.print(
                f"[red]✗[/red] {description}{duration}"
            )
            return

        self._console.print(f"[cyan]•[/cyan] {description}")

    @staticmethod
    def _position(event: ProgressEvent) -> str:
        if event.step is None or event.total_steps is None:
            return ""
        return f"[{event.step}/{event.total_steps}] "

    @staticmethod
    def _duration(elapsed_ms: float | None) -> str:
        if elapsed_ms is None:
            return ""
        if elapsed_ms < 1000:
            return f" · {elapsed_ms:.0f}ms"
        return f" · {elapsed_ms / 1000:.1f}s"

    @staticmethod
    def _label(event: ProgressEvent) -> str:
        activity = event.details.get("activity")
        if isinstance(activity, str) and activity.strip():
            return activity.strip()
        tool = event.details.get("tool")
        if isinstance(tool, str):
            return _TOOL_LABELS.get(tool, tool)
        return event.message
