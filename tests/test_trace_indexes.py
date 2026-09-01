from __future__ import annotations

import sqlite3

import pytest

from trace_agent.database.trace_indexes import ensure_trace_indexes


def _sample_database(path) -> None:
    """Create a TraceStreamer-like DB with the hot tables but no indexes."""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE callstack (
                id INTEGER PRIMARY KEY,
                callid INTEGER,
                ts INTEGER
            );
            CREATE TABLE thread (
                itid INTEGER PRIMARY KEY,
                ipid INTEGER
            );
            CREATE TABLE process (
                ipid INTEGER PRIMARY KEY
            );
            CREATE TABLE frame_slice (
                id INTEGER PRIMARY KEY,
                ipid INTEGER
            );
            CREATE TABLE sched_slice (
                itid INTEGER,
                ts INTEGER
            );
            CREATE TABLE frame_maps (
                src_row INTEGER,
                dst_row INTEGER
            );
            CREATE TABLE instant (
                ref INTEGER,
                ts INTEGER
            );
            INSERT INTO callstack (id, callid, ts) VALUES
                (1, 10, 100), (2, 10, 200), (3, 20, 300);
            INSERT INTO thread (itid, ipid) VALUES (10, 1), (20, 2);
            INSERT INTO process (ipid) VALUES (1), (2);
            """
        )


def test_ensure_trace_indexes_creates_indexes_for_existing_tables(
    tmp_path,
):
    database = tmp_path / "trace.db"
    _sample_database(database)

    created = ensure_trace_indexes(database)

    assert created == 10
    with sqlite3.connect(database) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {
        "idx_callstack_callid",
        "idx_callstack_ts",
        "idx_thread_ipid",
        "idx_thread_itid",
        "idx_process_ipid",
        "idx_frame_slice_ipid",
        "idx_frame_maps_src",
        "idx_frame_maps_dst",
        "idx_instant_ref",
        "idx_sched_slice_itid",
    } <= indexes


def test_ensure_trace_indexes_skips_missing_tables(tmp_path):
    database = tmp_path / "trace-minimal.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE process (ipid INTEGER PRIMARY KEY)")

    # 表缺失时跳过对应索引，不抛异常
    created = ensure_trace_indexes(database)

    assert created == 1
    with sqlite3.connect(database) as connection:
        index_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert index_names == {"idx_process_ipid"}


def test_ensure_trace_indexes_is_idempotent(tmp_path):
    database = tmp_path / "trace.db"
    _sample_database(database)

    ensure_trace_indexes(database)
    second_run = ensure_trace_indexes(database)

    assert second_run == 0


def test_ensure_trace_indexes_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        ensure_trace_indexes(tmp_path / "missing.db")
