from __future__ import annotations

from pathlib import Path
from typing import Any

from trace_agent.database.problem_window import ProblemWindowRepository
from trace_agent.database.repository import SQLiteTraceRepository


class CompletionLatencyRepository:
    """Return bounded completion-latency candidates without choosing one.

    The repository deliberately separates an application-defined duration
    Slice from frame/animation heuristics.  A quiet frame gap is useful
    discovery evidence, but it is not proof that a business action completed.
    """

    _ANIMATION_PATTERNS = (
        "h:app_swiper_fling,",
        "h:app_swiper_scroll,",
        "h:app_list_fling,",
        "h:ability_or_page_switch,",
        "h:ability_or_page_switch_interactive,",
        "h:show_input_method_animation,",
        "h:hide_input_method_animation,",
    )
    _FRAME_ACTIVITY_WINDOW_NS = 300_000_000
    _MIN_ACTIVE_PREVIOUS_FRAMES = 5
    _QUIET_GAP_NS = 200_000_000

    def __init__(self, database_path: Path) -> None:
        self._repository = SQLiteTraceRepository(database_path)
        self._problem_windows = ProblemWindowRepository(database_path)

    def inspect(
        self,
        *,
        target_ipid: int | None = None,
        target_process: str | None = None,
        operation_marker: str | None = None,
        start_marker: str | None = None,
        end_marker: str | None = None,
        response_marker: str | None = None,
        completion_marker: str | None = None,
        problem_duration_ms: float | None = None,
        interval_start_ns: int | None = None,
        interval_end_ns: int | None = None,
        lookahead_ms: int = 5000,
        max_candidates: int = 80,
    ) -> dict[str, Any]:
        if target_ipid is not None and target_ipid < 0:
            raise ValueError("target_ipid 必须是非负整数")
        if not 100 <= lookahead_ms <= 120_000:
            raise ValueError("lookahead_ms 必须在 100 到 120000 之间")
        if not 1 <= max_candidates <= 200:
            raise ValueError("max_candidates 必须在 1 到 200 之间")
        if (interval_start_ns is None) != (interval_end_ns is None):
            raise ValueError("显式完成时延区间必须同时提供开始和结束")
        explicit_time_range = None
        if interval_start_ns is not None and interval_end_ns is not None:
            if interval_start_ns < 0 or interval_end_ns <= interval_start_ns:
                raise ValueError("显式完成时延区间无效")
            explicit_time_range = {
                "start_ns": interval_start_ns,
                "end_ns": interval_end_ns,
                "duration_ms": (
                    interval_end_ns - interval_start_ns
                )
                / 1_000_000.0,
                "candidate_kind": "explicit-time-range",
                "single_operation_assumption": False,
                "metric_definition_from_user": True,
            }

        normalized_process = (target_process or "").strip()
        normalized_operation = (operation_marker or "").strip()
        normalized_start = (start_marker or "").strip()
        normalized_end = (
            (completion_marker or "").strip()
            or (end_marker or "").strip()
        )
        normalized_response = (response_marker or "").strip()
        tables = self._table_names()
        limitations: list[str] = []
        if (
            target_ipid is not None
            and not normalized_process
            and "process" in tables
        ):
            normalized_process = self._process_name(target_ipid) or ""

        generic = self._problem_windows.inspect(
            target_process=normalized_process or None,
            start_marker=normalized_start or None,
            end_marker=normalized_end or None,
            problem_duration_ms=problem_duration_ms,
            max_candidates=min(max_candidates, 100),
        )
        input_boundary = (
            {
                "source": "analyze-request.time-range",
                "source_id": None,
                "name": "explicit time-range start",
                "timestamp_ns": explicit_time_range["start_ns"],
                "candidate_kind": "explicit-time-range-start",
                "timestamp_rule": "user supplied --time-range start",
                "requires_agent_validation": False,
            }
            if explicit_time_range is not None
            else self._completion_input_boundary(
                generic["last_input_point"].get("candidate")
            )
        )

        implicit_operation = (
            normalized_start
            if normalized_start and not normalized_end
            else ""
        )
        operation_requested = normalized_operation or implicit_operation
        operation_match = self._match_marker(
            tables,
            operation_requested,
            target_ipid=target_ipid,
            target_process=normalized_process,
            max_candidates=max_candidates,
        )
        operation_span = self._duration_span_candidate(
            operation_match,
            requested_from=(
                "operation_marker"
                if normalized_operation
                else "single_start_marker"
                if implicit_operation
                else None
            ),
        )
        response_match = self._match_marker(
            tables,
            normalized_response,
            target_ipid=target_ipid,
            target_process=normalized_process,
            max_candidates=max_candidates,
        )
        completion_match = self._match_marker(
            tables,
            normalized_end,
            target_ipid=target_ipid,
            target_process=normalized_process,
            max_candidates=max_candidates,
        )

        marker_pair = generic["application_marker_pair"].get("candidate")
        duration_window = (
            explicit_time_range
            if explicit_time_range is not None
            else self._duration_window(
                input_boundary=input_boundary,
                problem_duration_ms=problem_duration_ms,
                fallback=generic.get("duration_window"),
            )
        )
        discovery_window = self._select_discovery_window(
            explicit_time_range=explicit_time_range,
            explicit_operation_span=(
                operation_span.get("candidate")
                if normalized_operation
                else None
            ),
            marker_pair=marker_pair,
            implicit_operation_span=(
                operation_span.get("candidate")
                if not normalized_operation
                else None
            ),
            duration_window=duration_window,
            input_boundary=input_boundary,
            lookahead_ms=lookahead_ms,
            trace_range=generic["trace_range"],
        )

        process_candidates = self._process_candidates(
            tables,
            target_ipid=target_ipid,
            target_process=normalized_process,
            marker_matches=[
                *operation_match.get("matches", []),
                *response_match.get("matches", []),
                *completion_match.get("matches", []),
            ],
            discovery_window=discovery_window,
            max_candidates=min(max_candidates, 20),
        )

        frames: list[dict[str, Any]] = []
        frame_links: list[dict[str, Any]] = []
        animations: list[dict[str, Any]] = []
        if target_ipid is not None and discovery_window is not None:
            frames, frame_links = self._frames(
                tables,
                target_ipid=target_ipid,
                start_ns=discovery_window["start_ns"],
                end_ns=discovery_window["end_ns"],
                max_candidates=max_candidates,
            )
            animations = self._animations(
                tables,
                start_ns=discovery_window["start_ns"],
                end_ns=discovery_window["end_ns"],
                max_candidates=max_candidates,
            )
        elif discovery_window is not None:
            limitations.append(
                "尚未指定 target_ipid；本次只返回目标进程候选，"
                "选择应用进程后应再次调用以获取帧和动画候选。"
            )

        response_candidates = self._response_candidates(
            response_match=response_match,
            input_boundary=input_boundary,
            frames=frames,
            frame_links=frame_links,
        )
        completion_candidates = self._completion_candidates(
            operation_span=operation_span,
            completion_match=completion_match,
            duration_window=duration_window,
            frames=frames,
            frame_links=frame_links,
            animations=animations,
        )
        if "frame_slice" not in tables:
            limitations.append(
                "Trace DB 不包含 frame_slice；无法发现响应帧或帧稳定候选。"
            )
        if operation_requested and operation_span.get("candidate") is None:
            limitations.append(
                "请求的 operation Marker 未唯一解析为正 duration Slice。"
            )

        return {
            "trace_range": generic["trace_range"],
            "target_ipid": target_ipid,
            "target_process_hint": normalized_process or None,
            "target_process_candidates": process_candidates,
            "input_boundary": input_boundary,
            "application_marker_pair": generic[
                "application_marker_pair"
            ],
            "operation_span": operation_span,
            "response_marker": response_match,
            "completion_marker": completion_match,
            "duration_window": duration_window,
            "explicit_time_range": explicit_time_range,
            "discovery_window": discovery_window,
            "app_frames": frames,
            "frame_links": frame_links,
            "animation_candidates": animations,
            "response_candidates": response_candidates,
            "completion_candidates": completion_candidates,
            "selection_policy": [
                "显式 time_range 是最高优先级的用户定义指标窗口；"
                "不得替换为 Trace 最后输入点。",
                "唯一的应用 duration Slice 可用 ts 到 ts+dur 定义总完成区间。",
                "唯一开始/完成 Marker 对可定义总完成区间。",
                "用户提供 duration 时，可在单次操作假设下用最后有效输入点加 duration 定义总区间。",
                "响应边界必须代表有效反馈；首个技术帧只是候选。",
                "动画结束必须证明属于本次操作，不能只靠时间接近。",
                "200ms 帧空档和窗口最后一帧仅用于发现，不证明业务完成。",
                "最终必须保留 input→response、response→completion 和"
                " input→completion 三段指标。",
            ],
            "heuristics": {
                "frame_activity_window_ms": 300,
                "minimum_previous_frames": 5,
                "quiet_gap_ms": 200,
                "source": (
                    "Harmony Trace Analyzer v1.0.6 frame-based strategy, "
                    "downgraded here to candidate-only evidence"
                ),
            },
            "limitations": list(
                dict.fromkeys(
                    [*generic.get("limitations", []), *limitations]
                )
            ),
        }

    def _process_name(self, ipid: int) -> str | None:
        rows = self._repository.query(
            "SELECT name FROM process WHERE ipid = ? LIMIT 1",
            [ipid],
            max_rows=1,
        ).rows
        if not rows:
            return None
        value = rows[0].get("name")
        return str(value) if value is not None else None

    def _table_names(self) -> set[str]:
        rows = self._repository.query(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table', 'view')",
            max_rows=500,
        ).rows
        return {
            str(row["name"])
            for row in rows
            if isinstance(row.get("name"), str)
        }

    def _match_marker(
        self,
        tables: set[str],
        marker: str,
        *,
        target_ipid: int | None,
        target_process: str,
        max_candidates: int,
    ) -> dict[str, Any]:
        if not marker:
            return {
                "requested": None,
                "status": "not_requested",
                "match_mode": None,
                "matches": [],
                "unique_candidate": None,
            }
        if "callstack" not in tables:
            return {
                "requested": marker,
                "status": "unavailable",
                "match_mode": None,
                "matches": [],
                "unique_candidate": None,
            }

        include_scope = {"thread", "process"} <= tables
        modes = ("exact", "contains")
        matches: list[dict[str, Any]] = []
        selected_mode: str | None = None
        for mode in modes:
            predicate = (
                "LOWER(TRIM(COALESCE(c.name, ''))) = LOWER(TRIM(?))"
                if mode == "exact"
                else "INSTR(LOWER(COALESCE(c.name, '')), LOWER(?)) > 0"
            )
            joins = (
                "JOIN thread AS t ON t.itid = c.callid "
                "JOIN process AS p ON p.ipid = t.ipid "
                if include_scope
                else ""
            )
            scope_columns = (
                "p.ipid, p.pid, p.name AS process_name, "
                "t.itid, t.tid, t.name AS thread_name "
                if include_scope
                else (
                    "NULL AS ipid, NULL AS pid, NULL AS process_name, "
                    "NULL AS itid, NULL AS tid, NULL AS thread_name "
                )
            )
            scope_predicate = ""
            parameters: list[str | int] = [marker]
            if include_scope and target_ipid is not None:
                scope_predicate = "AND p.ipid = ? "
                parameters.append(target_ipid)
            elif include_scope and target_process:
                scope_predicate = (
                    "AND INSTR(LOWER(COALESCE(p.name, '')), LOWER(?)) > 0 "
                )
                parameters.append(target_process)
            parameters.append(max_candidates + 1)
            rows = self._repository.query(
                "SELECT c.id, c.ts, c.dur, c.name, c.cat, c.depth, "
                f"c.callid, {scope_columns}"
                "FROM callstack AS c "
                f"{joins}"
                f"WHERE {predicate} {scope_predicate}"
                "ORDER BY c.ts, c.id LIMIT ?",
                parameters,
                max_rows=min(max_candidates + 1, 500),
            ).rows
            if rows:
                matches = [self._slice(row) for row in rows]
                selected_mode = mode
                break
        status = (
            "unique"
            if len(matches) == 1
            else "missing"
            if not matches
            else "ambiguous"
        )
        return {
            "requested": marker,
            "status": status,
            "match_mode": selected_mode,
            "matches": matches,
            "unique_candidate": matches[0] if len(matches) == 1 else None,
            "truncated": len(matches) > max_candidates,
        }

    @staticmethod
    def _duration_span_candidate(
        match: dict[str, Any],
        *,
        requested_from: str | None,
    ) -> dict[str, Any]:
        unique = match.get("unique_candidate")
        candidate = None
        status = match.get("status")
        if isinstance(unique, dict):
            start_ns = unique.get("point_timestamp_ns")
            end_ns = unique.get("slice_end_ns")
            if (
                isinstance(start_ns, int)
                and isinstance(end_ns, int)
                and end_ns > start_ns
            ):
                status = "resolved_unique_duration_slice"
                candidate = {
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "duration_ms": (end_ns - start_ns) / 1_000_000,
                    "candidate_kind": "application-duration-slice",
                    "source_id": unique.get("source_id"),
                    "name": unique.get("name"),
                    "ipid": unique.get("ipid"),
                    "itid": unique.get("itid"),
                    "requested_from": requested_from,
                    "requires_agent_validation": True,
                }
            else:
                status = "unique_but_not_duration_slice"
        return {
            "requested": match.get("requested"),
            "requested_from": requested_from,
            "status": status,
            "match_mode": match.get("match_mode"),
            "matches": match.get("matches", []),
            "candidate": candidate,
        }

    @staticmethod
    def _completion_input_boundary(
        candidate: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        name = str(candidate.get("name") or "")
        lowered = name.lower()
        use_slice_end = any(
            marker in lowered
            for marker in (
                "touchup",
                "touch up",
                "action:3",
                "action=3",
                "type=1",
            )
        )
        timestamp_ns = (
            candidate.get("slice_end_ns")
            if use_slice_end
            else candidate.get("point_timestamp_ns")
        )
        if not isinstance(timestamp_ns, int):
            return None
        return {
            **candidate,
            "timestamp_ns": timestamp_ns,
            "candidate_kind": "input-completion-point",
            "timestamp_rule": (
                "input Slice end" if use_slice_end else "input point ts"
            ),
            "requires_agent_validation": True,
        }

    @staticmethod
    def _select_discovery_window(
        *,
        explicit_time_range: dict[str, Any] | None,
        explicit_operation_span: dict[str, Any] | None,
        marker_pair: dict[str, Any] | None,
        implicit_operation_span: dict[str, Any] | None,
        duration_window: dict[str, Any] | None,
        input_boundary: dict[str, Any] | None,
        lookahead_ms: int,
        trace_range: dict[str, Any],
    ) -> dict[str, Any] | None:
        for source, candidate in (
            ("explicit-time-range", explicit_time_range),
            ("explicit-operation-duration-slice", explicit_operation_span),
            ("application-marker-pair", marker_pair),
            ("single-start-duration-slice", implicit_operation_span),
            ("input-plus-known-duration", duration_window),
        ):
            if not isinstance(candidate, dict):
                continue
            start_ns = candidate.get("start_ns")
            end_ns = candidate.get("end_ns")
            if (
                isinstance(start_ns, int)
                and isinstance(end_ns, int)
                and end_ns > start_ns
            ):
                return {
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "source": source,
                    "is_metric_candidate": True,
                }
        if not isinstance(input_boundary, dict):
            return None
        start_ns = input_boundary.get("timestamp_ns")
        if not isinstance(start_ns, int):
            return None
        requested_end = start_ns + lookahead_ms * 1_000_000
        trace_end = trace_range.get("end_ns")
        end_ns = (
            min(requested_end, trace_end)
            if isinstance(trace_end, int)
            else requested_end
        )
        if end_ns <= start_ns:
            return None
        return {
            "start_ns": start_ns,
            "end_ns": end_ns,
            "source": "input-plus-lookahead-discovery-only",
            "is_metric_candidate": False,
        }

    @staticmethod
    def _duration_window(
        *,
        input_boundary: dict[str, Any] | None,
        problem_duration_ms: float | None,
        fallback: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if (
            isinstance(input_boundary, dict)
            and isinstance(input_boundary.get("timestamp_ns"), int)
            and problem_duration_ms is not None
            and problem_duration_ms > 0
        ):
            start_ns = input_boundary["timestamp_ns"]
            end_ns = start_ns + round(problem_duration_ms * 1_000_000)
            return {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_ms": problem_duration_ms,
                "candidate_kind": "last-input-plus-user-duration",
                "single_operation_assumption": True,
                "metric_definition_from_user": True,
            }
        return fallback if isinstance(fallback, dict) else None

    def _process_candidates(
        self,
        tables: set[str],
        *,
        target_ipid: int | None,
        target_process: str,
        marker_matches: list[dict[str, Any]],
        discovery_window: dict[str, Any] | None,
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        if "process" not in tables:
            return []
        ipids = {
            int(item["ipid"])
            for item in marker_matches
            if isinstance(item.get("ipid"), int)
        }
        where: list[str] = []
        parameters: list[str | int] = []
        if target_ipid is not None:
            where.append("p.ipid = ?")
            parameters.append(target_ipid)
        elif target_process:
            where.append(
                "INSTR(LOWER(COALESCE(p.name, '')), LOWER(?)) > 0"
            )
            parameters.append(target_process)
        elif ipids:
            placeholders = ", ".join("?" for _ in ipids)
            where.append(f"p.ipid IN ({placeholders})")
            parameters.extend(sorted(ipids))
        elif discovery_window is not None and "frame_slice" in tables:
            return self._frame_process_candidates(
                discovery_window=discovery_window,
                max_candidates=max_candidates,
            )
        else:
            return []
        parameters.append(max_candidates)
        predicate = " AND ".join(where) or "1 = 1"
        thread_columns = (
            "COALESCE(MIN(CASE WHEN t.tid = p.pid THEN t.itid END), "
            "MIN(t.itid)) AS main_itid, "
            "COALESCE(MIN(CASE WHEN t.tid = p.pid THEN t.tid END), "
            "MIN(t.tid)) AS main_tid "
            if "thread" in tables
            else "NULL AS main_itid, NULL AS main_tid "
        )
        thread_join = (
            "LEFT JOIN thread AS t ON t.ipid = p.ipid "
            if "thread" in tables
            else ""
        )
        return self._repository.query(
            "SELECT p.ipid, p.pid, p.name AS process_name, "
            f"{thread_columns}"
            "FROM process AS p "
            f"{thread_join}"
            f"WHERE {predicate} "
            "GROUP BY p.ipid, p.pid, p.name "
            "ORDER BY p.ipid LIMIT ?",
            parameters,
            max_rows=max_candidates,
        ).rows

    def _frame_process_candidates(
        self,
        *,
        discovery_window: dict[str, Any],
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        return self._repository.query(
            "SELECT p.ipid, p.pid, p.name AS process_name, "
            "COUNT(*) AS actual_frame_count, MIN(f.ts) AS first_frame_ns, "
            "MAX(f.ts + MAX(COALESCE(f.dur, 0), 0)) AS last_frame_end_ns "
            "FROM frame_slice AS f "
            "JOIN process AS p ON p.ipid = f.ipid "
            "WHERE f.ts >= ? AND f.ts < ? "
            "AND LOWER(COALESCE(f.type_desc, '')) IN ('actual', 'actural') "
            "AND LOWER(COALESCE(p.name, '')) NOT IN "
            "('render_service', 'composer_host', 'bootanimation') "
            "GROUP BY p.ipid, p.pid, p.name "
            "ORDER BY actual_frame_count DESC, first_frame_ns, p.ipid LIMIT ?",
            [
                discovery_window["start_ns"],
                discovery_window["end_ns"],
                max_candidates,
            ],
            max_rows=max_candidates,
        ).rows

    def _frames(
        self,
        tables: set[str],
        *,
        target_ipid: int,
        start_ns: int,
        end_ns: int,
        max_candidates: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if "frame_slice" not in tables:
            return [], []
        frames = self._repository.query(
            "SELECT f.id, f.ts, f.dur, "
            "f.ts + MAX(COALESCE(f.dur, 0), 0) AS end_ns, "
            "f.vsync, f.ipid, f.itid, f.type_desc "
            "FROM frame_slice AS f "
            "WHERE f.ipid = ? AND f.ts >= ? AND f.ts < ? "
            "AND LOWER(COALESCE(f.type_desc, '')) IN ('actual', 'actural') "
            "ORDER BY f.ts, f.id LIMIT ?",
            [target_ipid, start_ns, end_ns, max_candidates],
            max_rows=max_candidates,
        ).rows
        serialized = [
            {
                **row,
                "source": "frame_slice",
                "source_id": f"frame_slice:{row.get('id')}",
            }
            for row in frames
        ]
        if "frame_maps" not in tables or not frames:
            return serialized, []
        frame_ids = [
            int(row["id"])
            for row in frames
            if isinstance(row.get("id"), int)
        ]
        placeholders = ", ".join("?" for _ in frame_ids)
        process_columns = (
            "p.name AS dst_process_name, "
            if "process" in tables
            else "NULL AS dst_process_name, "
        )
        process_join = (
            "LEFT JOIN process AS p ON p.ipid = dst.ipid "
            if "process" in tables
            else ""
        )
        links = self._repository.query(
            "SELECT m.id AS map_id, m.src_row, m.dst_row, "
            "dst.ts AS dst_ts, dst.dur AS dst_dur_ns, "
            "dst.ts + MAX(COALESCE(dst.dur, 0), 0) AS dst_end_ns, "
            "dst.ipid AS dst_ipid, dst.itid AS dst_itid, "
            f"{process_columns}dst.type_desc AS dst_type_desc "
            "FROM frame_maps AS m "
            "JOIN frame_slice AS dst ON dst.id = m.dst_row "
            f"{process_join}"
            f"WHERE m.src_row IN ({placeholders}) "
            "ORDER BY m.src_row, dst.ts, m.id LIMIT ?",
            [*frame_ids, min(max_candidates * 2, 500)],
            max_rows=min(max_candidates * 2, 500),
        ).rows
        return serialized, links

    def _animations(
        self,
        tables: set[str],
        *,
        start_ns: int,
        end_ns: int,
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        if not {"callstack", "thread", "process"} <= tables:
            return []
        predicates = " OR ".join(
            "LOWER(COALESCE(c.name, '')) LIKE ?"
            for _ in self._ANIMATION_PATTERNS
        )
        rows = self._repository.query(
            "SELECT c.id, c.ts, c.dur, "
            "c.ts + MAX(COALESCE(c.dur, 0), 0) AS end_ns, "
            "c.name, t.itid, t.tid, t.name AS thread_name, "
            "p.ipid, p.pid, p.name AS process_name "
            "FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
            "WHERE LOWER(COALESCE(p.name, '')) = 'render_service' "
            "AND c.ts < ? "
            "AND c.ts + MAX(COALESCE(c.dur, 0), 0) > ? "
            f"AND ({predicates}) "
            "ORDER BY c.ts, c.id LIMIT ?",
            [
                end_ns,
                start_ns,
                *(f"{pattern}%" for pattern in self._ANIMATION_PATTERNS),
                max_candidates,
            ],
            max_rows=max_candidates,
        ).rows
        return [
            {
                **row,
                "source": "callstack",
                "source_id": f"callstack:{row.get('id')}",
                "candidate_kind": "animation-end",
                "timestamp_ns": row.get("end_ns"),
                "confidence": 0.55,
                "requires_causal_validation": True,
            }
            for row in rows
            if isinstance(row.get("end_ns"), int)
            and row["end_ns"] > start_ns
        ]

    @staticmethod
    def _response_candidates(
        *,
        response_match: dict[str, Any],
        input_boundary: dict[str, Any] | None,
        frames: list[dict[str, Any]],
        frame_links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        marker = response_match.get("unique_candidate")
        if isinstance(marker, dict):
            candidates.append(
                {
                    "candidate_kind": "application-response-marker",
                    "timestamp_ns": marker.get("point_timestamp_ns"),
                    "source_id": marker.get("source_id"),
                    "name": marker.get("name"),
                    "confidence": 0.9,
                    "requires_agent_validation": True,
                }
            )
        input_ns = (
            input_boundary.get("timestamp_ns")
            if isinstance(input_boundary, dict)
            else None
        )
        eligible = [
            frame
            for frame in frames
            if not isinstance(input_ns, int) or frame.get("ts", 0) >= input_ns
        ]
        if not eligible:
            return candidates
        first = eligible[0]
        candidates.append(
            {
                "candidate_kind": "first-application-frame-completion",
                "timestamp_ns": first.get("end_ns"),
                "source_id": first.get("source_id"),
                "vsync": first.get("vsync"),
                "confidence": 0.5,
                "requires_effective_feedback_validation": True,
            }
        )
        mapped = [
            link
            for link in frame_links
            if link.get("src_row") == first.get("id")
            and isinstance(link.get("dst_end_ns"), int)
        ]
        if mapped:
            presentation = max(mapped, key=lambda item: item["dst_end_ns"])
            candidates.append(
                {
                    "candidate_kind": "first-mapped-presentation-completion",
                    "timestamp_ns": presentation["dst_end_ns"],
                    "source_id": f"frame_maps:{presentation.get('map_id')}",
                    "app_frame_source_id": first.get("source_id"),
                    "confidence": 0.65,
                    "requires_effective_feedback_validation": True,
                }
            )
        return candidates

    def _completion_candidates(
        self,
        *,
        operation_span: dict[str, Any],
        completion_match: dict[str, Any],
        duration_window: dict[str, Any] | None,
        frames: list[dict[str, Any]],
        frame_links: list[dict[str, Any]],
        animations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        span = operation_span.get("candidate")
        if isinstance(span, dict):
            candidates.append(
                {
                    "candidate_kind": "application-duration-slice-end",
                    "timestamp_ns": span["end_ns"],
                    "source_id": span.get("source_id"),
                    "name": span.get("name"),
                    "confidence": 0.95,
                    "requires_business_contract_validation": True,
                }
            )
        marker = completion_match.get("unique_candidate")
        if isinstance(marker, dict):
            candidates.append(
                {
                    "candidate_kind": "application-completion-marker",
                    "timestamp_ns": marker.get("point_timestamp_ns"),
                    "source_id": marker.get("source_id"),
                    "name": marker.get("name"),
                    "confidence": 0.9,
                    "requires_business_contract_validation": True,
                }
            )
        if isinstance(duration_window, dict) and isinstance(
            duration_window.get("end_ns"), int
        ):
            explicit = (
                duration_window.get("candidate_kind")
                == "explicit-time-range"
            )
            candidates.append(
                {
                    "candidate_kind": (
                        "explicit-time-range"
                        if explicit
                        else "input-plus-user-duration"
                    ),
                    "timestamp_ns": duration_window["end_ns"],
                    "confidence": 1.0 if explicit else 0.7,
                    "metric_definition_from_user": True,
                    "single_operation_assumption": not explicit,
                    "requires_input_validation": not explicit,
                }
            )
        candidates.extend(animations)

        links_by_source: dict[int, list[dict[str, Any]]] = {}
        for link in frame_links:
            source = link.get("src_row")
            if isinstance(source, int):
                links_by_source.setdefault(source, []).append(link)
        for index, frame in enumerate(frames[:-1]):
            ts = frame.get("ts")
            end_ns = frame.get("end_ns")
            next_ts = frames[index + 1].get("ts")
            if not all(
                isinstance(value, int) for value in (ts, end_ns, next_ts)
            ):
                continue
            previous = sum(
                1
                for prior in frames[:index]
                if isinstance(prior.get("ts"), int)
                and prior["ts"] >= ts - self._FRAME_ACTIVITY_WINDOW_NS
            )
            gap_ns = next_ts - end_ns
            if (
                previous < self._MIN_ACTIVE_PREVIOUS_FRAMES
                or gap_ns < self._QUIET_GAP_NS
            ):
                continue
            mapped_ends = [
                link["dst_end_ns"]
                for link in links_by_source.get(int(frame["id"]), [])
                if isinstance(link.get("dst_end_ns"), int)
            ]
            timestamp_ns = max([end_ns, *mapped_ends])
            candidates.append(
                {
                    "candidate_kind": "frame-quiescence-heuristic",
                    "timestamp_ns": timestamp_ns,
                    "source_id": frame.get("source_id"),
                    "vsync": frame.get("vsync"),
                    "previous_frames_in_300ms": previous,
                    "gap_to_next_frame_ms": gap_ns / 1_000_000,
                    "confidence": 0.45,
                    "discovery_only": True,
                    "does_not_prove_business_completion": True,
                }
            )
        if frames:
            final = frames[-1]
            candidates.append(
                {
                    "candidate_kind": "window-final-frame-fallback",
                    "timestamp_ns": final.get("end_ns"),
                    "source_id": final.get("source_id"),
                    "vsync": final.get("vsync"),
                    "confidence": 0.2,
                    "discovery_only": True,
                    "does_not_prove_business_completion": True,
                }
            )
        return candidates

    @staticmethod
    def _slice(row: dict[str, Any]) -> dict[str, Any]:
        ts = row.get("ts")
        dur = row.get("dur")
        end_ns = (
            ts + max(dur, 0)
            if isinstance(ts, int) and isinstance(dur, int)
            else None
        )
        return {
            "source": "callstack",
            "source_id": f"callstack:{row.get('id')}",
            "id": row.get("id"),
            "name": row.get("name"),
            "cat": row.get("cat"),
            "point_timestamp_ns": ts,
            "slice_end_ns": end_ns,
            "duration_ns": max(dur, 0) if isinstance(dur, int) else 0,
            "depth": row.get("depth"),
            "callid": row.get("callid"),
            "ipid": row.get("ipid"),
            "pid": row.get("pid"),
            "process_name": row.get("process_name"),
            "itid": row.get("itid"),
            "tid": row.get("tid"),
            "thread_name": row.get("thread_name"),
        }
