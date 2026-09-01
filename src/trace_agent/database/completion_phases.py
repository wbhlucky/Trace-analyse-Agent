from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from time import monotonic
from typing import Any

from trace_agent.database.thread_execution import (
    ThreadExecutionRepository,
)


def summarize_completion_latency_phases(data: dict[str, Any]) -> str:
    """Build the stable user-facing summary for one phase projection."""
    phases = data.get("phases") or []
    profile_count = sum(
        len(phase.get("thread_profiles") or [])
        for phase in phases
        if isinstance(phase, dict)
    )
    long_frame_count = sum(
        int((phase.get("frames") or {}).get("long_frame_count") or 0)
        for phase in phases
        if isinstance(phase, dict)
    )
    perf_scope = data.get("recommended_perf_scope") or {}
    thread_ids = perf_scope.get("thread_ids") or []
    return (
        "已完成完成时延精确阶段取证："
        f"{len(phases)} 个阶段、"
        f"{profile_count} 个关键线程投影、"
        f"{long_frame_count} 个 30ms 以上帧；"
        f"建议 Perf TID 数={len(thread_ids)}"
    )


class CompletionLatencyPhaseRepository:
    """Collect deterministic evidence after latency boundaries are chosen.

    Boundary selection deliberately stays outside this repository.  The
    repository only projects exact, non-overlapping phases and screens the
    application/render threads that deserve deeper analysis.
    """

    _LONG_FRAME_NS = 30_000_000

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 20,
    ) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Trace 数据库不存在：{database_path}"
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._database_path = database_path.resolve()
        self._timeout_seconds = timeout_seconds

    def inspect(
        self,
        *,
        target_ipid: int,
        input_ns: int,
        response_ns: int | None,
        completion_ns: int | None,
        max_threads_per_phase: int = 4,
        max_slices_per_phase: int = 12,
        max_frames_per_phase: int = 8,
    ) -> dict[str, Any]:
        self._validate_boundaries(
            target_ipid=target_ipid,
            input_ns=input_ns,
            response_ns=response_ns,
            completion_ns=completion_ns,
        )
        if not 1 <= max_threads_per_phase <= 8:
            raise ValueError("max_threads_per_phase 必须在 1 到 8 之间")
        if not 1 <= max_slices_per_phase <= 30:
            raise ValueError("max_slices_per_phase 必须在 1 到 30 之间")
        if not 1 <= max_frames_per_phase <= 30:
            raise ValueError("max_frames_per_phase 必须在 1 到 30 之间")

        phases = self._phases(
            input_ns=input_ns,
            response_ns=response_ns,
            completion_ns=completion_ns,
        )
        outer_end_ns = max(phase[2] for phase in phases)
        deadline = [monotonic() + self._timeout_seconds]

        def refresh_query_deadline() -> None:
            deadline[0] = monotonic() + self._timeout_seconds

        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.row_factory = sqlite3.Row
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline[0]),
                    10_000,
                )
                refresh_query_deadline()
                tables = self._tables(connection)
                refresh_query_deadline()
                process = self._process(connection, target_ipid)
                refresh_query_deadline()
                main_thread = self._main_thread(
                    connection,
                    target_ipid=target_ipid,
                    pid=process["pid"],
                )
                refresh_query_deadline()
                render_threads = self._mapped_render_threads(
                    connection,
                    tables=tables,
                    target_ipid=target_ipid,
                    start_ns=input_ns,
                    end_ns=outer_end_ns,
                )
                render_itids = {
                    item["itid"] for item in render_threads
                }

                phase_results: list[dict[str, Any]] = []
                recommended_tids: set[int] = set()
                for name, start_ns, end_ns in phases:
                    refresh_query_deadline()
                    rankings = self._rank_threads(
                        connection,
                        target_ipid=target_ipid,
                        extra_itids=render_itids,
                        start_ns=start_ns,
                        end_ns=end_ns,
                    )
                    selected = self._select_threads(
                        rankings,
                        main_itid=(
                            main_thread.get("itid")
                            if main_thread is not None
                            else None
                        ),
                        render_itids=render_itids,
                        limit=max_threads_per_phase,
                    )
                    profiles = self._thread_profiles(
                        selected,
                        start_ns=start_ns,
                        end_ns=end_ns,
                    )
                    phase_tids = sorted(
                        {
                            item["thread_execution"]["tid"]
                            for item in profiles
                            if item["thread_execution"][
                                "state_breakdown"
                            ]["running_ms"]
                            > 0
                        }
                    )
                    if not phase_tids and main_thread is not None:
                        phase_tids = [main_thread["tid"]]
                    recommended_tids.update(phase_tids)
                    refresh_query_deadline()
                    slice_hotspots = self._slice_hotspots(
                        connection,
                        tables=tables,
                        target_ipid=target_ipid,
                        extra_itids=render_itids,
                        start_ns=start_ns,
                        end_ns=end_ns,
                        limit=max_slices_per_phase,
                    )
                    refresh_query_deadline()
                    frames = self._frame_summary(
                        connection,
                        tables=tables,
                        target_ipid=target_ipid,
                        start_ns=start_ns,
                        end_ns=end_ns,
                        limit=max_frames_per_phase,
                    )
                    phase_results.append(
                        {
                            "name": name,
                            "start_ns": start_ns,
                            "end_ns": end_ns,
                            "duration_ms": (
                                end_ns - start_ns
                            )
                            / 1_000_000.0,
                            "thread_rankings": rankings[:20],
                            "thread_profiles": profiles,
                            "slice_hotspots": slice_hotspots,
                            "frames": frames,
                            "recommended_perf_thread_ids": phase_tids,
                        }
                    )
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise RuntimeError("完成时延阶段取证超时") from exc
            raise RuntimeError(f"完成时延阶段取证失败：{exc}") from exc

        limitations: list[str] = []
        if "callstack" not in tables:
            limitations.append("Trace DB 缺少 callstack，无法统计阶段 Slice")
        if "frame_slice" not in tables:
            limitations.append("Trace DB 缺少 frame_slice，无法统计阶段帧")
        if not render_threads:
            limitations.append(
                "未通过 frame_maps 发现与应用帧关联的 RenderService 线程"
            )

        return {
            "target_process": process,
            "main_thread": main_thread,
            "input_ns": input_ns,
            "response_ns": response_ns,
            "completion_ns": completion_ns,
            "render_threads": render_threads,
            "phases": phase_results,
            "recommended_perf_scope": {
                "thread_ids": sorted(recommended_tids),
                "process_ids": [],
                "interval_start_ns": input_ns,
                "interval_end_ns": outer_end_ns,
                "rule": (
                    "Only phase-selected application and frame-mapped "
                    "render threads with observed Running time"
                ),
            },
            "selection_policy": [
                "Compare absolute Running time before percentages.",
                "Keep the application main thread as pipeline context.",
                "Add the highest Runnable/D-state application thread when "
                "it differs from the CPU-running thread.",
                "Add RenderService threads only when frame_maps ties them "
                "to an application frame in the selected interval.",
                "Slice overlap is inclusive and may contain nested time; it "
                "is evidence, not an additive latency decomposition.",
                "Thirty milliseconds is only the large-frame screening "
                "threshold, not the business completion definition.",
            ],
            "limitations": limitations,
        }

    @staticmethod
    def _validate_boundaries(
        *,
        target_ipid: int,
        input_ns: int,
        response_ns: int | None,
        completion_ns: int | None,
    ) -> None:
        if target_ipid <= 0:
            raise ValueError("target_ipid 必须是已确认应用的非零 ipid")
        if input_ns < 0:
            raise ValueError("input_ns 必须是非负整数")
        if response_ns is None and completion_ns is None:
            raise ValueError("response_ns 和 completion_ns 至少提供一个")
        if response_ns is not None and response_ns <= input_ns:
            raise ValueError("response_ns 必须晚于 input_ns")
        if completion_ns is not None and completion_ns <= input_ns:
            raise ValueError("completion_ns 必须晚于 input_ns")
        if (
            response_ns is not None
            and completion_ns is not None
            and response_ns > completion_ns
        ):
            raise ValueError("response_ns 不得晚于 completion_ns")

    @staticmethod
    def _phases(
        *,
        input_ns: int,
        response_ns: int | None,
        completion_ns: int | None,
    ) -> list[tuple[str, int, int]]:
        phases: list[tuple[str, int, int]] = []
        if response_ns is not None:
            phases.append(("response", input_ns, response_ns))
        if completion_ns is not None:
            if response_ns is not None and completion_ns > response_ns:
                phases.append(
                    ("post-response", response_ns, completion_ns)
                )
            elif response_ns is None:
                phases.append(("completion", input_ns, completion_ns))
        return phases

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
    def _process(
        connection: sqlite3.Connection,
        target_ipid: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT ipid, pid, name FROM process WHERE ipid = ? LIMIT 1",
            (target_ipid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"未找到 target_ipid={target_ipid} 的进程")
        return {
            "ipid": int(row["ipid"]),
            "pid": int(row["pid"]),
            "name": str(row["name"] or "unknown"),
        }

    @staticmethod
    def _main_thread(
        connection: sqlite3.Connection,
        *,
        target_ipid: int,
        pid: int,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT itid, tid, name FROM thread "
            "WHERE ipid = ? "
            "ORDER BY CASE WHEN tid = ? THEN 0 "
            "WHEN LOWER(COALESCE(name, '')) = 'main' THEN 1 ELSE 2 END, "
            "itid LIMIT 1",
            (target_ipid, pid),
        ).fetchone()
        if row is None:
            return None
        return {
            "itid": int(row["itid"]),
            "tid": int(row["tid"]),
            "name": str(row["name"] or "unknown"),
        }

    @staticmethod
    def _mapped_render_threads(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        target_ipid: int,
        start_ns: int,
        end_ns: int,
    ) -> list[dict[str, Any]]:
        required = {"frame_slice", "frame_maps", "thread", "process"}
        if not required <= tables:
            return []
        frame_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(frame_slice)")
        }
        type_filter = (
            " AND LOWER(COALESCE(app_frame.type_desc, '')) "
            "IN ('actual', 'actural')"
            if "type_desc" in frame_columns
            else ""
        )
        rows = connection.execute(
            "SELECT DISTINCT t.itid, t.tid, t.name AS thread_name, "
            "p.ipid, p.pid, p.name AS process_name "
            "FROM frame_slice AS app_frame "
            "JOIN frame_maps AS fm ON fm.src_row = app_frame.id "
            "JOIN frame_slice AS render_frame ON render_frame.id = fm.dst_row "
            "JOIN thread AS t ON t.itid = render_frame.itid "
            "JOIN process AS p ON p.ipid = t.ipid "
            "WHERE app_frame.ipid = ? AND app_frame.ts < ? "
            f"AND app_frame.ts + app_frame.dur > ?{type_filter} "
            "ORDER BY p.ipid, t.itid",
            (target_ipid, end_ns, start_ns),
        ).fetchall()
        return [
            {
                "itid": int(row["itid"]),
                "tid": int(row["tid"]),
                "thread_name": str(row["thread_name"] or "unknown"),
                "ipid": int(row["ipid"]),
                "pid": int(row["pid"]),
                "process_name": str(row["process_name"] or "unknown"),
                "selection_source": "frame_maps",
            }
            for row in rows
        ]

    @classmethod
    def _rank_threads(
        cls,
        connection: sqlite3.Connection,
        *,
        target_ipid: int,
        extra_itids: set[int],
        start_ns: int,
        end_ns: int,
    ) -> list[dict[str, Any]]:
        extra = sorted(extra_itids)
        placeholders = ",".join("?" for _ in extra)
        extra_clause = (
            f" OR t.itid IN ({placeholders})" if extra else ""
        )
        rows = connection.execute(
            "SELECT t.itid, t.tid, t.name AS thread_name, "
            "p.ipid, p.pid, p.name AS process_name, "
            "s.ts, s.dur, s.state "
            "FROM thread AS t JOIN process AS p ON p.ipid = t.ipid "
            "LEFT JOIN thread_state AS s ON s.itid = t.itid "
            "AND s.ts < ? AND s.ts + s.dur > ? "
            f"WHERE t.ipid = ?{extra_clause} "
            "ORDER BY t.itid, s.ts, s.id",
            (end_ns, start_ns, target_ipid, *extra),
        ).fetchall()
        identities: dict[int, dict[str, Any]] = {}
        state_rows: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
        for row in rows:
            itid = int(row["itid"])
            identities[itid] = {
                "itid": itid,
                "tid": int(row["tid"]),
                "thread_name": str(row["thread_name"] or "unknown"),
                "ipid": int(row["ipid"]),
                "pid": int(row["pid"]),
                "process_name": str(row["process_name"] or "unknown"),
                "role": (
                    "application"
                    if int(row["ipid"]) == target_ipid
                    else "frame-mapped-render"
                ),
            }
            if row["ts"] is not None and row["dur"] is not None:
                state_rows[itid].append(
                    (int(row["ts"]), int(row["dur"]), str(row["state"] or ""))
                )

        rankings: list[dict[str, Any]] = []
        for itid, identity in identities.items():
            buckets = cls._state_totals(
                state_rows.get(itid, []),
                start_ns=start_ns,
                end_ns=end_ns,
            )
            active_ms = sum(
                buckets[name]
                for name in (
                    "running_ms",
                    "runnable_ms",
                    "uninterruptible_io_ms",
                    "uninterruptible_other_ms",
                )
            )
            rankings.append(
                {
                    **identity,
                    "state_breakdown": buckets,
                    "absolute_active_ms": active_ms,
                }
            )
        rankings.sort(
            key=lambda item: (
                -item["state_breakdown"]["running_ms"],
                -item["absolute_active_ms"],
                item["itid"],
            )
        )
        return rankings

    @staticmethod
    def _state_totals(
        rows: list[tuple[int, int, str]],
        *,
        start_ns: int,
        end_ns: int,
    ) -> dict[str, float]:
        totals = {
            "running_ms": 0.0,
            "runnable_ms": 0.0,
            "sleeping_ms": 0.0,
            "uninterruptible_io_ms": 0.0,
            "uninterruptible_other_ms": 0.0,
            "other_ms": 0.0,
        }
        covered_until = start_ns
        for ts, dur, raw_state in rows:
            clipped_start = max(ts, start_ns, covered_until)
            clipped_end = min(ts + dur, end_ns)
            if clipped_end <= clipped_start:
                continue
            covered_until = clipped_end
            bucket = ThreadExecutionRepository._state_bucket(raw_state)
            totals[bucket] += (clipped_end - clipped_start) / 1_000_000.0
        return totals

    @staticmethod
    def _select_threads(
        rankings: list[dict[str, Any]],
        *,
        main_itid: int | None,
        render_itids: set[int],
        limit: int,
    ) -> list[dict[str, Any]]:
        by_itid = {item["itid"]: item for item in rankings}
        selected: dict[int, dict[str, Any]] = {}
        reasons: dict[int, list[str]] = defaultdict(list)

        def add(item: dict[str, Any] | None, reason: str) -> None:
            if item is None:
                return
            itid = item["itid"]
            if len(selected) < limit or itid in selected:
                selected[itid] = item
                if reason not in reasons[itid]:
                    reasons[itid].append(reason)

        add(by_itid.get(main_itid), "application-main-thread")
        app_threads = [
            item for item in rankings if item["role"] == "application"
        ]
        add(app_threads[0] if app_threads else None, "highest-absolute-running")
        wait_ranked = sorted(
            app_threads,
            key=lambda item: -sum(
                item["state_breakdown"][name]
                for name in (
                    "runnable_ms",
                    "uninterruptible_io_ms",
                    "uninterruptible_other_ms",
                )
            ),
        )
        add(
            wait_ranked[0] if wait_ranked else None,
            "highest-runnable-or-d-state",
        )
        render_ranked = [
            item for item in rankings if item["itid"] in render_itids
        ]
        add(
            render_ranked[0] if render_ranked else None,
            "frame-mapped-render-thread",
        )
        for item in rankings:
            add(item, "next-highest-absolute-active-time")
            if len(selected) >= limit:
                break
        return [
            {**item, "selection_reasons": reasons[item["itid"]]}
            for item in selected.values()
        ]

    def _thread_profiles(
        self,
        selected: list[dict[str, Any]],
        *,
        start_ns: int,
        end_ns: int,
    ) -> list[dict[str, Any]]:
        repository = ThreadExecutionRepository(self._database_path)
        profiles: list[dict[str, Any]] = []
        for item in selected:
            data = repository.inspect(
                interval_start_ns=start_ns,
                interval_end_ns=end_ns,
                itid=item["itid"],
            )
            profiles.append(
                {
                    "selection_reasons": item["selection_reasons"],
                    "absolute_running_ms": item["state_breakdown"][
                        "running_ms"
                    ],
                    "absolute_active_ms": item["absolute_active_ms"],
                    "thread_execution": data["thread_execution"],
                    "limitations": data.get("limitations", []),
                }
            )
        return profiles

    @staticmethod
    def _slice_hotspots(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        target_ipid: int,
        extra_itids: set[int],
        start_ns: int,
        end_ns: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not {"callstack", "thread", "process"} <= tables:
            return []
        extra = sorted(extra_itids)
        placeholders = ",".join("?" for _ in extra)
        extra_clause = (
            f" OR t.itid IN ({placeholders})" if extra else ""
        )
        rows = connection.execute(
            "SELECT p.name AS process_name, t.itid, t.tid, "
            "t.name AS thread_name, c.name, MIN(c.depth) AS min_depth, "
            "COUNT(*) AS occurrences, "
            "SUM(MIN(c.ts + c.dur, ?) - MAX(c.ts, ?)) AS overlap_ns, "
            "MAX(MIN(c.ts + c.dur, ?) - MAX(c.ts, ?)) AS max_overlap_ns "
            "FROM callstack AS c JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
            "WHERE c.dur > 0 AND c.ts < ? AND c.ts + c.dur > ? "
            f"AND (t.ipid = ?{extra_clause}) "
            "GROUP BY p.name, t.itid, t.tid, t.name, c.name "
            "ORDER BY overlap_ns DESC, max_overlap_ns DESC LIMIT ?",
            (
                end_ns,
                start_ns,
                end_ns,
                start_ns,
                end_ns,
                start_ns,
                target_ipid,
                *extra,
                limit,
            ),
        ).fetchall()
        phase_ns = end_ns - start_ns
        return [
            {
                "process_name": str(row["process_name"] or "unknown"),
                "itid": int(row["itid"]),
                "tid": int(row["tid"]),
                "thread_name": str(row["thread_name"] or "unknown"),
                "name": str(row["name"] or "unknown"),
                "min_depth": int(row["min_depth"] or 0),
                "occurrences": int(row["occurrences"]),
                "inclusive_overlap_ms": int(row["overlap_ns"]) / 1_000_000.0,
                "max_overlap_ms": int(row["max_overlap_ns"]) / 1_000_000.0,
                "interval_wrapper_candidate": (
                    int(row["max_overlap_ns"]) >= phase_ns * 0.8
                    and int(row["min_depth"] or 0) <= 1
                ),
            }
            for row in rows
        ]

    @classmethod
    def _frame_summary(
        cls,
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        target_ipid: int,
        start_ns: int,
        end_ns: int,
        limit: int,
    ) -> dict[str, Any]:
        if "frame_slice" not in tables:
            return {
                "available": False,
                "frame_count": 0,
                "long_frame_count": 0,
                "long_frames": [],
                "mapped_presentations": [],
            }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(frame_slice)")
        }
        type_filter = (
            " AND LOWER(COALESCE(type_desc, '')) IN ('actual', 'actural')"
            if "type_desc" in columns
            else ""
        )
        aggregate = connection.execute(
            "SELECT COUNT(*) AS frame_count, "
            "COALESCE(AVG(dur), 0) AS avg_dur, "
            "COALESCE(MAX(dur), 0) AS max_dur, "
            "SUM(CASE WHEN dur >= ? THEN 1 ELSE 0 END) AS long_count "
            "FROM frame_slice WHERE ipid = ? AND ts < ? "
            f"AND ts + dur > ?{type_filter}",
            (cls._LONG_FRAME_NS, target_ipid, end_ns, start_ns),
        ).fetchone()
        long_rows = connection.execute(
            "SELECT id, ts, dur, vsync, itid "
            "FROM frame_slice WHERE ipid = ? AND ts < ? "
            f"AND ts + dur > ?{type_filter} AND dur >= ? "
            "ORDER BY dur DESC, ts LIMIT ?",
            (
                target_ipid,
                end_ns,
                start_ns,
                cls._LONG_FRAME_NS,
                limit,
            ),
        ).fetchall()
        mapped: list[dict[str, Any]] = []
        if "frame_maps" in tables:
            mapped_rows = connection.execute(
                "SELECT app.id AS app_frame_id, app.vsync AS app_vsync, "
                "render.id AS render_frame_id, render.ts, render.dur, "
                "render.itid, t.tid, t.name AS thread_name, "
                "p.name AS process_name "
                "FROM frame_slice AS app "
                "JOIN frame_maps AS fm ON fm.src_row = app.id "
                "JOIN frame_slice AS render ON render.id = fm.dst_row "
                "LEFT JOIN thread AS t ON t.itid = render.itid "
                "LEFT JOIN process AS p ON p.ipid = t.ipid "
                "WHERE app.ipid = ? AND app.ts < ? "
                f"AND app.ts + app.dur > ?{type_filter.replace('type_desc', 'app.type_desc')} "
                "ORDER BY app.ts, render.ts LIMIT ?",
                (target_ipid, end_ns, start_ns, limit),
            ).fetchall()
            mapped = [
                {
                    "app_frame_source_id": f"frame_slice:{row['app_frame_id']}",
                    "app_vsync": row["app_vsync"],
                    "render_frame_source_id": f"frame_slice:{row['render_frame_id']}",
                    "presentation_start_ns": int(row["ts"]),
                    "presentation_end_ns": int(row["ts"]) + int(row["dur"]),
                    "render_itid": (
                        int(row["itid"]) if row["itid"] is not None else None
                    ),
                    "render_tid": (
                        int(row["tid"]) if row["tid"] is not None else None
                    ),
                    "render_thread": row["thread_name"],
                    "render_process": row["process_name"],
                }
                for row in mapped_rows
            ]
        return {
            "available": True,
            "frame_count": int(aggregate["frame_count"] or 0),
            "average_duration_ms": float(aggregate["avg_dur"]) / 1_000_000.0,
            "maximum_duration_ms": float(aggregate["max_dur"]) / 1_000_000.0,
            "long_frame_threshold_ms": cls._LONG_FRAME_NS / 1_000_000.0,
            "long_frame_count": int(aggregate["long_count"] or 0),
            "long_frames": [
                {
                    "source_id": f"frame_slice:{row['id']}",
                    "start_ns": int(row["ts"]),
                    "end_ns": int(row["ts"]) + int(row["dur"]),
                    "duration_ms": int(row["dur"]) / 1_000_000.0,
                    "vsync": row["vsync"],
                    "itid": row["itid"],
                }
                for row in long_rows
            ],
            "mapped_presentations": mapped,
        }
