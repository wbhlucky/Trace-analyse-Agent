from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from time import monotonic
from typing import Any, Iterable


class CausalThreadDependencyRepository:
    """Find threads that materially unblock critical application threads.

    Scheduler causality and observed blocked time drive selection. Thread and
    slice names are descriptive output only and never participate in ranking.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 10,
    ) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(f"Trace database does not exist: {database_path}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._database_path = database_path.resolve()
        self._timeout_seconds = timeout_seconds

    def select_significant(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        root_itids: Iterable[int],
        max_dependencies: int = 3,
    ) -> list[dict[str, Any]]:
        if interval_end_ns <= interval_start_ns:
            raise ValueError("interval_end_ns must be later than interval_start_ns")
        if max_dependencies <= 0:
            return []
        roots = sorted({int(itid) for itid in root_itids if int(itid) >= 0})
        if not roots:
            return []

        deadline = monotonic() + self._timeout_seconds
        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    10_000,
                )
                tables = self._tables(connection)
                if not {"thread_state", "instant", "thread", "process"} <= tables:
                    return []
                required_columns = {"ts", "name", "ref", "wakeup_from"}
                if not required_columns <= self._columns(connection, "instant"):
                    return []

                sleeps = self._sleep_intervals(
                    connection,
                    roots=roots,
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                )
                if not sleeps:
                    return []
                wakeups = self._wakeup_events(
                    connection,
                    roots=roots,
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                )
                identities = self._identities(connection)

                waits_by_waker: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for sleep in sleeps:
                    event = self._nearest_wakeup(
                        wakeups.get(sleep["waiting_itid"], []),
                        sleep_end_ns=sleep["sleep_end_ns"],
                    )
                    if event is None or event["waker_itid"] is None:
                        continue
                    waker_itid = int(event["waker_itid"])
                    if waker_itid in roots or waker_itid not in identities:
                        continue
                    distance_ns = abs(event["wakeup_ns"] - sleep["sleep_end_ns"])
                    confidence = 0.9 if distance_ns <= 100_000 else 0.75
                    waits_by_waker[waker_itid].append(
                        {
                            **sleep,
                            "wakeup_ns": event["wakeup_ns"],
                            "wakeup_event": event["name"],
                            "confidence": confidence,
                            "waiting_thread": identities.get(
                                sleep["waiting_itid"], {}
                            ).get("thread_name"),
                            "waiting_slice": None,
                            "waker_slice": None,
                        }
                    )
        except sqlite3.DatabaseError:
            return []

        window_ns = interval_end_ns - interval_start_ns
        significance_ns = max(
            5_000_000,
            min(50_000_000, int(window_ns * 0.01)),
        )
        dependencies: list[dict[str, Any]] = []
        for waker_itid, waits in waits_by_waker.items():
            merged = self._merge_intervals(
                (wait["sleep_start_ns"], wait["sleep_end_ns"])
                for wait in waits
            )
            total_wait_ns = sum(end - start for start, end in merged)
            if total_wait_ns < significance_ns:
                continue
            confidence = max(float(wait["confidence"]) for wait in waits)
            identity = identities[waker_itid]
            dependencies.append(
                {
                    **identity,
                    "itid": waker_itid,
                    "total_wait_ns": total_wait_ns,
                    "total_wait_ms": total_wait_ns / 1_000_000.0,
                    "wait_share": total_wait_ns / window_ns,
                    "impact_score": (total_wait_ns / window_ns) * confidence,
                    "confidence": confidence,
                    "significance_threshold_ms": significance_ns / 1_000_000.0,
                    "wait_count": len(waits),
                    "waits": sorted(
                        waits,
                        key=lambda item: (
                            -(item["sleep_end_ns"] - item["sleep_start_ns"]),
                            item["sleep_start_ns"],
                        ),
                    )[:12],
                }
            )
        selected = sorted(
            dependencies,
            key=lambda item: (
                -item["impact_score"],
                -item["total_wait_ns"],
                item["itid"],
            ),
        )[:max_dependencies]
        self._hydrate_selected_slices(selected, deadline=deadline)
        return selected

    def _hydrate_selected_slices(
        self,
        dependencies: list[dict[str, Any]],
        *,
        deadline: float,
    ) -> None:
        if not dependencies or monotonic() >= deadline:
            return
        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    10_000,
                )
                tables = self._tables(connection)
                for dependency in dependencies:
                    for wait in dependency["waits"]:
                        wait["waiting_slice"] = self._enclosing_slice(
                            connection,
                            tables=tables,
                            itid=wait["waiting_itid"],
                            timestamp_ns=max(
                                wait["sleep_start_ns"],
                                wait["sleep_end_ns"] - 1,
                            ),
                        )
                        wait["waker_slice"] = self._enclosing_slice(
                            connection,
                            tables=tables,
                            itid=dependency["itid"],
                            timestamp_ns=wait["wakeup_ns"],
                        )
        except sqlite3.DatabaseError:
            return

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }

    @staticmethod
    def _sleep_intervals(
        connection: sqlite3.Connection,
        *,
        roots: list[int],
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> list[dict[str, int]]:
        placeholders = ",".join("?" for _ in roots)
        rows = connection.execute(
            "SELECT itid, ts, dur FROM thread_state "
            f"WHERE itid IN ({placeholders}) AND state IN ('S', 'Sleep', 'Sleeping') "
            "AND dur > 0 AND ts < ? AND ts + dur > ? ORDER BY ts",
            [*roots, interval_end_ns, interval_start_ns],
        )
        result: list[dict[str, int]] = []
        for row in rows:
            start_ns = max(int(row[1]), interval_start_ns)
            end_ns = min(int(row[1]) + int(row[2]), interval_end_ns)
            if end_ns - start_ns < 1_000_000:
                continue
            result.append(
                {
                    "waiting_itid": int(row[0]),
                    "sleep_start_ns": start_ns,
                    "sleep_end_ns": end_ns,
                    "wait_duration_ns": end_ns - start_ns,
                }
            )
        return result

    @staticmethod
    def _wakeup_events(
        connection: sqlite3.Connection,
        *,
        roots: list[int],
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> dict[int, list[dict[str, Any]]]:
        placeholders = ",".join("?" for _ in roots)
        rows = connection.execute(
            "SELECT ts, name, ref, wakeup_from FROM instant "
            f"WHERE ref IN ({placeholders}) "
            "AND name IN ('sched_waking', 'sched_wakeup') "
            "AND ts >= ? AND ts <= ? ORDER BY ts",
            [*roots, interval_start_ns - 1_000_000, interval_end_ns + 1_000_000],
        )
        result: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            result[int(row[2])].append(
                {
                    "wakeup_ns": int(row[0]),
                    "name": str(row[1]),
                    "waker_itid": int(row[3]) if row[3] is not None else None,
                }
            )
        return result

    @staticmethod
    def _nearest_wakeup(
        events: list[dict[str, Any]],
        *,
        sleep_end_ns: int,
    ) -> dict[str, Any] | None:
        candidates = [
            event
            for event in events
            if abs(event["wakeup_ns"] - sleep_end_ns) <= 1_000_000
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda event: (
                abs(event["wakeup_ns"] - sleep_end_ns),
                0 if event["name"] == "sched_wakeup" else 1,
            ),
        )

    @staticmethod
    def _identities(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
        rows = connection.execute(
            "SELECT t.itid, t.tid, t.name, t.ipid, p.pid, p.name "
            "FROM thread AS t LEFT JOIN process AS p ON p.ipid = t.ipid"
        )
        return {
            int(row[0]): {
                "tid": int(row[1]) if row[1] is not None else 0,
                "thread_name": str(row[2]) if row[2] else "unknown",
                "ipid": int(row[3]) if row[3] is not None else 0,
                "pid": int(row[4]) if row[4] is not None else 0,
                "process_name": str(row[5]) if row[5] else "unknown",
            }
            for row in rows
            if row[0] is not None
        }

    @staticmethod
    def _enclosing_slice(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        itid: int,
        timestamp_ns: int,
    ) -> str | None:
        if "callstack" not in tables:
            return None
        row = connection.execute(
            "SELECT name FROM callstack WHERE callid = ? AND dur > 0 "
            "AND ts <= ? AND ts + dur >= ? "
            "ORDER BY depth DESC, dur ASC LIMIT 1",
            (itid, timestamp_ns, timestamp_ns),
        ).fetchone()
        return str(row[0]) if row is not None and row[0] else None

    @staticmethod
    def _merge_intervals(
        intervals: Iterable[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        merged: list[list[int]] = []
        for start_ns, end_ns in sorted(intervals):
            if not merged or start_ns > merged[-1][1]:
                merged.append([start_ns, end_ns])
            else:
                merged[-1][1] = max(merged[-1][1], end_ns)
        return [(start_ns, end_ns) for start_ns, end_ns in merged]
