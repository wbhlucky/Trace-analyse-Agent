from __future__ import annotations

from pathlib import Path
from typing import Any

from trace_agent.database.repository import SQLiteTraceRepository


class ColdStartRepository:
    """Read bounded cold-start candidate facts from Trace Streamer SQLite."""

    _TABLES = (
        "app_startup",
        "callstack",
        "data_dict",
        "frame_maps",
        "frame_slice",
        "instant",
        "process",
        "thread",
        "trace_range",
    )

    def __init__(self, database_path: Path) -> None:
        self._repository = SQLiteTraceRepository(database_path)

    def inspect(
        self,
        *,
        target_process: str | None,
        max_candidates: int,
    ) -> dict[str, Any]:
        if not 1 <= max_candidates <= 50:
            raise ValueError("max_candidates 必须在 1 到 50 之间")
        hint = (target_process or "").strip()
        tables = self._read_tables()
        trace_range = self._read_trace_range(tables)
        processes = self._read_processes(
            tables,
            hint=hint,
            max_candidates=max_candidates,
        )
        self._annotate_process_start(processes, trace_range)
        candidate_ipids = [
            int(process["ipid"])
            for process in processes
            if isinstance(process.get("ipid"), int)
        ]
        startup_stage_count, startup_stages = (
            self._read_startup_stages(
                tables,
                hint=hint,
                max_rows=min(max_candidates * 10, 500),
            )
        )
        first_frames = self._read_first_frames(
            tables,
            candidate_ipids=candidate_ipids,
            max_candidates=max_candidates,
        )
        earliest_slices = self._read_earliest_slices(
            tables,
            candidate_ipids=candidate_ipids,
            max_rows=min(max_candidates * 3, 150),
        )

        limitations: list[str] = []
        if "app_startup" not in tables:
            limitations.append("Trace DB 不包含 app_startup 表")
        elif startup_stage_count == 0:
            limitations.append(
                "app_startup 表存在但没有记录启动阶段"
            )
        elif hint and not startup_stages:
            limitations.append(
                "app_startup 有记录，但没有阶段与目标应用提示匹配"
            )
        if not processes:
            limitations.append(
                "没有找到与进程提示匹配的候选进程"
                if hint
                else "没有读取到候选进程"
            )
        if "frame_slice" not in tables:
            limitations.append("Trace DB 不包含 frame_slice 表")
        elif not first_frames:
            limitations.append(
                "没有找到与候选范围匹配的首帧记录"
            )

        return {
            "target_process_hint": hint or None,
            "table_presence": {
                table: table in tables for table in self._TABLES
            },
            "trace_range": trace_range,
            "process_candidates": processes,
            "app_startup": {
                "table_available": "app_startup" in tables,
                "row_count": startup_stage_count,
                "matched_row_count": len(startup_stages),
                "stages": startup_stages,
            },
            "first_frame_candidates": first_frames,
            "earliest_slice_candidates": earliest_slices,
            "limitations": limitations,
        }

    def inspect_timeline(
        self,
        *,
        target_ipid: int,
        lookback_ms: int,
        lookahead_ms: int,
        max_events: int,
    ) -> dict[str, Any]:
        if target_ipid < 0:
            raise ValueError("target_ipid 必须是非负整数")
        if not 0 <= lookback_ms <= 60_000:
            raise ValueError("lookback_ms 必须在 0 到 60000 之间")
        if not 1 <= lookahead_ms <= 120_000:
            raise ValueError("lookahead_ms 必须在 1 到 120000 之间")
        if not 20 <= max_events <= 500:
            raise ValueError("max_events 必须在 20 到 500 之间")

        tables = self._read_tables()
        if not {"process", "thread"} <= tables:
            raise ValueError(
                "构造冷启动时间线需要 process 和 thread 表"
            )
        process = self._read_process(target_ipid)
        if process is None:
            raise ValueError(f"找不到目标 ipid：{target_ipid}")

        trace_range = self._read_trace_range(tables)
        threads = self._read_process_threads(
            target_ipid=target_ipid,
            include_slices="callstack" in tables,
        )
        main_thread = next(
            (
                thread
                for thread in threads
                if thread.get("is_main_thread") == 1
            ),
            None,
        )
        earliest_slice_ns = self._read_earliest_process_slice(
            tables,
            target_ipid=target_ipid,
        )
        earliest_frame_ns = self._read_earliest_process_frame(
            tables,
            target_ipid=target_ipid,
        )
        anchor_ns, anchor_source = self._select_timeline_anchor(
            process=process,
            main_thread=main_thread,
            earliest_slice_ns=earliest_slice_ns,
            earliest_frame_ns=earliest_frame_ns,
            trace_range=trace_range,
        )
        lookback_ns = lookback_ms * 1_000_000
        lookahead_ns = lookahead_ms * 1_000_000
        window_start_ns = anchor_ns - lookback_ns
        window_end_ns = anchor_ns + lookahead_ns
        if trace_range is not None:
            window_start_ns = max(
                window_start_ns,
                trace_range["start_ns"],
            )
            window_end_ns = min(
                window_end_ns,
                trace_range["end_ns"],
            )
        if window_end_ns <= window_start_ns:
            raise ValueError("冷启动候选时间窗口为空")

        process_name = str(process.get("process_name") or "")
        app_startup_total, app_startup_rows = self._read_startup_stages(
            tables,
            hint=process_name,
            max_rows=min(max_events, 500),
        )
        app_startup_rows = [
            row
            for row in app_startup_rows
            if self._overlaps_window(
                row.get("start_ns"),
                row.get("end_ns"),
                window_start_ns,
                window_end_ns,
            )
        ]

        events, event_sources = self._read_timeline_events(
            tables,
            target_ipid=target_ipid,
            target_process_name=process_name,
            target_itids=[
                int(thread["itid"])
                for thread in threads
                if isinstance(thread.get("itid"), int)
            ][:50],
            anchor_ns=anchor_ns,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            max_events=max_events,
        )
        frames, frame_links = self._read_timeline_frames(
            tables,
            target_ipid=target_ipid,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            max_rows=min(max_events, 200),
        )
        boundary_evidence = self._read_boundary_evidence(
            tables,
            target_ipid=target_ipid,
            main_itid=(
                int(main_thread["itid"])
                if main_thread is not None
                and isinstance(main_thread.get("itid"), int)
                else None
            ),
            process_start_ns=(
                int(process["start_ts"])
                if isinstance(process.get("start_ts"), int)
                else None
            ),
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            max_rows=min(max_events, 200),
        )
        same_name_processes = self._read_same_name_processes(
            process_name=process_name,
            max_rows=20,
        )

        limitations: list[str] = []
        if process.get("start_ts") is None:
            limitations.append(
                "process.start_ts 缺失，时间线使用后备锚点"
            )
        elif anchor_source != "process.start_ts":
            limitations.append(
                "process.start_ts 不在有效 Trace 范围内，"
                "时间线使用后备锚点"
            )
        if "callstack" not in tables:
            limitations.append("Trace DB 不包含 callstack 表")
        if "app_startup" not in tables:
            limitations.append("Trace DB 不包含 app_startup 表")
        elif app_startup_total == 0:
            limitations.append("app_startup 表为空")
        elif not app_startup_rows:
            limitations.append(
                "当前窗口内没有与目标应用匹配的 app_startup 阶段"
            )
        if "frame_slice" not in tables:
            limitations.append("Trace DB 不包含 frame_slice 表")
        elif not frames:
            limitations.append("当前窗口内没有目标应用帧候选")
        if "frame_maps" not in tables:
            limitations.append("Trace DB 不包含 frame_maps 表")
        elif frames and not frame_links:
            limitations.append("目标应用帧没有可用的跨帧映射")

        return {
            "target_process": process,
            "same_name_processes": same_name_processes,
            "threads": threads,
            "main_thread": main_thread,
            "trace_range": trace_range,
            "window": {
                "start_ns": window_start_ns,
                "end_ns": window_end_ns,
                "anchor_ns": anchor_ns,
                "anchor_source": anchor_source,
                "lookback_ms": lookback_ms,
                "lookahead_ms": lookahead_ms,
            },
            "cold_start_observations": {
                "process_start_observed": isinstance(
                    process.get("start_ts"),
                    int,
                ),
                "process_started_inside_trace": (
                    isinstance(process.get("start_ts"), int)
                    and trace_range is not None
                    and trace_range["start_ns"]
                    <= process["start_ts"]
                    < trace_range["end_ns"]
                ),
                "main_thread_start_observed": (
                    main_thread is not None
                    and isinstance(main_thread.get("start_ts"), int)
                ),
                "earliest_target_slice_ns": earliest_slice_ns,
                "earliest_target_frame_ns": earliest_frame_ns,
                "same_name_instance_count": len(same_name_processes),
            },
            "app_startup": {
                "table_row_count": app_startup_total,
                "window_matched_row_count": len(app_startup_rows),
                "stages": app_startup_rows,
            },
            "timeline_events": events,
            "timeline_event_sources": event_sources,
            "timeline_event_scope": {
                "max_events": max_events,
                "returned_events": len(events),
                "may_be_truncated": len(events) >= max_events,
            },
            "frame_candidates": frames,
            "frame_links": frame_links,
            "frame_link_scope": {
                "returned_frame_candidates": len(frames),
                "mapped_candidate_frames": min(len(frames), 40),
                "mapping_scope_truncated": len(frames) > 40,
            },
            "boundary_evidence": boundary_evidence,
            "limitations": limitations,
            "decision_policy": (
                "这些是结构化候选事实；工具没有证明冷启动、"
                "选择启动边界或认定首帧已上屏"
            ),
        }

    def _read_boundary_evidence(
        self,
        tables: set[str],
        *,
        target_ipid: int,
        main_itid: int | None,
        process_start_ns: int | None,
        window_start_ns: int,
        window_end_ns: int,
        max_rows: int,
    ) -> dict[str, Any]:
        """Build facts that can prove launch-to-frame boundary pairs.

        The returned metric candidate is deterministic, but intentionally
        remains a candidate: input-to-launch causality and product semantics
        still belong to the agent.  A frame is never treated as first content
        merely because it is the earliest row in ``frame_slice``.
        """
        empty = {
            "launch_candidates": [],
            "input_candidates": [],
            "mapped_frame_candidates": [],
            "metric_candidate": None,
            "selection_policy": {
                "application_complete": (
                    "first main-thread ReceiveVsync that owns an actual app "
                    "frame mapped through frame_maps to a render-service frame"
                ),
                "presentation_complete": (
                    "end of the mapped render-service actual frame"
                ),
                "input_latency": (
                    "only report click-to-frame when an input-to-launch "
                    "causality chain is present; temporal proximity is not proof"
                ),
            },
        }
        if "callstack" not in tables or main_itid is None:
            return empty

        candidate_limit = max(10, min(max_rows, 200))
        launch_rows = self._repository.query(
            "SELECT c.id, c.ts, c.dur, c.ts + c.dur AS end_ns, "
            "c.name, c.cat, c.depth "
            "FROM callstack AS c "
            "WHERE c.callid = ? AND c.ts >= ? AND c.ts < ? "
            "AND (LOWER(COALESCE(c.name, '')) LIKE '%appspawn%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%appstart%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%applicationlaunch%') "
            "ORDER BY c.ts, c.id LIMIT ?",
            [main_itid, window_start_ns, window_end_ns, candidate_limit],
            max_rows=candidate_limit,
        ).rows
        launch_candidates = [
            {
                **row,
                "source": "callstack",
                "source_id": f"callstack:{row['id']}",
                "candidate_kind": "platform_launch_marker",
            }
            for row in launch_rows
            if isinstance(row.get("id"), int)
        ]

        input_rows = self._repository.query(
            "SELECT c.id, c.ts, c.dur, c.ts + c.dur AS end_ns, "
            "c.name, c.cat, p.ipid, p.pid, p.name AS process_name, "
            "t.itid, t.tid, t.name AS thread_name "
            "FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
            "WHERE c.ts >= ? AND c.ts < ? "
            "AND (LOWER(COALESCE(c.name, '')) LIKE '%touch%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%pointer%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%click%') "
            "ORDER BY c.ts, c.id LIMIT ?",
            [window_start_ns, window_end_ns, candidate_limit],
            max_rows=candidate_limit,
        ).rows
        input_candidates = [
            {
                **row,
                "source": "callstack",
                "source_id": f"callstack:{row['id']}",
                "causality_proven": False,
            }
            for row in input_rows
            if isinstance(row.get("id"), int)
        ]

        if not {"frame_slice", "frame_maps"} <= tables:
            empty["launch_candidates"] = launch_candidates
            empty["input_candidates"] = input_candidates
            return empty

        mapped_rows = self._repository.query(
            "SELECT app.id AS app_frame_id, app.ts AS app_frame_start_ns, "
            "app.dur AS app_frame_dur_ns, app.ts + app.dur AS app_frame_end_ns, "
            "app.vsync, app.itid AS app_itid, app.callstack_id, "
            "app.type_desc AS app_frame_type, m.id AS frame_map_id, "
            "rs.id AS rs_frame_id, rs.ts AS rs_frame_start_ns, "
            "rs.dur AS rs_frame_dur_ns, rs.ts + rs.dur AS rs_frame_end_ns, "
            "rs.ipid AS rs_ipid, rs.itid AS rs_itid, "
            "rp.pid AS rs_pid, rp.name AS rs_process_name, "
            "rs.type_desc AS rs_frame_type "
            "FROM frame_slice AS app "
            "JOIN frame_maps AS m ON m.src_row = app.id "
            "JOIN frame_slice AS rs ON rs.id = m.dst_row "
            "LEFT JOIN process AS rp ON rp.ipid = rs.ipid "
            "WHERE app.ipid = ? AND app.itid = ? "
            "AND app.ts >= ? AND app.ts < ? "
            "AND LOWER(COALESCE(app.type_desc, '')) IN ('actural', 'actual') "
            "ORDER BY app.ts, app.id LIMIT ?",
            [
                target_ipid,
                main_itid,
                window_start_ns,
                window_end_ns,
                candidate_limit,
            ],
            max_rows=candidate_limit,
        ).rows
        receive_rows = self._repository.query(
            "SELECT c.id, c.ts, c.dur, c.ts + c.dur AS end_ns, "
            "c.name, c.cat, c.depth "
            "FROM callstack AS c "
            "WHERE c.callid = ? AND c.ts >= ? AND c.ts < ? "
            "AND LOWER(COALESCE(c.name, '')) LIKE '%receivevsync%' "
            "ORDER BY c.ts, c.id LIMIT ?",
            [main_itid, window_start_ns, window_end_ns, candidate_limit],
            max_rows=candidate_limit,
        ).rows
        receive_by_id = {
            row["id"]: row
            for row in receive_rows
            if isinstance(row.get("id"), int)
        }

        mapped_candidates: list[dict[str, Any]] = []
        for row in mapped_rows:
            receive = receive_by_id.get(row.get("callstack_id"))
            if receive is None:
                frame_start = row.get("app_frame_start_ns")
                receive = next(
                    (
                        candidate
                        for candidate in receive_rows
                        if isinstance(frame_start, int)
                        and candidate.get("ts") == frame_start
                    ),
                    None,
                )
            receive_start_ns = (
                receive.get("ts") if receive is not None else None
            )
            receive_end_ns = (
                receive.get("end_ns") if receive is not None else None
            )
            mapped_candidates.append(
                {
                    **row,
                    "receive_slice_id": (
                        receive.get("id") if receive is not None else None
                    ),
                    "receive_slice_name": (
                        receive.get("name") if receive is not None else None
                    ),
                    "receive_start_ns": receive_start_ns,
                    "receive_end_ns": receive_end_ns,
                    "main_thread_receive_proven": receive is not None,
                    "app_to_rs_mapping_proven": True,
                    "app_frame_source_id": (
                        f"frame_slice:{row['app_frame_id']}"
                    ),
                    "rs_frame_source_id": (
                        f"frame_slice:{row['rs_frame_id']}"
                    ),
                    "frame_map_source_id": (
                        f"frame_maps:{row['frame_map_id']}"
                    ),
                    "receive_source_id": (
                        f"callstack:{receive['id']}"
                        if receive is not None
                        else None
                    ),
                }
            )

        preferred_launch = next(
            (
                candidate
                for candidate in launch_candidates
                if "appspawn" in str(candidate.get("name", "")).lower()
            ),
            launch_candidates[0] if launch_candidates else None,
        )
        preferred_frame = next(
            (
                candidate
                for candidate in mapped_candidates
                if candidate["main_thread_receive_proven"]
            ),
            None,
        )
        metric_candidate: dict[str, Any] | None = None
        if preferred_frame is not None:
            start_ns = (
                preferred_launch.get("ts")
                if preferred_launch is not None
                else process_start_ns
            )
            app_end_ns = preferred_frame.get("receive_end_ns")
            presentation_end_ns = preferred_frame.get("rs_frame_end_ns")
            if (
                isinstance(start_ns, int)
                and isinstance(app_end_ns, int)
                and app_end_ns > start_ns
            ):
                metric_candidate = {
                    "status": (
                        "proven_platform_boundary_pair"
                        if preferred_launch is not None
                        else "frame_proven_start_fallback"
                    ),
                    "start": (
                        preferred_launch
                        if preferred_launch is not None
                        else {
                            "name": "process.start_ts",
                            "ts": start_ns,
                            "source": "process",
                            "source_id": f"process:{target_ipid}",
                        }
                    ),
                    "application_complete": {
                        "name": preferred_frame.get("receive_slice_name"),
                        "timestamp_ns": app_end_ns,
                        "source": "callstack",
                        "source_id": preferred_frame.get(
                            "receive_source_id"
                        ),
                        "app_frame_source_id": preferred_frame.get(
                            "app_frame_source_id"
                        ),
                    },
                    "presentation_complete": {
                        "name": "mapped render-service frame end",
                        "timestamp_ns": presentation_end_ns,
                        "source": "frame_slice",
                        "source_id": preferred_frame.get(
                            "rs_frame_source_id"
                        ),
                    },
                    "application_duration_ms": (
                        app_end_ns - start_ns
                    )
                    / 1_000_000.0,
                    "presentation_duration_ms": (
                        (presentation_end_ns - start_ns) / 1_000_000.0
                        if isinstance(presentation_end_ns, int)
                        and presentation_end_ns > start_ns
                        else None
                    ),
                    "proof": [
                        "launch marker or explicit process-start fallback",
                        "main-thread ReceiveVsync ownership",
                        "actual application frame",
                        "frame_maps app-to-render-service link",
                    ],
                }

        return {
            **empty,
            "launch_candidates": launch_candidates,
            "input_candidates": input_candidates,
            "mapped_frame_candidates": mapped_candidates,
            "metric_candidate": metric_candidate,
        }

    def _read_process(self, target_ipid: int) -> dict[str, Any] | None:
        result = self._repository.query(
            "SELECT p.ipid, p.pid, p.name AS process_name, "
            "p.start_ts, p.switch_count, p.thread_count, p.slice_count "
            "FROM process AS p WHERE p.ipid = ? LIMIT 1",
            [target_ipid],
            max_rows=1,
        )
        return result.rows[0] if result.rows else None

    def _read_process_threads(
        self,
        *,
        target_ipid: int,
        include_slices: bool,
    ) -> list[dict[str, Any]]:
        slice_columns = (
            "MIN(c.ts) AS first_slice_ns, "
            "MAX(c.ts + c.dur) AS last_slice_end_ns, "
            "COUNT(c.id) AS slice_count"
            if include_slices
            else (
                "NULL AS first_slice_ns, "
                "NULL AS last_slice_end_ns, "
                "0 AS slice_count"
            )
        )
        slice_join = (
            "LEFT JOIN callstack AS c ON c.callid = t.itid"
            if include_slices
            else ""
        )
        result = self._repository.query(
            "SELECT t.itid, t.tid, t.name AS thread_name, "
            "t.start_ts, t.end_ts, t.is_main_thread, "
            f"{slice_columns} "
            "FROM thread AS t "
            f"{slice_join} "
            "WHERE t.ipid = ? "
            "GROUP BY t.itid, t.tid, t.name, t.start_ts, "
            "t.end_ts, t.is_main_thread "
            "ORDER BY t.is_main_thread DESC, first_slice_ns, t.itid "
            "LIMIT 200",
            [target_ipid],
            max_rows=200,
        )
        return result.rows

    def _read_earliest_process_slice(
        self,
        tables: set[str],
        *,
        target_ipid: int,
    ) -> int | None:
        if not {"callstack", "thread"} <= tables:
            return None
        result = self._repository.query(
            "SELECT MIN(c.ts) AS first_slice_ns "
            "FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "WHERE t.ipid = ?",
            [target_ipid],
            max_rows=1,
        )
        value = result.rows[0].get("first_slice_ns")
        return value if isinstance(value, int) else None

    def _read_earliest_process_frame(
        self,
        tables: set[str],
        *,
        target_ipid: int,
    ) -> int | None:
        if "frame_slice" not in tables:
            return None
        result = self._repository.query(
            "SELECT MIN(ts) AS first_frame_ns "
            "FROM frame_slice WHERE ipid = ?",
            [target_ipid],
            max_rows=1,
        )
        value = result.rows[0].get("first_frame_ns")
        return value if isinstance(value, int) else None

    @staticmethod
    def _select_timeline_anchor(
        *,
        process: dict[str, Any],
        main_thread: dict[str, Any] | None,
        earliest_slice_ns: int | None,
        earliest_frame_ns: int | None,
        trace_range: dict[str, int] | None,
    ) -> tuple[int, str]:
        candidates = [
            ("process.start_ts", process.get("start_ts")),
            (
                "main_thread.start_ts",
                main_thread.get("start_ts")
                if main_thread is not None
                else None,
            ),
            ("target_process.first_slice", earliest_slice_ns),
            ("target_process.first_frame", earliest_frame_ns),
        ]
        for source, value in candidates:
            if not isinstance(value, int):
                continue
            if (
                trace_range is None
                or trace_range["start_ns"]
                <= value
                < trace_range["end_ns"]
            ):
                return value, source
        if trace_range is not None:
            return trace_range["start_ns"], "trace_range.start_ts"
        raise ValueError("无法为目标进程建立冷启动候选锚点")

    def _read_same_name_processes(
        self,
        *,
        process_name: str,
        max_rows: int,
    ) -> list[dict[str, Any]]:
        if not process_name:
            return []
        result = self._repository.query(
            "SELECT ipid, pid, name AS process_name, start_ts, "
            "switch_count, thread_count, slice_count "
            "FROM process WHERE name = ? "
            "ORDER BY CASE WHEN start_ts IS NULL THEN 1 ELSE 0 END, "
            "start_ts, ipid LIMIT ?",
            [process_name, max_rows],
            max_rows=max_rows,
        )
        return result.rows

    def _read_timeline_events(
        self,
        tables: set[str],
        *,
        target_ipid: int,
        target_process_name: str,
        target_itids: list[int],
        anchor_ns: int,
        window_start_ns: int,
        window_end_ns: int,
        max_events: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        if not {"callstack", "thread", "process"} <= tables:
            return [], {}

        target_limit = max(1, max_events // 2)
        package_limit = max(1, max_events // 5)
        nearby_limit = max(1, max_events // 5)
        instant_limit = max(
            1,
            max_events - target_limit - package_limit - nearby_limit,
        )
        event_map: dict[tuple[str, int], dict[str, Any]] = {}
        source_counts: dict[str, int] = {}

        self._add_slice_events(
            event_map,
            source_counts,
            self._repository.query(
                self._slice_event_select()
                + "WHERE p.ipid = ? "
                "AND c.ts < ? AND c.ts + c.dur > ? "
                "AND c.depth <= 2 "
                "ORDER BY c.ts, c.id LIMIT ?",
                [
                    target_ipid,
                    window_end_ns,
                    window_start_ns,
                    target_limit,
                ],
                max_rows=target_limit,
            ).rows,
            reason="target_process_shallow_slice",
        )

        if target_process_name:
            self._add_slice_events(
                event_map,
                source_counts,
                self._repository.query(
                    self._slice_event_select()
                    + "WHERE c.ts < ? AND c.ts + c.dur > ? "
                    "AND LOWER(COALESCE(c.name, '')) "
                    "LIKE '%' || LOWER(?) || '%' "
                    "ORDER BY c.ts, c.id LIMIT ?",
                    [
                        window_end_ns,
                        window_start_ns,
                        target_process_name,
                        package_limit,
                    ],
                    max_rows=package_limit,
                ).rows,
                reason="target_package_name_match",
            )

        nearby_start_ns = max(
            window_start_ns,
            anchor_ns - 250_000_000,
        )
        nearby_end_ns = min(
            window_end_ns,
            anchor_ns + 500_000_000,
        )
        self._add_slice_events(
            event_map,
            source_counts,
            self._repository.query(
                self._slice_event_select()
                + "WHERE c.ts < ? AND c.ts + c.dur > ? "
                "AND c.depth = 0 "
                "ORDER BY c.ts, c.id LIMIT ?",
                [nearby_end_ns, nearby_start_ns, nearby_limit],
                max_rows=nearby_limit,
            ).rows,
            reason="near_anchor_root_slice",
        )

        if "instant" in tables and target_itids:
            placeholders = ", ".join("?" for _ in target_itids)
            result = self._repository.query(
                "SELECT i.rowid AS id, i.ts, i.name, i.ref, i.ref_type, "
                "i.wakeup_from, i.value "
                "FROM instant AS i "
                "WHERE i.ts >= ? AND i.ts < ? "
                "AND i.ref_type = 'itid' "
                f"AND i.ref IN ({placeholders}) "
                "ORDER BY i.ts, i.rowid LIMIT ?",
                [
                    window_start_ns,
                    window_end_ns,
                    *target_itids,
                    instant_limit,
                ],
                max_rows=instant_limit,
            )
            for row in result.rows:
                event_id = row.get("id")
                if not isinstance(event_id, int):
                    continue
                event_map[("instant", event_id)] = {
                    "source": "instant",
                    "source_id": event_id,
                    "event_type": "point_candidate",
                    "ts": row.get("ts"),
                    "end_ns": row.get("ts"),
                    "name": row.get("name"),
                    "ref": row.get("ref"),
                    "ref_type": row.get("ref_type"),
                    "wakeup_from": row.get("wakeup_from"),
                    "value": row.get("value"),
                    "candidate_reasons": [
                        "target_thread_point_event"
                    ],
                }
            source_counts["target_thread_point_event"] = len(
                result.rows
            )

        events = sorted(
            event_map.values(),
            key=lambda event: (
                event.get("ts")
                if isinstance(event.get("ts"), int)
                else 2**63 - 1,
                str(event.get("source")),
                int(event.get("source_id") or 0),
            ),
        )
        return events[:max_events], source_counts

    @staticmethod
    def _slice_event_select() -> str:
        return (
            "SELECT c.id, c.ts, c.dur, c.name, c.cat, c.depth, "
            "c.parent_id, p.ipid, p.pid, "
            "p.name AS process_name, t.itid, t.tid, "
            "t.name AS thread_name, t.is_main_thread "
            "FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
        )

    @staticmethod
    def _add_slice_events(
        event_map: dict[tuple[str, int], dict[str, Any]],
        source_counts: dict[str, int],
        rows: list[dict[str, Any]],
        *,
        reason: str,
    ) -> None:
        source_counts[reason] = len(rows)
        for row in rows:
            event_id = row.get("id")
            if not isinstance(event_id, int):
                continue
            key = ("callstack", event_id)
            existing = event_map.get(key)
            if existing is not None:
                existing["candidate_reasons"].append(reason)
                continue
            ts = row.get("ts")
            dur = row.get("dur")
            event_map[key] = {
                "source": "callstack",
                "source_id": event_id,
                "event_type": "slice_candidate",
                "ts": ts,
                "end_ns": (
                    ts + dur
                    if isinstance(ts, int) and isinstance(dur, int)
                    else None
                ),
                "dur_ns": dur,
                "name": row.get("name"),
                "category": row.get("cat"),
                "depth": row.get("depth"),
                "parent_id": row.get("parent_id"),
                "ipid": row.get("ipid"),
                "pid": row.get("pid"),
                "process_name": row.get("process_name"),
                "itid": row.get("itid"),
                "tid": row.get("tid"),
                "thread_name": row.get("thread_name"),
                "is_main_thread": row.get("is_main_thread"),
                "candidate_reasons": [reason],
            }

    def _read_timeline_frames(
        self,
        tables: set[str],
        *,
        target_ipid: int,
        window_start_ns: int,
        window_end_ns: int,
        max_rows: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if "frame_slice" not in tables:
            return [], []
        process_join = (
            "LEFT JOIN process AS p ON p.ipid = f.ipid "
            if "process" in tables
            else ""
        )
        thread_join = (
            "LEFT JOIN thread AS t ON t.itid = f.itid "
            if "thread" in tables
            else ""
        )
        process_columns = (
            "p.pid, p.name AS process_name, "
            if "process" in tables
            else "NULL AS pid, NULL AS process_name, "
        )
        thread_columns = (
            "t.tid, t.name AS thread_name, t.is_main_thread, "
            if "thread" in tables
            else (
                "NULL AS tid, NULL AS thread_name, "
                "NULL AS is_main_thread, "
            )
        )
        result = self._repository.query(
            "SELECT f.id, f.ts, f.dur, "
            "CASE WHEN f.dur >= 0 THEN f.ts + f.dur "
            "ELSE NULL END AS end_ns, "
            "f.vsync, f.ipid, f.itid, f.callstack_id, "
            f"{process_columns}{thread_columns}"
            "f.src, f.dst, f.type, f.type_desc, f.flag, "
            "f.depth, f.frame_no "
            "FROM frame_slice AS f "
            f"{process_join}{thread_join}"
            "WHERE f.ipid = ? AND f.ts >= ? AND f.ts < ? "
            "ORDER BY f.ts, f.id LIMIT ?",
            [
                target_ipid,
                window_start_ns,
                window_end_ns,
                max_rows,
            ],
            max_rows=max_rows,
        )
        frames = result.rows
        if "frame_maps" not in tables or not frames:
            return frames, []

        frame_ids = [
            int(frame["id"])
            for frame in frames
            if isinstance(frame.get("id"), int)
        ][:40]
        if not frame_ids:
            return frames, []
        placeholders = ", ".join("?" for _ in frame_ids)
        link_limit = min(max_rows * 2, 500)
        links = self._repository.query(
            "SELECT m.id AS map_id, m.src_row, m.dst_row, "
            "src.ts AS src_ts, src.dur AS src_dur_ns, "
            "src.ipid AS src_ipid, sp.pid AS src_pid, "
            "sp.name AS src_process_name, src.itid AS src_itid, "
            "src.type_desc AS src_type_desc, "
            "dst.ts AS dst_ts, dst.dur AS dst_dur_ns, "
            "dst.ipid AS dst_ipid, dp.pid AS dst_pid, "
            "dp.name AS dst_process_name, dst.itid AS dst_itid, "
            "dst.type_desc AS dst_type_desc "
            "FROM frame_maps AS m "
            "JOIN frame_slice AS src ON src.id = m.src_row "
            "JOIN frame_slice AS dst ON dst.id = m.dst_row "
            "LEFT JOIN process AS sp ON sp.ipid = src.ipid "
            "LEFT JOIN process AS dp ON dp.ipid = dst.ipid "
            f"WHERE m.src_row IN ({placeholders}) "
            f"OR m.dst_row IN ({placeholders}) "
            "ORDER BY src.ts, dst.ts, m.id LIMIT ?",
            [*frame_ids, *frame_ids, link_limit],
            max_rows=link_limit,
        ).rows
        return frames, links

    @staticmethod
    def _overlaps_window(
        start_ns: Any,
        end_ns: Any,
        window_start_ns: int,
        window_end_ns: int,
    ) -> bool:
        return (
            isinstance(start_ns, int)
            and isinstance(end_ns, int)
            and start_ns < window_end_ns
            and end_ns > window_start_ns
        )

    def _read_tables(self) -> set[str]:
        placeholders = ", ".join("?" for _ in self._TABLES)
        result = self._repository.query(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view') "
            f"AND name IN ({placeholders})",
            list(self._TABLES),
            max_rows=len(self._TABLES),
        )
        return {str(row["name"]) for row in result.rows}

    def _read_trace_range(
        self,
        tables: set[str],
    ) -> dict[str, int] | None:
        if "trace_range" not in tables:
            return None
        result = self._repository.query(
            "SELECT start_ts, end_ts "
            "FROM trace_range ORDER BY start_ts LIMIT 1",
            max_rows=1,
        )
        if not result.rows:
            return None
        row = result.rows[0]
        start_ns = row.get("start_ts")
        end_ns = row.get("end_ts")
        if not isinstance(start_ns, int) or not isinstance(end_ns, int):
            return None
        return {"start_ns": start_ns, "end_ns": end_ns}

    def _read_processes(
        self,
        tables: set[str],
        *,
        hint: str,
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        if "process" not in tables:
            return []
        thread_columns = (
            "t.itid AS main_itid, t.tid AS main_tid, "
            "t.name AS main_thread_name"
            if "thread" in tables
            else (
                "NULL AS main_itid, NULL AS main_tid, "
                "NULL AS main_thread_name"
            )
        )
        thread_join = (
            "LEFT JOIN thread AS t "
            "ON t.ipid = p.ipid AND t.is_main_thread = 1"
            if "thread" in tables
            else ""
        )
        result = self._repository.query(
            "SELECT p.ipid, p.pid, p.name AS process_name, "
            "p.start_ts, p.switch_count, p.thread_count, "
            f"p.slice_count, {thread_columns} "
            "FROM process AS p "
            f"{thread_join} "
            "WHERE (? = '' OR LOWER(COALESCE(p.name, '')) "
            "LIKE '%' || LOWER(?) || '%') "
            "ORDER BY CASE WHEN p.start_ts IS NULL THEN 1 ELSE 0 END, "
            "p.start_ts, p.slice_count DESC, p.ipid "
            "LIMIT ?",
            [hint, hint, max_candidates],
            max_rows=max_candidates,
        )
        return result.rows

    @staticmethod
    def _annotate_process_start(
        processes: list[dict[str, Any]],
        trace_range: dict[str, int] | None,
    ) -> None:
        for process in processes:
            start_ts = process.get("start_ts")
            process["started_inside_trace"] = (
                isinstance(start_ts, int)
                and trace_range is not None
                and trace_range["start_ns"] <= start_ts
                < trace_range["end_ns"]
            )

    def _read_startup_stages(
        self,
        tables: set[str],
        *,
        hint: str,
        max_rows: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        if "app_startup" not in tables:
            return 0, []
        count_result = self._repository.query(
            "SELECT COUNT(*) AS row_count FROM app_startup",
            max_rows=1,
        )
        row_count = int(count_result.rows[0]["row_count"])
        if row_count == 0:
            return 0, []

        process_columns = (
            "p.pid, p.name AS process_name"
            if "process" in tables
            else "NULL AS pid, NULL AS process_name"
        )
        process_join = (
            "LEFT JOIN process AS p ON p.ipid = a.ipid"
            if "process" in tables
            else ""
        )
        stage_name = (
            "d.data AS stage_name"
            if "data_dict" in tables
            else "NULL AS stage_name"
        )
        dictionary_join = (
            "LEFT JOIN data_dict AS d ON d.id = a.start_name"
            if "data_dict" in tables
            else ""
        )
        package_name = (
            "COALESCE(pd.data, CAST(a.packed_name AS TEXT)) "
            "AS package_name"
            if "data_dict" in tables
            else "CAST(a.packed_name AS TEXT) AS package_name"
        )
        package_dictionary_join = (
            "LEFT JOIN data_dict AS pd ON pd.id = a.packed_name"
            if "data_dict" in tables
            else ""
        )
        package_expression = (
            "COALESCE(pd.data, CAST(a.packed_name AS TEXT), '')"
            if "data_dict" in tables
            else "COALESCE(CAST(a.packed_name AS TEXT), '')"
        )
        process_hint_predicate = (
            " OR LOWER(COALESCE(p.name, '')) "
            "LIKE '%' || LOWER(?) || '%'"
            if "process" in tables
            else ""
        )
        hint_filter = (
            f"WHERE (? = '' OR LOWER({package_expression}) "
            "LIKE '%' || LOWER(?) || '%'"
            f"{process_hint_predicate})"
        )
        hint_parameters = [hint, hint]
        if "process" in tables:
            hint_parameters.append(hint)
        result = self._repository.query(
            "SELECT a.id, a.call_id, a.ipid, "
            f"{process_columns}, a.tid AS raw_tid, "
            "a.start_time AS start_ns, a.end_time AS end_ns, "
            "CASE WHEN a.end_time >= a.start_time "
            "THEN a.end_time - a.start_time ELSE NULL END "
            f"AS duration_ns, {stage_name}, "
            "a.start_name AS stage_name_id, "
            "a.packed_name AS packed_name_raw, "
            f"{package_name}, "
            "'trace_streamer_app_startup' AS stage_source "
            "FROM app_startup AS a "
            f"{process_join} {dictionary_join} "
            f"{package_dictionary_join} "
            f"{hint_filter} "
            "ORDER BY a.start_time, a.id LIMIT ?",
            [*hint_parameters, max_rows],
            max_rows=max_rows,
        )
        return row_count, result.rows

    def _read_first_frames(
        self,
        tables: set[str],
        *,
        candidate_ipids: list[int],
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        if (
            not {"frame_slice", "process"} <= tables
            or not candidate_ipids
        ):
            return []
        placeholders = ", ".join("?" for _ in candidate_ipids)
        result = self._repository.query(
            "SELECT f.ipid, p.pid, p.name AS process_name, "
            "MIN(CASE WHEN f.type_desc = 'actural' THEN f.ts END) "
            "AS first_actual_frame_ns, "
            "MIN(CASE WHEN f.type_desc = 'expect' THEN f.ts END) "
            "AS first_expected_frame_ns, "
            "SUM(CASE WHEN f.type_desc = 'actural' THEN 1 ELSE 0 END) "
            "AS actual_frame_rows, "
            "SUM(CASE WHEN f.type_desc = 'expect' THEN 1 ELSE 0 END) "
            "AS expected_frame_rows "
            "FROM frame_slice AS f "
            "JOIN process AS p ON p.ipid = f.ipid "
            f"WHERE f.ipid IN ({placeholders}) "
            "GROUP BY f.ipid, p.pid, p.name "
            "HAVING first_actual_frame_ns IS NOT NULL "
            "OR first_expected_frame_ns IS NOT NULL "
            "ORDER BY first_actual_frame_ns, first_expected_frame_ns "
            "LIMIT ?",
            [*candidate_ipids, max_candidates],
            max_rows=max_candidates,
        )
        return result.rows

    def _read_earliest_slices(
        self,
        tables: set[str],
        *,
        candidate_ipids: list[int],
        max_rows: int,
    ) -> list[dict[str, Any]]:
        if (
            not {"callstack", "thread", "process"} <= tables
            or not candidate_ipids
        ):
            return []
        placeholders = ", ".join("?" for _ in candidate_ipids)
        result = self._repository.query(
            "WITH ranked AS ("
            "SELECT c.id, c.ts, c.dur, c.name AS slice_name, "
            "c.cat, c.depth, p.ipid, p.pid, "
            "p.name AS process_name, t.itid, t.tid, "
            "t.name AS thread_name, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY p.ipid ORDER BY c.ts, c.id"
            ") AS process_rank "
            "FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
            f"WHERE p.ipid IN ({placeholders})"
            ") "
            "SELECT id, ts, dur, slice_name, cat, depth, "
            "ipid, pid, process_name, itid, tid, thread_name "
            "FROM ranked WHERE process_rank <= 3 "
            "ORDER BY ts, id LIMIT ?",
            [*candidate_ipids, max_rows],
            max_rows=max_rows,
        )
        return result.rows
