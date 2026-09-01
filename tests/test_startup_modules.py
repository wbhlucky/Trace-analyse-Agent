from __future__ import annotations

import sqlite3

from trace_agent.database import StartupModuleRepository


def test_startup_modules_rank_merged_slice_time_on_relevant_threads(
    tmp_path,
) -> None:
    database_path = tmp_path / "startup-modules.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT,
                cat TEXT, name TEXT, depth INT
            );
                INSERT INTO callstack VALUES
                    (1, 1000000000, 60000000, 11, 'load',
                     'H:libfoo.so|I39', 1),
                    (2, 1020000000, 20000000, 11, 'arkts',
                     'H:JSPandaFileExecutor::ExecuteFromBuffer libfoo.so/foo.js', 2),
                (3, 1100000000, 30000000, 11, 'load',
                 'H:async_load_libbar.so', 1),
                (4, 1000000000, 90000000, 12, 'load',
                 'H:libunrelated.so|I39', 1),
                (5, 1140000000, 10000000, 11, 'load',
                 'H:/system/lib64/module/libsystem.z.so|I39', 1);
            """
        )

    result = StartupModuleRepository(database_path).inspect(
        interval_start_ns=1_000_000_000,
        interval_end_ns=1_200_000_000,
        thread_itids=[11],
    )

    assert result["available"] is True
    assert [item["name"] for item in result["modules"]] == [
        "libfoo.so",
        "libbar.so",
        "libsystem.z.so",
    ]
    assert result["total_module_count"] == 3
    foo = result["modules"][0]
    assert foo["observed_duration_ms"] == 60
    assert foo["max_single_duration_ms"] == 60
    assert foo["occurrences"] == 2
    assert foo["kinds"] == ["arkts-evaluate", "module-load"]
    assert foo["relative_width"] == 1
    assert result["modules"][2]["path"] == (
        "/system/lib64/module/libsystem.z.so"
    )
