from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trace_agent.database import ThreadExecutionRepository


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "trace.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INTEGER, pid INTEGER, name TEXT);
            CREATE TABLE thread(
                itid INTEGER, tid INTEGER, name TEXT, ipid INTEGER
            );
            CREATE TABLE thread_state(
                id INTEGER, ts INTEGER, dur INTEGER,
                itid INTEGER, state TEXT
            );
            CREATE TABLE sched_slice(
                id INTEGER, ts INTEGER, dur INTEGER, cpu INTEGER,
                itid INTEGER, priority INTEGER
            );
            CREATE TABLE instant(
                id INTEGER, ts INTEGER, name TEXT,
                ref INTEGER, wakeup_from INTEGER
            );
            CREATE TABLE callstack(
                id INTEGER, ts INTEGER, dur INTEGER, callid INTEGER,
                name TEXT, depth INTEGER
            );
            INSERT INTO process VALUES (10, 100, 'com.example.app');
            INSERT INTO thread VALUES (11, 100, 'main', 10);
            INSERT INTO thread VALUES (12, 101, 'worker', 10);
            INSERT INTO instant VALUES (
                1, 1500000000, 'sched_wakeup', 11, 12
            );
            INSERT INTO callstack VALUES
                (1, 1200000000, 300000000, 11, 'WaitForData', 1),
                (2, 1490000000,  20000000, 12, 'NotifyDataReady', 1);
            """
        )
        connection.executemany(
            "INSERT INTO thread_state VALUES (?, ?, ?, ?, ?)",
            [
                (1, 900_000_000, 300_000_000, 11, "Running"),
                (2, 1_200_000_000, 300_000_000, 11, "S"),
                (3, 1_500_000_000, 200_000_000, 11, "R"),
                (4, 1_700_000_000, 100_000_000, 11, "Running"),
                (5, 1_800_000_000, 300_000_000, 11, "D-IO"),
            ],
        )
        connection.executemany(
            "INSERT INTO sched_slice VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 900_000_000, 300_000_000, 1, 11, 53),
                (2, 1_700_000_000, 100_000_000, 2, 11, 51),
                (3, 1_500_000_000, 200_000_000, 3, 12, 50),
            ],
        )
    return path


def test_inspect_clips_thread_data_to_exact_interval(
    tmp_path: Path,
) -> None:
    result = ThreadExecutionRepository(_database(tmp_path)).inspect(
        interval_start_ns=1_000_000_000,
        interval_end_ns=2_000_000_000,
        itid=11,
    )

    thread = result["thread_execution"]
    states = thread["state_breakdown"]
    assert sum(states.values()) == pytest.approx(1_000.0)
    assert states == {
        "running_ms": 300.0,
        "runnable_ms": 200.0,
        "sleeping_ms": 300.0,
        "uninterruptible_io_ms": 200.0,
        "uninterruptible_other_ms": 0.0,
        "other_ms": 0.0,
    }
    assert thread["longest_running_ms"] == pytest.approx(200.0)
    assert thread["longest_runnable_ms"] == pytest.approx(200.0)
    assert thread["longest_sleep_ms"] == pytest.approx(300.0)
    assert sum(
        item["running_ms"] for item in thread["cpu_distribution"]
    ) == pytest.approx(states["running_ms"])
    assert thread["cpu_migrations"] == 1
    assert thread["schedule_slices"] == 2
    assert thread["priority"]["observed_values"] == [53, 51]
    assert thread["wakeup_chain"] == [
        {
            "depth": 1,
            "sleep_start_ns": 1_200_000_000,
            "sleep_end_ns": 1_500_000_000,
            "waiting_itid": 11,
            "waiting_thread": "main",
            "enclosing_slice": "WaitForData",
            "wakeup_ns": 1_500_000_000,
            "waker_itid": 12,
            "waker_process": "com.example.app",
            "waker_thread": "worker",
            "waker_slice": "NotifyDataReady",
            "confidence": 0.9,
            "evidence_ids": [],
        }
    ]
    assert thread["contention_intervals"][0]["competing_thread"] == (
        "worker"
    )
    assert thread["contention_intervals"][0]["duration_ms"] == 200


def test_wakeup_chain_stops_before_revisiting_a_thread(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            INSERT INTO thread_state VALUES
                (10, 1300000000, 200000000, 12, 'S');
            INSERT INTO instant VALUES
                (2, 1500000000, 'sched_wakeup', 12, 11);
            """
        )

    result = ThreadExecutionRepository(database).inspect(
        interval_start_ns=1_000_000_000,
        interval_end_ns=2_000_000_000,
        itid=11,
    )

    chain = result["thread_execution"]["wakeup_chain"]
    assert [hop["waiting_itid"] for hop in chain] == [11]
    assert chain[0]["waker_itid"] == 12
    assert any("循环前截断" in item for item in result["limitations"])


def test_wakeup_chain_clips_upstream_sleep_to_parent_interval(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            INSERT INTO thread VALUES (13, 102, 'upstream', 10);
            INSERT INTO thread_state VALUES
                (10, 800000000, 700000000, 12, 'S');
            INSERT INTO instant VALUES
                (2, 1500000000, 'sched_wakeup', 12, 13);
            """
        )

    result = ThreadExecutionRepository(database).inspect(
        interval_start_ns=1_000_000_000,
        interval_end_ns=2_000_000_000,
        itid=11,
    )

    chain = result["thread_execution"]["wakeup_chain"]
    assert [hop["waiting_itid"] for hop in chain] == [11, 12]
    assert chain[1]["sleep_start_ns"] == 1_000_000_000
    assert chain[1]["sleep_end_ns"] == 1_500_000_000
    assert all(
        1_000_000_000 <= hop["sleep_start_ns"] < hop["sleep_end_ns"]
        <= 2_000_000_000
        for hop in chain
    )
    assert any("精确父窗口裁剪" in item for item in result["limitations"])
