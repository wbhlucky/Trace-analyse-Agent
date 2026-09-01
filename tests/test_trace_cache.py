from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Sequence

from trace_agent.models import AnalyzeRequest, ScenarioType
from trace_agent.trace import HTraceAdapter
from trace_agent.trace.trace_streamer import ProcessResult


class CountingTraceStreamerRunner:
    def __init__(self) -> None:
        self.conversions = 0

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        del cwd, timeout_seconds
        if command[-1] == "-v":
            return ProcessResult(0, b"cache-test-v1", b"")
        self.conversions += 1
        database_path = Path(command[-1])
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE process (id INTEGER, ipid INTEGER);
                CREATE TABLE thread (id INTEGER, ipid INTEGER, itid INTEGER);
                CREATE TABLE callstack (id INTEGER, callid INTEGER, ts INTEGER);
                """
            )
        return ProcessResult(0, b"converted", b"")


def _request(trace_path: Path, output_dir: Path) -> AnalyzeRequest:
    return AnalyzeRequest(
        trace_id="cache-case",
        trace_path=trace_path,
        scenario_type=ScenarioType.FRAME_JANK,
        scenario="页面滑动",
        symptom="存在卡顿",
        output_dir=output_dir,
    )


def test_htrace_adapter_reuses_content_addressed_database(tmp_path: Path):
    trace_path = tmp_path / "same.htrace"
    trace_path.write_bytes(b"same-trace-content")
    executable = tmp_path / "trace_streamer-test.exe"
    executable.write_bytes(b"same-converter")
    runner = CountingTraceStreamerRunner()
    adapter = HTraceAdapter(
        trace_streamer_path=executable,
        runner=runner,
        cache_dir=tmp_path / "cache",
    )

    first = asyncio.run(
        adapter.prepare(
            _request(trace_path, tmp_path / "out-1"),
            tmp_path / "work-1",
        )
    )
    second = asyncio.run(
        adapter.prepare(
            _request(trace_path, tmp_path / "out-2"),
            tmp_path / "work-2",
        )
    )

    assert runner.conversions == 1
    assert first.conversions[0].cache_hit is False
    assert second.conversions[0].cache_hit is True
    assert first.conversions[0].cache_key == second.conversions[0].cache_key
    assert first.database_path != second.database_path
    assert second.database_path is not None
    assert second.database_path.is_file()


def test_htrace_cache_invalidates_when_trace_content_changes(tmp_path: Path):
    trace_path = tmp_path / "changing.htrace"
    trace_path.write_bytes(b"version-one")
    executable = tmp_path / "trace_streamer-test.exe"
    executable.write_bytes(b"converter")
    runner = CountingTraceStreamerRunner()
    adapter = HTraceAdapter(
        trace_streamer_path=executable,
        runner=runner,
        cache_dir=tmp_path / "cache",
    )

    first = asyncio.run(
        adapter.prepare(
            _request(trace_path, tmp_path / "out-1"),
            tmp_path / "work-1",
        )
    )
    trace_path.write_bytes(b"version-two")
    second = asyncio.run(
        adapter.prepare(
            _request(trace_path, tmp_path / "out-2"),
            tmp_path / "work-2",
        )
    )

    assert runner.conversions == 2
    assert first.conversions[0].cache_key != second.conversions[0].cache_key
    assert second.conversions[0].cache_hit is False
