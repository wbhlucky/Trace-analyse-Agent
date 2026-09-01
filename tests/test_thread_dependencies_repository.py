from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trace_agent.database import CausalThreadDependencyRepository


def _dependency_database(tmp_path: Path) -> Path:
    path = tmp_path / "dependencies.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, pid INT, name TEXT);
            CREATE TABLE thread(itid INT, tid INT, name TEXT, ipid INT);
            CREATE TABLE thread_state(
                id INT, ts INT, dur INT, itid INT, state TEXT
            );
            CREATE TABLE instant(
                id INT, ts INT, name TEXT, ref INT, wakeup_from INT
            );
            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT, name TEXT, depth INT
            );
            INSERT INTO process VALUES (10, 100, 'com.example.app');
            INSERT INTO thread VALUES
                (11, 100, 'main', 10),
                (12, 101, 'worker-important', 10),
                (13, 102, 'worker-noise', 10);
            INSERT INTO thread_state VALUES
                (1, 100000000, 80000000, 11, 'S'),
                (2, 300000000, 2000000, 11, 'S');
            INSERT INTO instant VALUES
                (1, 180000000, 'sched_wakeup', 11, 12),
                (2, 302000000, 'sched_wakeup', 11, 13);
            INSERT INTO callstack VALUES
                (1, 100000000, 80000000, 11, 'WaitForModule', 2),
                (2, 90000000, 100000000, 12, 'LoadBusinessModule.so', 3),
                (3, 300000000, 2000000, 13, 'NotifyTinyTask', 1);
            """
        )
    return path


def test_selects_only_material_scheduler_proven_dependencies(
    tmp_path: Path,
) -> None:
    dependencies = CausalThreadDependencyRepository(
        _dependency_database(tmp_path)
    ).select_significant(
        interval_start_ns=0,
        interval_end_ns=1_000_000_000,
        root_itids=[11],
    )

    assert [item["thread_name"] for item in dependencies] == [
        "worker-important"
    ]
    dependency = dependencies[0]
    assert dependency["total_wait_ms"] == pytest.approx(80)
    assert dependency["wait_share"] == pytest.approx(0.08)
    assert dependency["waits"][0]["waiting_slice"] == "WaitForModule"
    assert dependency["waits"][0]["waker_slice"] == "LoadBusinessModule.so"


def test_overlapping_waits_are_not_double_counted(tmp_path: Path) -> None:
    database = _dependency_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            INSERT INTO thread VALUES (14, 103, 'ui', 10);
            INSERT INTO thread_state VALUES
                (3, 120000000, 60000000, 14, 'S');
            INSERT INTO instant VALUES
                (3, 180000000, 'sched_wakeup', 14, 12);
            """
        )

    dependency = CausalThreadDependencyRepository(database).select_significant(
        interval_start_ns=0,
        interval_end_ns=1_000_000_000,
        root_itids=[11, 14],
    )[0]

    assert dependency["total_wait_ms"] == pytest.approx(80)
    assert len(dependency["waits"]) == 2
