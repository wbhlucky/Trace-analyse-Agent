from __future__ import annotations

import sqlite3

import pytest

from trace_agent.models import TraceCapability
from trace_agent.trace.trace_streamer import (
    TraceDatabaseInspector,
    TraceStreamerError,
    TraceStreamerLocator,
)


def test_locator_selects_project_binary_by_platform(tmp_path):
    binary = (
        tmp_path
        / "windows-x86_64"
        / "trace_streamer.exe"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")

    locator = TraceStreamerLocator(
        bundle_roots=[tmp_path],
        system="Windows",
        machine="AMD64",
    )

    assert locator.resolve() == binary.resolve()


def test_locator_falls_back_when_platform_machine_is_empty(
    tmp_path,
    monkeypatch,
):
    binary = (
        tmp_path
        / "windows-x86_64"
        / "trace_streamer.exe"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    monkeypatch.setattr(
        "trace_agent.trace.trace_streamer.sysconfig.get_platform",
        lambda: "win-amd64",
    )

    locator = TraceStreamerLocator(
        bundle_roots=[tmp_path],
        system="Windows",
        machine="",
    )

    assert locator.resolve() == binary.resolve()


def test_locator_reports_unsupported_platform(tmp_path):
    locator = TraceStreamerLocator(
        bundle_roots=[tmp_path],
        system="Linux",
        machine="aarch64",
    )

    with pytest.raises(TraceStreamerError, match="--trace-streamer"):
        locator.resolve()


def test_database_inspector_derives_trace_capabilities(tmp_path):
    database = tmp_path / "trace.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE process (id INTEGER);
            CREATE TABLE thread (id INTEGER);
            CREATE TABLE callstack (id INTEGER);
            CREATE TABLE sched_slice (id INTEGER);
            CREATE TABLE frame_slice (id INTEGER);
                CREATE TABLE diskio (id INTEGER);
                CREATE TABLE instant (id INTEGER);
                CREATE TABLE perf_sample (id INTEGER);
                """
            )

    capabilities = set(TraceDatabaseInspector().inspect(database))

    assert {
        TraceCapability.TRACE_DATABASE,
        TraceCapability.PROCESSES,
        TraceCapability.THREADS,
        TraceCapability.SLICES,
        TraceCapability.CPU_SCHEDULING,
        TraceCapability.FRAME_EVENTS,
        TraceCapability.IO_EVENTS,
        TraceCapability.MARKERS,
    } <= capabilities
    assert TraceCapability.PERF_SAMPLES not in capabilities


def test_database_inspector_requires_real_perf_samples(tmp_path):
    database = tmp_path / "trace-with-perf.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE perf_sample (
                id INTEGER,
                timestamp_trace INTEGER
            );
            INSERT INTO perf_sample VALUES (1, 1000);
            """
        )

    capabilities = set(TraceDatabaseInspector().inspect(database))

    assert TraceCapability.PERF_SAMPLES in capabilities


def test_database_inspector_requires_real_app_startup_stages(
    tmp_path,
):
    database = tmp_path / "trace-with-startup.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE app_startup (id INTEGER);
            INSERT INTO app_startup VALUES (1);
            """
        )

    capabilities = set(TraceDatabaseInspector().inspect(database))

    assert TraceCapability.APP_STARTUP_STAGES in capabilities
