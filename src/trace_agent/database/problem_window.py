from __future__ import annotations

from pathlib import Path
from typing import Any

from trace_agent.database.repository import SQLiteTraceRepository


class ProblemWindowRepository:
    """Read bounded candidates for a scenario-independent problem interval."""

    _INPUT_PREDICATE = """
        (
            LOWER(COALESCE(c.name, '')) LIKE '%touchevent%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%touch event%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%pointerevent%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%pointer event%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%pointevent%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%clickevent%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%click event%'
            OR LOWER(COALESCE(c.name, '')) LIKE '%click_recognize%'
        )
        AND LOWER(COALESCE(c.name, '')) NOT LIKE '%feature detection%'
        AND LOWER(COALESCE(c.name, '')) NOT LIKE '%featuredetection%'
        AND LOWER(COALESCE(c.name, '')) NOT LIKE '%listener%'
        AND LOWER(COALESCE(c.name, '')) NOT LIKE '%callback%'
        AND LOWER(COALESCE(c.name, '')) NOT LIKE '%register%'
    """

    def __init__(self, database_path: Path) -> None:
        self._repository = SQLiteTraceRepository(database_path)

    def inspect(
        self,
        *,
        target_process: str | None = None,
        start_marker: str | None = None,
        end_marker: str | None = None,
        problem_duration_ms: float | None = None,
        max_candidates: int = 50,
    ) -> dict[str, Any]:
        tables = self._table_names()
        trace_range = self._trace_range(tables)
        limitations: list[str] = []

        if "callstack" not in tables:
            return {
                "trace_range": trace_range,
                "application_marker_pair": {
                    "status": "unavailable",
                    "start": self._empty_marker(start_marker),
                    "end": self._empty_marker(end_marker),
                    "candidate": None,
                },
                "last_input_point": {
                    "candidate": None,
                    "candidates": [],
                    "single_operation_assumption": True,
                },
                "duration_window": None,
                "default_candidate": None,
                "decision_policy": self._decision_policy(),
                "limitations": [
                    "callstack 表不存在，无法发现 Slice Marker 或输入打点。"
                ],
            }

        can_filter_process = {"thread", "process"} <= tables
        normalized_target = (target_process or "").strip()
        if normalized_target and not can_filter_process:
            limitations.append(
                "thread/process 表不完整，应用 Marker 无法按目标进程过滤。"
            )

        start = self._match_marker(
            start_marker,
            target_process=(
                normalized_target if can_filter_process else None
            ),
            include_process=can_filter_process,
            max_candidates=max_candidates,
        )
        end = self._match_marker(
            end_marker,
            target_process=(
                normalized_target if can_filter_process else None
            ),
            include_process=can_filter_process,
            max_candidates=max_candidates,
        )
        marker_pair = self._build_marker_pair(start, end)
        if marker_pair["status"] == "invalid_order":
            limitations.append("唯一应用结束 Marker 不晚于开始 Marker。")

        input_candidates = self._input_candidates(
            include_process=can_filter_process,
            max_candidates=max_candidates,
        )
        last_input = input_candidates[0] if input_candidates else None
        duration_window = self._duration_window(
            last_input,
            problem_duration_ms=problem_duration_ms,
            trace_range=trace_range,
        )
        if (
            duration_window is not None
            and not duration_window["fully_captured"]
        ):
            limitations.append(
                "按输入起点和问题持续时间推导的终点超出 Trace，"
                "候选区间已裁剪且不完整。"
            )

        default_candidate = marker_pair["candidate"] or duration_window
        if default_candidate is None and last_input is not None:
            limitations.append(
                "已找到默认输入起点，但缺少问题持续时间或可靠结束 Marker，"
                "暂不能组成完整问题区间。"
            )

        return {
            "trace_range": trace_range,
            "application_marker_pair": marker_pair,
            "last_input_point": {
                "candidate": last_input,
                "candidates": input_candidates,
                "single_operation_assumption": True,
                "selection_rule": (
                    "在排除配置、监听器和注册类噪声后，选择 Trace 中"
                    "时间最晚的 TouchEvent、PointerEvent 或点击打点。"
                ),
            },
            "duration_window": duration_window,
            "default_candidate": default_candidate,
            "decision_policy": self._decision_policy(),
            "limitations": limitations,
        }

    def _table_names(self) -> set[str]:
        rows = self._repository.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
            max_rows=500,
        ).rows
        return {
            str(row["name"])
            for row in rows
            if isinstance(row.get("name"), str)
        }

    def _trace_range(self, tables: set[str]) -> dict[str, int | None]:
        if "trace_range" in tables:
            rows = self._repository.query(
                "SELECT MIN(start_ts) AS start_ns, "
                "MAX(end_ts) AS end_ns FROM trace_range",
                max_rows=1,
            ).rows
            if rows:
                return {
                    "start_ns": self._as_int(rows[0].get("start_ns")),
                    "end_ns": self._as_int(rows[0].get("end_ns")),
                }
        if "callstack" in tables:
            rows = self._repository.query(
                "SELECT MIN(ts) AS start_ns, "
                "MAX(ts + MAX(COALESCE(dur, 0), 0)) AS end_ns "
                "FROM callstack",
                max_rows=1,
            ).rows
            if rows:
                return {
                    "start_ns": self._as_int(rows[0].get("start_ns")),
                    "end_ns": self._as_int(rows[0].get("end_ns")),
                }
        return {"start_ns": None, "end_ns": None}

    def _match_marker(
        self,
        marker: str | None,
        *,
        target_process: str | None,
        include_process: bool,
        max_candidates: int,
    ) -> dict[str, Any]:
        normalized = (marker or "").strip()
        if not normalized:
            return self._empty_marker(marker)

        exact = self._query_marker(
            normalized,
            match_mode="exact",
            target_process=target_process,
            include_process=include_process,
            max_candidates=max_candidates,
        )
        matches = exact
        match_mode = "exact"
        if not matches:
            matches = self._query_marker(
                normalized,
                match_mode="contains",
                target_process=target_process,
                include_process=include_process,
                max_candidates=max_candidates,
            )
            match_mode = "contains"

        return {
            "requested": normalized,
            "status": (
                "unique"
                if len(matches) == 1
                else "missing"
                if not matches
                else "ambiguous"
            ),
            "match_mode": match_mode if matches else None,
            "matches": matches,
            "unique_candidate": matches[0] if len(matches) == 1 else None,
            "truncated": len(matches) > max_candidates,
        }

    def _query_marker(
        self,
        marker: str,
        *,
        match_mode: str,
        target_process: str | None,
        include_process: bool,
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        select_process = (
            ", p.ipid, p.pid, p.name AS process_name, "
            "t.itid, t.tid, t.name AS thread_name "
            if include_process
            else ", NULL AS ipid, NULL AS pid, NULL AS process_name, "
            "NULL AS itid, NULL AS tid, NULL AS thread_name "
        )
        joins = (
            "JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
            if include_process
            else ""
        )
        predicate = (
            "LOWER(TRIM(COALESCE(c.name, ''))) = LOWER(TRIM(?))"
            if match_mode == "exact"
            else "INSTR(LOWER(COALESCE(c.name, '')), LOWER(?)) > 0"
        )
        process_predicate = ""
        parameters: list[str | int] = [marker]
        if target_process:
            process_predicate = (
                "AND INSTR(LOWER(COALESCE(p.name, '')), LOWER(?)) > 0 "
            )
            parameters.append(target_process)
        parameters.append(max_candidates + 1)

        rows = self._repository.query(
            "SELECT c.id, c.ts, c.dur, c.ts + MAX(COALESCE(c.dur, 0), 0) "
            "AS slice_end_ns, c.name, c.cat, c.depth, c.callid "
            f"{select_process}"
            "FROM callstack AS c "
            f"{joins}"
            f"WHERE {predicate} "
            f"{process_predicate}"
            "ORDER BY c.ts, c.id LIMIT ?",
            parameters,
            max_rows=min(max_candidates + 1, 500),
        ).rows
        return [self._slice_candidate(row) for row in rows]

    def _input_candidates(
        self,
        *,
        include_process: bool,
        max_candidates: int,
    ) -> list[dict[str, Any]]:
        select_process = (
            ", p.ipid, p.pid, p.name AS process_name, "
            "t.itid, t.tid, t.name AS thread_name "
            if include_process
            else ", NULL AS ipid, NULL AS pid, NULL AS process_name, "
            "NULL AS itid, NULL AS tid, NULL AS thread_name "
        )
        joins = (
            "JOIN thread AS t ON t.itid = c.callid "
            "JOIN process AS p ON p.ipid = t.ipid "
            if include_process
            else ""
        )
        rows = self._repository.query(
            "SELECT c.id, c.ts, c.dur, c.name, c.cat, c.depth, c.callid, "
            "CASE "
            "WHEN LOWER(COALESCE(c.name, '')) LIKE '%pointerevent%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%pointer event%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%pointevent%' "
            "THEN 'pointer-event' "
            "WHEN LOWER(COALESCE(c.name, '')) LIKE '%touchevent%' "
            "OR LOWER(COALESCE(c.name, '')) LIKE '%touch event%' "
            "THEN 'touch-event' "
            "ELSE 'click-marker' END AS signal_kind "
            f"{select_process}"
            "FROM callstack AS c "
            f"{joins}"
            f"WHERE {self._INPUT_PREDICATE} "
            "ORDER BY c.ts DESC, c.id DESC LIMIT ?",
            [max_candidates],
            max_rows=max_candidates,
        ).rows
        return [
            {
                **self._slice_candidate(row),
                "signal_kind": row.get("signal_kind"),
                "point_timestamp_ns": self._as_int(row.get("ts")),
                "candidate_kind": "last-input-point",
            }
            for row in rows
        ]

    @staticmethod
    def _build_marker_pair(
        start: dict[str, Any],
        end: dict[str, Any],
    ) -> dict[str, Any]:
        start_candidate = start.get("unique_candidate")
        end_candidate = end.get("unique_candidate")
        if not start.get("requested") or not end.get("requested"):
            status = "not_requested"
            candidate = None
        elif start_candidate is None or end_candidate is None:
            status = "unresolved"
            candidate = None
        else:
            start_ns = start_candidate.get("point_timestamp_ns")
            end_ns = end_candidate.get("point_timestamp_ns")
            if not isinstance(start_ns, int) or not isinstance(end_ns, int):
                status = "unresolved"
                candidate = None
            elif end_ns <= start_ns:
                status = "invalid_order"
                candidate = None
            else:
                status = "resolved_unique_pair"
                candidate = {
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "duration_ms": (end_ns - start_ns) / 1_000_000,
                    "candidate_kind": "application-slice-pair",
                    "start_source_id": start_candidate["source_id"],
                    "end_source_id": end_candidate["source_id"],
                    "fully_captured": True,
                    "requires_agent_validation": True,
                }
        return {
            "status": status,
            "start": start,
            "end": end,
            "candidate": candidate,
            "selection_rule": (
                "仅当开始和结束 Slice 在目标范围内各自唯一且顺序有效时，"
                "才组成应用自定义问题区间；取两个 Slice 的 ts 作为点时间。"
            ),
        }

    @staticmethod
    def _duration_window(
        last_input: dict[str, Any] | None,
        *,
        problem_duration_ms: float | None,
        trace_range: dict[str, int | None],
    ) -> dict[str, Any] | None:
        if last_input is None or problem_duration_ms is None:
            return None
        start_ns = last_input.get("point_timestamp_ns")
        if not isinstance(start_ns, int):
            return None
        requested_end_ns = start_ns + round(problem_duration_ms * 1_000_000)
        trace_end_ns = trace_range.get("end_ns")
        fully_captured = (
            not isinstance(trace_end_ns, int)
            or requested_end_ns <= trace_end_ns
        )
        end_ns = (
            min(requested_end_ns, trace_end_ns)
            if isinstance(trace_end_ns, int)
            else requested_end_ns
        )
        return {
            "start_ns": start_ns,
            "end_ns": end_ns,
            "requested_end_ns": requested_end_ns,
            "duration_ms": (end_ns - start_ns) / 1_000_000,
            "requested_duration_ms": problem_duration_ms,
            "candidate_kind": "last-input-plus-duration",
            "start_source_id": last_input["source_id"],
            "fully_captured": fully_captured,
            "single_operation_assumption": True,
            "requires_agent_validation": True,
        }

    @staticmethod
    def _slice_candidate(row: dict[str, Any]) -> dict[str, Any]:
        ts = ProblemWindowRepository._as_int(row.get("ts"))
        dur = ProblemWindowRepository._as_int(row.get("dur")) or 0
        return {
            "source": "callstack",
            "source_id": f"callstack:{row.get('id')}",
            "id": ProblemWindowRepository._as_int(row.get("id")),
            "name": row.get("name"),
            "cat": row.get("cat"),
            "point_timestamp_ns": ts,
            "slice_end_ns": (
                ProblemWindowRepository._as_int(row.get("slice_end_ns"))
                if row.get("slice_end_ns") is not None
                else ts + max(dur, 0)
                if ts is not None
                else None
            ),
            "duration_ns": max(dur, 0),
            "depth": ProblemWindowRepository._as_int(row.get("depth")),
            "callid": ProblemWindowRepository._as_int(row.get("callid")),
            "ipid": ProblemWindowRepository._as_int(row.get("ipid")),
            "pid": ProblemWindowRepository._as_int(row.get("pid")),
            "process_name": row.get("process_name"),
            "itid": ProblemWindowRepository._as_int(row.get("itid")),
            "tid": ProblemWindowRepository._as_int(row.get("tid")),
            "thread_name": row.get("thread_name"),
        }

    @staticmethod
    def _empty_marker(marker: str | None) -> dict[str, Any]:
        normalized = (marker or "").strip()
        return {
            "requested": normalized or None,
            "status": "not_requested" if not normalized else "missing",
            "match_mode": None,
            "matches": [],
            "unique_candidate": None,
            "truncated": False,
        }

    @staticmethod
    def _decision_policy() -> list[str]:
        return [
            "显式 time_range 始终优先，由 Agent 按用户定义验证。",
            "应用自定义起止 Slice 各自唯一时，优先采用该 Marker 对。",
            "场景专用语义边界优先于通用输入兜底；冷启动结束可由应用定义的"
            "首页稳定帧或唯一 Slice 表示，不强制等于 Trace 最早帧。",
            "缺少显式边界时，默认假设本 Trace 只记录一次用户操作，"
            "使用最后一个有效输入打点作为起点。",
            "只有提供 problem_duration_ms 时，才用输入起点加持续时间"
            "组成完整候选区间。",
            "所有候选都需要 Agent 结合业务语义、因果链和 Trace 覆盖范围确认。",
        ]

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None
