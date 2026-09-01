from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from time import monotonic
from typing import Any


class ThreadExecutionRepository:
    """Project one thread's scheduling facts into one exact interval."""

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 15,
    ) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(f"Trace 数据库不存在：{database_path}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._database_path = database_path.resolve()
        self._timeout_seconds = timeout_seconds

    def inspect(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        itid: int,
    ) -> dict[str, Any]:
        if interval_end_ns <= interval_start_ns:
            raise ValueError("线程分析结束时间必须晚于开始时间")
        if itid < 0:
            raise ValueError("itid 必须是非负整数")

        deadline = monotonic() + self._timeout_seconds
        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    10_000,
                )
                tables = self._tables(connection)
                identity = self._identity(connection, itid)
                state_rows = list(
                    connection.execute(
                        "SELECT ts, dur, state FROM thread_state "
                        "WHERE itid = ? AND ts < ? AND ts + dur > ? "
                        "ORDER BY ts, id",
                        (itid, interval_end_ns, interval_start_ns),
                    )
                )
                sched_rows = list(
                    connection.execute(
                        "SELECT ts, dur, cpu, priority FROM sched_slice "
                        "WHERE itid = ? AND ts < ? AND ts + dur > ? "
                        "ORDER BY ts, id",
                        (itid, interval_end_ns, interval_start_ns),
                    )
                )
                wakeup_chain, wakeup_limitations = self._wakeup_chain(
                    connection,
                    tables=tables,
                    waiting_itid=itid,
                    state_rows=state_rows,
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                )
                contention_intervals = self._contention_intervals(
                    connection,
                    tables=tables,
                    target_itid=itid,
                    state_rows=state_rows,
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                )
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise RuntimeError("线程执行分析超时") from exc
            raise RuntimeError(f"线程执行分析失败：{exc}") from exc

        states, longest, state_limitations = self._aggregate_states(
            state_rows,
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
        )
        scheduling = self._aggregate_scheduling(
            sched_rows,
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
        )
        interval_ms = (interval_end_ns - interval_start_ns) / 1_000_000.0
        diagnosis, assessment = self._diagnosis(
            interval_ms=interval_ms,
            states=states,
        )
        return {
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "interval_duration_ms": interval_ms,
            "itid": itid,
            "identity": identity,
            "thread_execution": {
                "process_name": identity["process_name"],
                "thread_name": identity["thread_name"],
                "pid": identity["pid"],
                "ipid": identity["ipid"],
                "tid": identity["tid"],
                "itid": itid,
                "state_breakdown": states,
                "cpu_distribution": scheduling["cpu_distribution"],
                "cpu_migrations": scheduling["cpu_migrations"],
                "schedule_slices": scheduling["schedule_slices"],
                "longest_running_ms": longest["running_ms"],
                "longest_runnable_ms": longest["runnable_ms"],
                "longest_sleep_ms": longest["sleeping_ms"],
                "priority": scheduling["priority"],
                "contention_intervals": contention_intervals,
                "wakeup_chain": wakeup_chain,
                "diagnosis": diagnosis,
                "assessment": assessment,
                "confidence": 0.9 if state_rows else 0.3,
            },
            "raw_state_values": sorted(
                {str(row[2]) for row in state_rows if row[2] is not None}
            ),
            "limitations": [*state_limitations, *wakeup_limitations],
        }

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'view')"
            )
        }

    @staticmethod
    def _identity(
        connection: sqlite3.Connection,
        itid: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT t.tid, t.name, t.ipid, p.pid, p.name "
            "FROM thread AS t "
            "LEFT JOIN process AS p ON p.ipid = t.ipid "
            "WHERE t.itid = ? LIMIT 1",
            (itid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"未找到 itid={itid} 的线程")
        return {
            "tid": int(row[0]),
            "thread_name": str(row[1]) if row[1] else None,
            "ipid": int(row[2]),
            "pid": int(row[3]) if row[3] is not None else 0,
            "process_name": str(row[4]) if row[4] else "unknown",
        }

    @classmethod
    def _wakeup_chain(
        cls,
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        waiting_itid: int,
        state_rows: list[tuple[Any, ...]],
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if "instant" not in tables:
            return [], ["Trace DB 缺少 instant，无法恢复 Sleep 唤醒链"]
        sleep_intervals = cls._state_intervals(
            state_rows,
            wanted_states={"S"},
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
        )
        if not sleep_intervals:
            return [], []
        initial = max(sleep_intervals, key=lambda item: item[1] - item[0])
        chain: list[dict[str, Any]] = []
        limitations: list[str] = []
        visited = {waiting_itid}
        current_itid = waiting_itid
        current_sleep = initial
        for depth in range(1, 6):
            sleep_start, sleep_end = current_sleep
            waiting_identity = cls._identity_or_none(
                connection, current_itid
            )
            enclosing_slice = cls._enclosing_slice(
                connection,
                tables=tables,
                itid=current_itid,
                timestamp_ns=max(sleep_start, sleep_end - 1),
            )
            wakeup = connection.execute(
                "SELECT ts, wakeup_from FROM instant "
                "WHERE name IN ('sched_waking', 'sched_wakeup') "
                "AND ref = ? AND ts >= ? AND ts <= ? "
                "ORDER BY ABS(ts - ?), "
                "CASE WHEN name = 'sched_wakeup' THEN 0 ELSE 1 END LIMIT 1",
                (
                    current_itid,
                    sleep_end - 1_000_000,
                    sleep_end + 1_000_000,
                    sleep_end,
                ),
            ).fetchone()
            if wakeup is None:
                chain.append(
                    {
                        "depth": depth,
                        "sleep_start_ns": sleep_start,
                        "sleep_end_ns": sleep_end,
                        "waiting_itid": current_itid,
                        "waiting_thread": (
                            waiting_identity.get("thread_name")
                            if waiting_identity is not None
                            else None
                        ),
                        "enclosing_slice": enclosing_slice,
                        "wakeup_ns": None,
                        "waker_itid": None,
                        "waker_process": None,
                        "waker_thread": None,
                        "waker_slice": None,
                        "confidence": 0.35,
                        "evidence_ids": [],
                    }
                )
                break

            wakeup_ns = int(wakeup[0])
            waker_itid = (
                int(wakeup[1]) if wakeup[1] is not None else None
            )
            waker_identity = (
                cls._identity_or_none(connection, waker_itid)
                if waker_itid is not None
                else None
            )
            waker_slice = (
                cls._enclosing_slice(
                    connection,
                    tables=tables,
                    itid=waker_itid,
                    timestamp_ns=wakeup_ns,
                )
                if waker_itid is not None
                else None
            )
            if waker_itid is not None and waker_itid in visited:
                limitations.append(
                    "唤醒关系返回已访问线程，已在形成循环前截断 Wakeup Chain"
                )
                break
            chain.append(
                {
                    "depth": depth,
                    "sleep_start_ns": sleep_start,
                    "sleep_end_ns": sleep_end,
                    "waiting_itid": current_itid,
                    "waiting_thread": (
                        waiting_identity.get("thread_name")
                        if waiting_identity is not None
                        else None
                    ),
                    "enclosing_slice": enclosing_slice,
                    "wakeup_ns": wakeup_ns,
                    "waker_itid": waker_itid,
                    "waker_process": (
                        waker_identity.get("process_name")
                        if waker_identity is not None
                        else None
                    ),
                    "waker_thread": (
                        waker_identity.get("thread_name")
                        if waker_identity is not None
                        else None
                    ),
                    "waker_slice": waker_slice,
                    "confidence": (
                        0.9
                        if abs(wakeup_ns - sleep_end) <= 100_000
                        else 0.7
                    ),
                    "evidence_ids": [],
                }
            )
            if waker_itid is None:
                break
            prior_sleep = cls._preceding_sleep(
                connection,
                itid=waker_itid,
                timestamp_ns=wakeup_ns,
            )
            if prior_sleep is None:
                break
            clipped_prior_sleep = (
                max(prior_sleep[0], interval_start_ns),
                min(prior_sleep[1], interval_end_ns),
            )
            if clipped_prior_sleep[1] <= clipped_prior_sleep[0]:
                limitations.append(
                    "上游唤醒者的前序 Sleep 位于分析窗口之外，"
                    "已停止继续扩展 Wakeup Chain"
                )
                break
            if clipped_prior_sleep != prior_sleep:
                limitations.append(
                    "上游唤醒者的前序 Sleep 跨越分析窗口边界，"
                    "Wakeup Chain 已按精确父窗口裁剪"
                )
            visited.add(waker_itid)
            current_itid = waker_itid
            current_sleep = clipped_prior_sleep
        return chain, list(dict.fromkeys(limitations))

    @classmethod
    def _contention_intervals(
        cls,
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        target_itid: int,
        state_rows: list[tuple[Any, ...]],
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> list[dict[str, Any]]:
        if not {"sched_slice", "thread", "process"} <= tables:
            return []
        runnable = cls._state_intervals(
            state_rows,
            wanted_states={"R", "R+"},
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
        )
        results: list[dict[str, Any]] = []
        for start_ns, end_ns in sorted(
            runnable,
            key=lambda item: -(item[1] - item[0]),
        )[:5]:
            competitor = connection.execute(
                "SELECT s.itid, s.cpu, s.priority, t.name, p.name, "
                "SUM(MIN(s.ts + s.dur, ?) - MAX(s.ts, ?)) AS overlap_ns "
                "FROM sched_slice AS s "
                "LEFT JOIN thread AS t ON t.itid = s.itid "
                "LEFT JOIN process AS p ON p.ipid = t.ipid "
                "WHERE s.itid != ? AND s.ts < ? AND s.ts + s.dur > ? "
                "GROUP BY s.itid, s.cpu, s.priority, t.name, p.name "
                "ORDER BY overlap_ns DESC LIMIT 1",
                (end_ns, start_ns, target_itid, end_ns, start_ns),
            ).fetchone()
            results.append(
                {
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "duration_ms": (end_ns - start_ns) / 1_000_000.0,
                    "cpu": (
                        int(competitor[1])
                        if competitor is not None
                        and competitor[1] is not None
                        else None
                    ),
                    "target_priority": None,
                    "competing_process": (
                        str(competitor[4])
                        if competitor is not None and competitor[4]
                        else None
                    ),
                    "competing_thread": (
                        str(competitor[3])
                        if competitor is not None and competitor[3]
                        else None
                    ),
                    "competing_priority": (
                        int(competitor[2])
                        if competitor is not None
                        and competitor[2] is not None
                        else None
                    ),
                    "assessment": (
                        "Runnable 区间内观察到并发 CPU 执行者；"
                        "缺少 CPU 亲和性/可运行队列语义时不得直接认定其阻塞目标线程"
                    ),
                    "confidence": 0.55 if competitor is not None else 0.3,
                    "evidence_ids": [],
                }
            )
        return results

    @staticmethod
    def _state_intervals(
        rows: list[tuple[Any, ...]],
        *,
        wanted_states: set[str],
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> list[tuple[int, int]]:
        intervals: list[tuple[int, int]] = []
        covered_until = interval_start_ns
        for raw_ts, raw_dur, raw_state in rows:
            start = max(int(raw_ts), interval_start_ns, covered_until)
            end = min(int(raw_ts) + int(raw_dur), interval_end_ns)
            if end <= start:
                continue
            covered_until = end
            if str(raw_state or "") in wanted_states:
                intervals.append((start, end))
        return intervals

    @classmethod
    def _preceding_sleep(
        cls,
        connection: sqlite3.Connection,
        *,
        itid: int,
        timestamp_ns: int,
    ) -> tuple[int, int] | None:
        row = connection.execute(
            "SELECT ts, dur FROM thread_state WHERE itid = ? "
            "AND state = 'S' AND ts + dur >= ? AND ts + dur <= ? "
            "ORDER BY ABS(ts + dur - ?) LIMIT 1",
            (
                itid,
                timestamp_ns - 1_000_000,
                timestamp_ns + 1_000_000,
                timestamp_ns,
            ),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[0]) + int(row[1])

    @classmethod
    def _identity_or_none(
        cls,
        connection: sqlite3.Connection,
        itid: int,
    ) -> dict[str, Any] | None:
        try:
            return cls._identity(connection, itid)
        except ValueError:
            return None

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
            "SELECT name FROM callstack WHERE callid = ? "
            "AND ts <= ? AND ts + MAX(dur, 0) >= ? "
            "ORDER BY depth DESC, dur ASC, id DESC LIMIT 1",
            (itid, timestamp_ns, timestamp_ns),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    @staticmethod
    def _state_bucket(raw_state: Any) -> str:
        state = str(raw_state or "")
        if state == "Running":
            return "running_ms"
        if state in {"R", "R+"}:
            return "runnable_ms"
        if state == "S":
            return "sleeping_ms"
        if state == "D-IO":
            return "uninterruptible_io_ms"
        if state in {"D", "D-NIO"}:
            return "uninterruptible_other_ms"
        return "other_ms"

    @classmethod
    def _aggregate_states(
        cls,
        rows: list[tuple[Any, ...]],
        *,
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> tuple[dict[str, float], dict[str, float], list[str]]:
        buckets_ns = {
            "running_ms": 0,
            "runnable_ms": 0,
            "sleeping_ms": 0,
            "uninterruptible_io_ms": 0,
            "uninterruptible_other_ms": 0,
            "other_ms": 0,
        }
        longest_ns = {
            "running_ms": 0,
            "runnable_ms": 0,
            "sleeping_ms": 0,
        }
        limitations: list[str] = []
        covered_until = interval_start_ns
        overlap_detected = False
        for raw_ts, raw_dur, raw_state in rows:
            start = max(int(raw_ts), interval_start_ns)
            end = min(int(raw_ts) + int(raw_dur), interval_end_ns)
            if end <= start:
                continue
            if start < covered_until:
                overlap_detected = True
                start = covered_until
            if end <= start:
                continue
            covered_until = max(covered_until, end)
            duration = end - start
            bucket = cls._state_bucket(raw_state)
            buckets_ns[bucket] += duration
            if bucket in longest_ns:
                longest_ns[bucket] = max(longest_ns[bucket], duration)
        if overlap_detected:
            limitations.append(
                "thread_state 存在重叠行，确定性投影按时间顺序去重"
            )
        return (
            {
                name: value / 1_000_000.0
                for name, value in buckets_ns.items()
            },
            {
                name: value / 1_000_000.0
                for name, value in longest_ns.items()
            },
            limitations,
        )

    @staticmethod
    def _aggregate_scheduling(
        rows: list[tuple[Any, ...]],
        *,
        interval_start_ns: int,
        interval_end_ns: int,
    ) -> dict[str, Any]:
        cpu_totals: dict[int, dict[str, int]] = defaultdict(
            lambda: {"running_ns": 0, "schedule_slices": 0}
        )
        priority_totals: dict[int, int] = defaultdict(int)
        previous_cpu: int | None = None
        migrations = 0
        schedule_slices = 0
        for raw_ts, raw_dur, raw_cpu, raw_priority in rows:
            start = max(int(raw_ts), interval_start_ns)
            end = min(int(raw_ts) + int(raw_dur), interval_end_ns)
            if end <= start:
                continue
            duration = end - start
            cpu = int(raw_cpu)
            cpu_totals[cpu]["running_ns"] += duration
            cpu_totals[cpu]["schedule_slices"] += 1
            schedule_slices += 1
            if previous_cpu is not None and cpu != previous_cpu:
                migrations += 1
            previous_cpu = cpu
            if raw_priority is not None:
                priority_totals[int(raw_priority)] += duration

        total_running_ns = sum(
            item["running_ns"] for item in cpu_totals.values()
        )
        distribution = [
            {
                "cpu": cpu,
                "running_ms": values["running_ns"] / 1_000_000.0,
                "share": (
                    values["running_ns"] / total_running_ns
                    if total_running_ns
                    else 0
                ),
                "schedule_slices": values["schedule_slices"],
            }
            for cpu, values in sorted(
                cpu_totals.items(),
                key=lambda item: -item[1]["running_ns"],
            )
        ]
        observed_priorities = [
            priority for priority, _ in sorted(
                priority_totals.items(),
                key=lambda item: -item[1],
            )
        ]
        return {
            "cpu_distribution": distribution,
            "cpu_migrations": migrations,
            "schedule_slices": schedule_slices,
            "priority": {
                "observed_values": observed_priorities,
                "dominant_value": (
                    observed_priorities[0] if observed_priorities else None
                ),
                "interpretation": (
                    "Trace 原始优先级值；未建立平台排序和调度策略语义"
                ),
            },
        }

    @staticmethod
    def _diagnosis(
        *,
        interval_ms: float,
        states: dict[str, float],
    ) -> tuple[str, str]:
        if interval_ms <= 0:
            return "inconclusive", "分析区间为空"
        running_share = states["running_ms"] / interval_ms
        runnable_share = states["runnable_ms"] / interval_ms
        sleep_share = states["sleeping_ms"] / interval_ms
        io_share = states["uninterruptible_io_ms"] / interval_ms
        if running_share >= 0.7:
            diagnosis = "cpu-bound"
        elif io_share >= 0.2:
            diagnosis = "io-wait"
        elif runnable_share >= 0.2:
            diagnosis = "cpu-contention"
        elif sleep_share >= 0.3:
            diagnosis = "blocked-wait"
        elif running_share + runnable_share + sleep_share >= 0.8:
            diagnosis = "mixed"
        else:
            diagnosis = "inconclusive"
        assessment = (
            f"窗口内 Running={running_share:.1%}、"
            f"Runnable={runnable_share:.1%}、Sleep={sleep_share:.1%}、"
            f"D-IO={io_share:.1%}；阻塞和调度因果仍需独立 Evidence。"
        )
        return diagnosis, assessment
