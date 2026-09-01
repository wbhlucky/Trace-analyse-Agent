from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trace_agent.database import SQLiteTraceRepository, TraceQueryError


@pytest.fixture()
def trace_database(tmp_path: Path) -> Path:
    path = tmp_path / "trace.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE callstack("
            "id INTEGER PRIMARY KEY, name TEXT, ts INTEGER, dur INTEGER)"
        )
        connection.executemany(
            "INSERT INTO callstack(name, ts, dur) VALUES (?, ?, ?)",
            [
                ("first", 100, 10),
                ("second", 200, 20),
                ("third", 300, 30),
            ],
        )
    return path


def test_query_returns_bounded_structured_rows(
    trace_database: Path,
) -> None:
    repository = SQLiteTraceRepository(trace_database)

    result = repository.query(
        "SELECT name, dur FROM callstack "
        "WHERE ts >= ? ORDER BY dur DESC",
        [100],
        max_rows=2,
    )

    assert result.columns == ["name", "dur"]
    assert result.rows == [
        {"name": "third", "dur": 30},
        {"name": "second", "dur": 20},
    ]
    assert result.returned_rows == 2
    assert result.truncated is True


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM callstack",
        "UPDATE callstack SET dur = 0",
        "PRAGMA table_info(callstack)",
        "ATTACH DATABASE 'other.db' AS other",
    ],
)
def test_query_rejects_non_select_statements(
    trace_database: Path,
    sql: str,
) -> None:
    repository = SQLiteTraceRepository(trace_database)

    with pytest.raises(TraceQueryError):
        repository.query(sql)


def test_with_statement_cannot_hide_a_write(
    trace_database: Path,
) -> None:
    repository = SQLiteTraceRepository(trace_database)

    with pytest.raises(TraceQueryError):
        repository.query(
            "WITH selected AS (SELECT id FROM callstack) "
            "DELETE FROM callstack WHERE id IN selected"
        )

    with sqlite3.connect(trace_database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM callstack"
        ).fetchone()[0]
    assert count == 3
