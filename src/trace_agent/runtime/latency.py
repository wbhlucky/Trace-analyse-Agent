from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

_T = TypeVar("_T")

StreamItem = Callable[[Any], bool] | None


@dataclass(frozen=True, slots=True)
class LatencyPolicy:
    """Hierarchical deadline budgets for one analysis run.

    Values are resolved in seconds. ``None`` means unbounded and mirrors the
    previous behaviour, so existing callers do not regress. The LLM budgets
    are deliberately separate: connect, first byte, stream idle and overall
    turn are independent failure modes in production agents.
    """

    job_deadline_seconds: float | None = 15 * 60

    stage_timeouts: dict[str, float] = field(
        default_factory=lambda: {
            "trace.prepare": 120,
            "analysis.setup": 60,
            "analysis.preflight": 60,
            "agent.analyze": 420,
            "analysis.normalize": 60,
            "analysis.validate": 60,
            "report.render": 60,
        }
    )

    llm_connect_timeout_seconds: float | None = 30
    llm_first_byte_timeout_seconds: float | None = 120
    llm_idle_timeout_seconds: float | None = 180
    llm_turn_timeout_seconds: float | None = 600

    tool_timeout_seconds: float | None = 30

    def stage_timeout(self, stage: str) -> float | None:
        return self.stage_timeouts.get(stage)


@dataclass(slots=True)
class StageTiming:
    stage: str
    started_monotonic: float
    elapsed_ms: float | None = None
    deadline_exceeded: bool = False


class StageLatencyRecorder:
    """Collect wall-clock timings for every pipeline stage."""

    def __init__(self, policy: LatencyPolicy) -> None:
        self._policy = policy
        self._timings: dict[str, StageTiming] = {}
        self._current: dict[str, float] = {}

    def start(self, stage: str) -> None:
        now = perf_counter()
        self._current[stage] = now
        self._timings.setdefault(
            stage, StageTiming(stage=stage, started_monotonic=now)
        )

    def finish(self, stage: str) -> float | None:
        started = self._current.pop(stage, None)
        if started is None:
            return None
        elapsed_ms = (perf_counter() - started) * 1000
        timing = self._timings.setdefault(
            stage, StageTiming(stage=stage, started_monotonic=started)
        )
        timing.elapsed_ms = elapsed_ms
        timeout = self._policy.stage_timeout(stage)
        if timeout is not None:
            timing.deadline_exceeded = elapsed_ms / 1000 > timeout
        return elapsed_ms

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "stage": timing.stage,
                "elapsed_ms": timing.elapsed_ms,
                "deadline_ms": (
                    self._policy.stage_timeout(timing.stage) * 1000
                    if self._policy.stage_timeout(timing.stage) is not None
                    else None
                ),
                "deadline_exceeded": timing.deadline_exceeded,
            }
            for timing in self._timings.values()
        ]


class StreamIdleWatchdog:
    """Timeout for a stream that has not emitted a byte for a while.

    ``asyncio.timeout`` alone cannot express "the stream may run for a long
    total time as long as it keeps producing output". This wrapper tracks last
    activity and aborts when the gap exceeds ``idle_seconds``.
    """

    def __init__(self, idle_seconds: float | None) -> None:
        self._idle_seconds = idle_seconds

    async def wrap(
        self,
        iterator: AsyncIterator[Any],
        *,
        is_activity: StreamItem = None,
    ) -> AsyncIterator[Any]:
        if self._idle_seconds is None or self._idle_seconds <= 0:
            async for item in iterator:
                yield item
            return

        last_activity = perf_counter()
        while True:
            timeout = self._idle_seconds - (perf_counter() - last_activity)
            if timeout <= 0:
                raise asyncio.TimeoutError(
                    "stream idle for more than "
                    f"{self._idle_seconds:.1f}s"
                )
            try:
                item = await asyncio.wait_for(
                    anext(iterator),
                    timeout=timeout,
                )
            except StopAsyncIteration:
                return
            yield item
            if is_activity is None or is_activity(item):
                last_activity = perf_counter()


async def timeout_async(
    operation: Awaitable[_T],
    *,
    timeout_seconds: float | None,
    scope: str,
) -> _T:
    """Run an awaitable under a named hard deadline."""
    if timeout_seconds is None or timeout_seconds <= 0:
        return await operation
    try:
        async with asyncio.timeout(timeout_seconds):
            return await operation
    except TimeoutError as exc:
        raise asyncio.TimeoutError(
            f"{scope} timed out after {timeout_seconds:.1f}s"
        ) from exc


def write_performance_report(
    path: Path,
    *,
    run_id: str,
    trace_id: str,
    agent: str,
    scenario_type: str,
    total_duration_ms: float,
    stages: list[dict[str, Any]],
    llm_metrics: dict[str, Any] | None = None,
    tool_metrics: list[dict[str, Any]] | None = None,
    job_deadline_seconds: float | None = None,
) -> Path:
    """Persist a machine-readable latency report for CI and dashboards.

    The file is intentionally a plain JSON document at
    ``<output_dir>/performance.json`` so it can be consumed by external
    regression tooling without importing this project.
    """
    payload = {
        "run_id": run_id,
        "trace_id": trace_id,
        "agent": agent,
        "scenario_type": scenario_type,
        "total_duration_ms": total_duration_ms,
        "job_deadline_seconds": job_deadline_seconds,
        "stages": stages,
        "llm": llm_metrics or {},
        "tools": tool_metrics or [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path
