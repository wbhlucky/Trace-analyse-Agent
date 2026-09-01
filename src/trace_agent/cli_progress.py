from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from trace_agent.progress import ProgressEvent, ProgressStatus


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

    def __init__(self, console: Console | None = None) -> None:
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
