from __future__ import annotations

import asyncio
import json
import time

from trace_agent.runtime.latency import (
    LatencyPolicy,
    StageLatencyRecorder,
    StreamIdleWatchdog,
    write_performance_report,
)


def test_stage_recorder_tracks_elapsed_and_deadline() -> None:
    policy = LatencyPolicy(stage_timeouts={"agent.analyze": 0.1})
    recorder = StageLatencyRecorder(policy)

    recorder.start("agent.analyze")
    time.sleep(0.05)
    elapsed = recorder.finish("agent.analyze")

    assert elapsed is not None and elapsed > 0
    snapshot = recorder.snapshot()
    assert snapshot[0]["stage"] == "agent.analyze"
    assert snapshot[0]["elapsed_ms"] == elapsed
    assert snapshot[0]["deadline_ms"] == 100.0
    assert snapshot[0]["deadline_exceeded"] is False


def test_stream_idle_watchdog_preserves_stream() -> None:
    async def stream():
        for item in (1, 2, 3):
            yield item

    async def run():
        watchdog = StreamIdleWatchdog(idle_seconds=0.5)
        return [item async for item in watchdog.wrap(stream())]

    assert asyncio.run(run()) == [1, 2, 3]


def test_write_performance_report_is_machine_readable(tmp_path: Path) -> None:
    policy = LatencyPolicy()
    recorder = StageLatencyRecorder(policy)
    recorder.start("report.render")
    recorder.finish("report.render")

    path = write_performance_report(
        tmp_path / "performance.json",
        run_id="run-1",
        trace_id="trace-1",
        agent="qoder",
        scenario_type="cold-start",
        total_duration_ms=123.0,
        stages=recorder.snapshot(),
        llm_metrics={"ttft_ms": 42.0},
        tool_metrics=[{"tool": "get_trace_overview", "duration_ms": 7.0}],
        job_deadline_seconds=900,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-1"
    assert payload["llm"]["ttft_ms"] == 42.0
    assert payload["tools"][0]["tool"] == "get_trace_overview"
    assert payload["stages"][0]["stage"] == "report.render"
