from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from trace_agent.runtime.events import AgentEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    phase TEXT,
    tool_name TEXT,
    data_json TEXT NOT NULL,
    timestamp REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_events_run
    ON agent_events (run_id, seq);
"""


class SqliteEventStore:
    """Single-writer SQLite event log for audit and cross-restart replay."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        return conn

    def append(self, event: AgentEvent) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_events
                    (
                        run_id,
                        seq,
                        event_id,
                        type,
                        phase,
                        tool_name,
                        data_json,
                        timestamp
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.seq,
                    event.event_id,
                    event.type.value,
                    event.phase,
                    event.tool_name,
                    json.dumps(event.data, ensure_ascii=False, default=str),
                    event.timestamp,
                ),
            )
            conn.commit()

    def replay(
        self,
        run_id: str,
        *,
        from_seq: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connection()
            rows = conn.execute(
                """
                SELECT
                    event_id,
                    run_id,
                    seq,
                    timestamp,
                    type,
                    phase,
                    tool_name,
                    data_json
                FROM agent_events
                WHERE run_id = ? AND seq >= ?
                ORDER BY seq ASC
                """,
                (run_id, from_seq),
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "run_id": row[1],
                "seq": row[2],
                "timestamp": row[3],
                "type": row[4],
                "phase": row[5],
                "tool_name": row[6],
                "data": json.loads(row[7] or "{}"),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
