from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable


class FrameJankRepository:
    """Build refresh-aware frame facts for one application pipeline.

    The repository deliberately stops at observable pipeline delay.  It does
    not turn a long application/RenderService stage into a root-cause claim;
    scheduling, slices, wakeups, and Perf evidence are required for that.
    """

    _ACTUAL_TYPES = frozenset({"actual", "actural"})
    _EXPECTED_TYPES = frozenset({"expect", "expected"})
    _COMMON_REFRESH_RATES = (
        24.0,
        30.0,
        45.0,
        48.0,
        50.0,
        60.0,
        72.0,
        75.0,
        90.0,
        100.0,
        120.0,
        144.0,
        165.0,
    )
    _MAX_FRAME_ROWS = 20_000
    _ACTIVE_GAP_NS = 200_000_000

    def __init__(self, database_path: Path) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(f"Trace 数据库不存在：{database_path}")
        self._database_path = database_path.resolve()

    def inspect(
        self,
        *,
        target_ipid: int,
        interval_start_ns: int,
        interval_end_ns: int,
        refresh_rate_hz: float | None,
        frame_producer_itid: int | None,
        max_bad_frames: int,
        max_clusters: int,
    ) -> dict[str, Any]:
        uri = f"{self._database_path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            tables = self._tables(connection)
            if "frame_slice" not in tables:
                return self._unavailable(
                    target_ipid=target_ipid,
                    start_ns=interval_start_ns,
                    end_ns=interval_end_ns,
                    reason="Trace DB 缺少 frame_slice",
                )

            frame_columns = self._columns(connection, "frame_slice")
            required = {"id", "ts", "dur", "ipid", "itid"}
            missing = sorted(required - frame_columns)
            if missing:
                return self._unavailable(
                    target_ipid=target_ipid,
                    start_ns=interval_start_ns,
                    end_ns=interval_end_ns,
                    reason="frame_slice 缺少字段：" + ", ".join(missing),
                )

            process = self._process_identity(connection, target_ipid)
            raw_frames, truncated = self._application_frames(
                connection,
                frame_columns=frame_columns,
                target_ipid=target_ipid,
                start_ns=interval_start_ns,
                end_ns=interval_end_ns,
            )
            actual_frames = [
                row for row in raw_frames if self._is_actual(row)
            ]
            expected_frames = [
                row for row in raw_frames if self._is_expected(row)
            ]
            producer_candidates = self._producer_candidates(actual_frames)
            selected_itid, selection = self._select_producer(
                producer_candidates,
                requested_itid=frame_producer_itid,
            )
            if selected_itid is None:
                limitations = [
                    "问题区间没有目标应用的 actual/actural frame_slice，无法计算帧率。"
                ]
                if truncated:
                    limitations.append("frame_slice 行数达到安全上限，结果可能不完整。")
                return {
                    "available": False,
                    "target_process": process,
                    "interval": {
                        "start_ns": interval_start_ns,
                        "end_ns": interval_end_ns,
                        "duration_ms": (
                            interval_end_ns - interval_start_ns
                        )
                        / 1_000_000.0,
                    },
                    "pipeline_candidates": producer_candidates,
                    "selected_pipeline": selection,
                    "limitations": limitations,
                }

            actual_frames = [
                row for row in actual_frames if row["itid"] == selected_itid
            ]
            expected_frames = [
                row
                for row in expected_frames
                if row["itid"] == selected_itid
            ]
            mappings = self._mapped_render_frames(
                connection,
                tables=tables,
                frame_columns=frame_columns,
                target_ipid=target_ipid,
                producer_itid=selected_itid,
                start_ns=interval_start_ns,
                end_ns=interval_end_ns,
            )
            render_expectations = self._render_expectations(
                connection,
                frame_columns=frame_columns,
                expected_frames=expected_frames,
            )
            render_service_interval = self._render_service_interval_summary(
                connection,
                tables=tables,
                frame_columns=frame_columns,
                start_ns=interval_start_ns,
                end_ns=interval_end_ns,
            )
            gpu_by_frame = self._gpu_durations(connection, tables=tables)

            cadence_segments, cadence_limitations = self._cadence_segments(
                expected_frames=expected_frames,
                actual_frames=actual_frames,
                start_ns=interval_start_ns,
                end_ns=interval_end_ns,
                requested_refresh_rate_hz=refresh_rate_hz,
            )
            dominant_refresh_hz = self._dominant_refresh(cadence_segments)
            dominant_budget_ns = (
                int(round(1_000_000_000 / dominant_refresh_hz))
                if dominant_refresh_hz is not None
                else None
            )
            frame_facts = self._frame_facts(
                actual_frames=actual_frames,
                expected_frames=expected_frames,
                mappings=mappings,
                render_expectations=render_expectations,
                gpu_by_frame=gpu_by_frame,
                cadence_segments=cadence_segments,
                dominant_budget_ns=dominant_budget_ns,
            )
            frame_metrics = self._frame_metrics(
                frame_facts,
                expected_frames=expected_frames,
                interval_start_ns=interval_start_ns,
                interval_end_ns=interval_end_ns,
                dominant_budget_ns=dominant_budget_ns,
                render_service_interval=render_service_interval,
            )
            bad_frames = sorted(
                (item for item in frame_facts if item["is_jank"]),
                key=lambda item: (
                    -item["missed_vsyncs"],
                    -item["presentation_lateness_ms"],
                    -item["application_duration_ms"],
                    item["application_start_ns"],
                ),
            )
            clusters = self._bad_frame_clusters(
                bad_frames,
                dominant_budget_ns=dominant_budget_ns,
                limit=max_clusters,
            )
            architecture = self._render_architecture(
                connection,
                tables=tables,
                process=process,
                target_ipid=target_ipid,
                start_ns=interval_start_ns,
                end_ns=interval_end_ns,
                producer_candidates=producer_candidates,
                selected_itid=selected_itid,
                mappings=mappings,
            )
            hidump = self._hidump_samples(
                connection,
                tables=tables,
                start_ns=interval_start_ns,
                end_ns=interval_end_ns,
            )

            limitations = [*cadence_limitations]
            if truncated:
                limitations.append(
                    f"frame_slice 超过 {self._MAX_FRAME_ROWS} 行，已执行有界截断。"
                )
            if not mappings:
                limitations.append(
                    "没有通过 frame_maps 找到目标应用帧的 RenderService 映射；"
                    "应用产帧指标可用，但最终呈现 FPS 和 RS 阶段不可证明。"
                )
            if "gpu_slice" not in tables or not gpu_by_frame:
                limitations.append(
                    "问题区间缺少可关联的 gpu_slice；不得诊断 GPU 根因。"
                )
            if not architecture["surface_identity_available"]:
                limitations.append(
                    "当前 frame_slice/frame_maps 未提供可验证的 Surface 标识；"
                    "当同一进程存在多个产帧管线时需显式选择 frame_producer_itid。"
                )
            if (
                architecture["frame_producer_ui_mismatch"]["status"]
                == "strong-ui-candidate-differs-from-frame-owner"
            ):
                mismatch = architecture["frame_producer_ui_mismatch"]
                limitations.append(
                    "frame_slice 归属线程与有绘制/送帧证据的框架 UI 候选线程不同"
                    f"（{mismatch['ui_candidate_thread_name']}/"
                    f"tid={mismatch['ui_candidate_tid']}）；当前帧数只能描述已选中的"
                    "平台或包装层产帧管线，不能作为目标 UI Surface 的确定 FPS。"
                )

            return {
                "available": True,
                "target_process": process,
                "interval": {
                    "start_ns": interval_start_ns,
                    "end_ns": interval_end_ns,
                    "duration_ms": (
                        interval_end_ns - interval_start_ns
                    )
                    / 1_000_000.0,
                    "frame_boundary_rule": (
                        "count actual application frames whose start timestamp "
                        "is in [start_ns, end_ns)"
                    ),
                },
                "pipeline_candidates": producer_candidates,
                "selected_pipeline": selection,
                "render_architecture": architecture,
                "cadence": {
                    "segments": cadence_segments,
                    "dominant_refresh_rate_hz": dominant_refresh_hz,
                    "dominant_frame_budget_ms": (
                        dominant_budget_ns / 1_000_000.0
                        if dominant_budget_ns is not None
                        else None
                    ),
                    "dynamic_refresh_detected": len(
                        {item["refresh_rate_hz"] for item in cadence_segments}
                    )
                    > 1,
                },
                "metrics": frame_metrics,
                "bad_frames": bad_frames[:max_bad_frames],
                "bad_frame_count": len(bad_frames),
                "clusters": clusters,
                "hidump": hidump,
                "recommended_follow_up": {
                    "inspect_exact_windows": [
                        {
                            "start_ns": item["analysis_start_ns"],
                            "end_ns": item["analysis_end_ns"],
                            "application_itid": item["application_itid"],
                            "render_itid": item["render_itid"],
                            "delay_stage": item["observable_delay_stage"],
                        }
                        for item in bad_frames[: min(max_bad_frames, 8)]
                    ],
                    "perf_thread_ids": architecture[
                        "recommended_perf_thread_ids"
                    ],
                    "instruction": (
                        "对最差帧或卡顿簇的精确窗口复用线程状态、Slice、"
                        "唤醒链与 Perf 工具；observable_delay_stage 只是流水线"
                        "定位，不是根因。"
                    ),
                },
                "limitations": list(dict.fromkeys(limitations)),
            }

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0]).lower()
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'view')"
            )
        }

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1]).lower()
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    @staticmethod
    def _unavailable(
        *, target_ipid: int, start_ns: int, end_ns: int, reason: str
    ) -> dict[str, Any]:
        return {
            "available": False,
            "target_process": {"ipid": target_ipid},
            "interval": {
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_ms": (end_ns - start_ns) / 1_000_000.0,
            },
            "limitations": [reason],
        }

    @staticmethod
    def _process_identity(
        connection: sqlite3.Connection, target_ipid: int
    ) -> dict[str, Any]:
        tables = FrameJankRepository._tables(connection)
        if "process" not in tables:
            return {"ipid": target_ipid, "pid": None, "name": "unknown"}
        row = connection.execute(
            "SELECT ipid, pid, name FROM process WHERE ipid = ? LIMIT 1",
            (target_ipid,),
        ).fetchone()
        if row is None:
            return {"ipid": target_ipid, "pid": None, "name": "unknown"}
        return {
            "ipid": int(row["ipid"]),
            "pid": int(row["pid"]) if row["pid"] is not None else None,
            "name": str(row["name"] or "unknown"),
        }

    def _application_frames(
        self,
        connection: sqlite3.Connection,
        *,
        frame_columns: set[str],
        target_ipid: int,
        start_ns: int,
        end_ns: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        expressions = self._frame_select_expressions(
            frame_columns, alias="f"
        )
        joins = ""
        if {"thread", "process"} <= self._tables(connection):
            joins = (
                " LEFT JOIN thread AS t ON t.itid = f.itid "
                "LEFT JOIN process AS p ON p.ipid = t.ipid "
            )
            identity = (
                ", t.tid AS os_tid, t.name AS thread_name, "
                "p.pid AS os_pid, p.name AS process_name"
            )
        else:
            identity = (
                ", NULL AS os_tid, NULL AS thread_name, "
                "NULL AS os_pid, NULL AS process_name"
            )
        rows = connection.execute(
            f"SELECT {expressions}{identity} FROM frame_slice AS f{joins}"
            "WHERE f.ipid = ? AND f.ts >= ? AND f.ts < ? "
            "AND f.dur IS NOT NULL AND f.dur >= 0 "
            "ORDER BY f.ts, f.id LIMIT ?",
            (target_ipid, start_ns, end_ns, self._MAX_FRAME_ROWS + 1),
        ).fetchall()
        truncated = len(rows) > self._MAX_FRAME_ROWS
        return [dict(row) for row in rows[: self._MAX_FRAME_ROWS]], truncated

    @staticmethod
    def _frame_select_expressions(
        columns: set[str], *, alias: str
    ) -> str:
        def expr(name: str, default: str = "NULL") -> str:
            return (
                f"{alias}.{name} AS {name}"
                if name in columns
                else f"{default} AS {name}"
            )

        return ", ".join(
            [
                expr("id"),
                expr("ts"),
                expr("dur"),
                expr("vsync"),
                expr("ipid"),
                expr("itid"),
                expr("type"),
                expr("type_desc", "''"),
                expr("flag"),
                expr("src"),
                expr("dst"),
                expr("frame_no"),
            ]
        )

    @classmethod
    def _is_actual(cls, row: dict[str, Any]) -> bool:
        value = str(row.get("type_desc") or "").strip().lower()
        return value in cls._ACTUAL_TYPES or (not value and row.get("type") == 0)

    @classmethod
    def _is_expected(cls, row: dict[str, Any]) -> bool:
        value = str(row.get("type_desc") or "").strip().lower()
        return value in cls._EXPECTED_TYPES or (not value and row.get("type") == 1)

    @staticmethod
    def _producer_candidates(
        actual_frames: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in actual_frames:
            if row.get("itid") is not None:
                grouped[int(row["itid"])].append(row)
        candidates = []
        for itid, rows in grouped.items():
            first = rows[0]
            candidates.append(
                {
                    "itid": itid,
                    "tid": (
                        int(first["os_tid"])
                        if first.get("os_tid") is not None
                        else None
                    ),
                    "thread_name": str(first.get("thread_name") or "unknown"),
                    "actual_frame_count": len(rows),
                    "total_frame_duration_ms": sum(
                        int(row["dur"]) for row in rows
                    )
                    / 1_000_000.0,
                    "first_frame_ns": min(int(row["ts"]) for row in rows),
                    "last_frame_ns": max(int(row["ts"]) for row in rows),
                    "evidence": "frame_slice actual-frame ownership",
                }
            )
        candidates.sort(
            key=lambda item: (
                -item["actual_frame_count"],
                -item["total_frame_duration_ms"],
                item["itid"],
            )
        )
        return candidates

    @staticmethod
    def _select_producer(
        candidates: list[dict[str, Any]], *, requested_itid: int | None
    ) -> tuple[int | None, dict[str, Any]]:
        if requested_itid is not None:
            candidate = next(
                (item for item in candidates if item["itid"] == requested_itid),
                None,
            )
            if candidate is None:
                return None, {
                    "status": "requested-producer-not-found",
                    "requested_itid": requested_itid,
                }
            return requested_itid, {
                **candidate,
                "status": "selected",
                "selection_rule": "explicit frame_producer_itid",
                "confidence": 1.0,
            }
        if not candidates:
            return None, {"status": "unavailable"}
        candidate = candidates[0]
        total = sum(item["actual_frame_count"] for item in candidates)
        share = candidate["actual_frame_count"] / total if total else 0.0
        return int(candidate["itid"]), {
            **candidate,
            "status": "selected",
            "selection_rule": "dominant actual-frame producer in interval",
            "confidence": round(0.65 + min(share, 1.0) * 0.3, 3),
            "candidate_frame_share": round(share, 6),
            "main_thread_assumed": False,
        }

    def _mapped_render_frames(
        self,
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        frame_columns: set[str],
        target_ipid: int,
        producer_itid: int,
        start_ns: int,
        end_ns: int,
    ) -> dict[int, list[dict[str, Any]]]:
        if "frame_maps" not in tables:
            return {}
        render_expr = self._frame_select_expressions(
            frame_columns, alias="render"
        )
        joins = ""
        identity = (
            ", NULL AS render_tid, NULL AS render_thread_name, "
            "NULL AS render_pid, NULL AS render_process_name"
        )
        if {"thread", "process"} <= tables:
            joins = (
                " LEFT JOIN thread AS rt ON rt.itid = render.itid "
                "LEFT JOIN process AS rp ON rp.ipid = rt.ipid "
            )
            identity = (
                ", rt.tid AS render_tid, rt.name AS render_thread_name, "
                "rp.pid AS render_pid, rp.name AS render_process_name"
            )
        rows = connection.execute(
            "SELECT app.id AS app_frame_id, fm.id AS frame_map_id, "
            f"{render_expr}{identity} "
            "FROM frame_slice AS app "
            "JOIN frame_maps AS fm ON fm.src_row = app.id "
            "JOIN frame_slice AS render ON render.id = fm.dst_row "
            f"{joins}"
            "WHERE app.ipid = ? AND app.itid = ? "
            "AND app.ts >= ? AND app.ts < ? ORDER BY app.ts, render.ts",
            (target_ipid, producer_itid, start_ns, end_ns),
        ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for raw in rows:
            row = dict(raw)
            if self._is_actual(row):
                grouped[int(row["app_frame_id"])].append(row)
        return dict(grouped)

    def _render_expectations(
        self,
        connection: sqlite3.Connection,
        *,
        frame_columns: set[str],
        expected_frames: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        ids = sorted(
            {
                int(row["dst"])
                for row in expected_frames
                if row.get("dst") is not None and int(row["dst"]) > 0
            }
        )
        if not ids:
            return {}
        result: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(ids), 400):
            batch = ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            expressions = self._frame_select_expressions(
                frame_columns, alias="f"
            )
            rows = connection.execute(
                f"SELECT {expressions} FROM frame_slice AS f "
                f"WHERE f.id IN ({placeholders})",
                batch,
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                if self._is_expected(row):
                    result[int(row["id"])] = row
        return result

    def _render_service_interval_summary(
        self,
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        frame_columns: set[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, Any]:
        if not {"frame_slice", "thread", "process"} <= tables:
            return {"available": False}
        expressions = self._frame_select_expressions(
            frame_columns, alias="f"
        )
        rows = [
            dict(row)
            for row in connection.execute(
                f"SELECT {expressions}, t.tid AS os_tid, "
                "t.name AS thread_name, p.pid AS os_pid, "
                "p.name AS process_name "
                "FROM frame_slice AS f "
                "JOIN thread AS t ON t.itid=f.itid "
                "JOIN process AS p ON p.ipid=f.ipid "
                "WHERE LOWER(p.name) IN ('render_service','renderservice') "
                "AND f.dur IS NOT NULL AND f.dur>=0 "
                "AND f.ts+f.dur>=? AND f.ts+f.dur<? "
                "ORDER BY f.ts,f.id LIMIT ?",
                (start_ns, end_ns, self._MAX_FRAME_ROWS + 1),
            ).fetchall()
        ]
        truncated = len(rows) > self._MAX_FRAME_ROWS
        rows = rows[: self._MAX_FRAME_ROWS]
        actual = [row for row in rows if self._is_actual(row)]
        expected = [row for row in rows if self._is_expected(row)]
        actual_points = [int(row["ts"]) + int(row["dur"]) for row in actual]
        activity = self._activity_metric(actual_points)
        interval_ns = end_ns - start_ns
        actual_vsyncs = {
            row.get("vsync") for row in actual if row.get("vsync") is not None
        }
        expected_vsyncs = {
            row.get("vsync")
            for row in expected
            if row.get("vsync") is not None
        }
        durations = [int(row["dur"]) / 1_000_000.0 for row in actual]
        return {
            "available": bool(actual or expected),
            "scope": "global-render-service-output",
            "target_surface_attributed": False,
            "actual_frames": len(actual),
            "expected_frames": len(expected),
            "problem_interval_fps": (
                len(actual) * 1_000_000_000 / interval_ns
                if interval_ns > 0
                else None
            ),
            "active_fps": activity["fps"],
            "active_intervals": activity["intervals"],
            "dropped_frame_candidates": len(
                expected_vsyncs - actual_vsyncs
            ),
            "frame_duration_ms": self._distribution(durations),
            "threads": sorted(
                {
                    (
                        int(row["itid"]),
                        int(row["os_tid"]),
                        str(row.get("thread_name") or "unknown"),
                    )
                    for row in rows
                    if row.get("itid") is not None
                    and row.get("os_tid") is not None
                }
            ),
            "boundary_rule": (
                "count RenderService actual frames whose completion "
                "timestamp (ts+dur) is in [start_ns,end_ns)"
            ),
            "truncated": truncated,
        }

    @staticmethod
    def _gpu_durations(
        connection: sqlite3.Connection, *, tables: set[str]
    ) -> dict[int, int]:
        if "gpu_slice" not in tables:
            return {}
        columns = FrameJankRepository._columns(connection, "gpu_slice")
        if not {"frame_row", "dur"} <= columns:
            return {}
        rows = connection.execute(
            "SELECT frame_row, SUM(dur) AS dur FROM gpu_slice "
            "WHERE dur IS NOT NULL AND dur > 0 GROUP BY frame_row"
        ).fetchall()
        return {int(row["frame_row"]): int(row["dur"]) for row in rows}

    def _cadence_segments(
        self,
        *,
        expected_frames: list[dict[str, Any]],
        actual_frames: list[dict[str, Any]],
        start_ns: int,
        end_ns: int,
        requested_refresh_rate_hz: float | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if requested_refresh_rate_hz is not None:
            return [
                {
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "refresh_rate_hz": float(requested_refresh_rate_hz),
                    "frame_budget_ms": 1000.0 / requested_refresh_rate_hz,
                    "source": "analyze-request.refresh_rate_hz",
                    "sample_count": 0,
                    "confidence": 1.0,
                }
            ], []

        samples = [
            (int(row["ts"]), int(row["dur"]))
            for row in expected_frames
            if row.get("dur") is not None and int(row["dur"]) > 0
        ]
        source = "frame_slice.expected-duration"
        if len(samples) < 2:
            ordered = sorted(int(row["ts"]) for row in actual_frames)
            samples = [
                (ordered[index], ordered[index] - ordered[index - 1])
                for index in range(1, len(ordered))
                if 4_000_000
                <= ordered[index] - ordered[index - 1]
                <= 50_000_000
            ]
            source = "frame_slice.actual-cadence-inference"
        if not samples:
            return [], [
                "无法从请求参数或 Trace 帧事件证明刷新率；未默认使用 60Hz。"
            ]

        normalized = [
            (ts, *self._normalized_refresh(period_ns))
            for ts, period_ns in sorted(samples)
        ]
        groups: list[list[tuple[int, float, int]]] = []
        current: list[tuple[int, float, int]] = []
        for sample in normalized:
            if current and (
                sample[1] != current[-1][1]
                or sample[0] - current[-1][0] > self._ACTIVE_GAP_NS
            ):
                groups.append(current)
                current = []
            current.append(sample)
        if current:
            groups.append(current)

        segments = []
        for group in groups:
            rate = float(group[0][1])
            period_ns = int(round(median(item[2] for item in group)))
            segments.append(
                {
                    "start_ns": max(start_ns, group[0][0]),
                    "end_ns": min(
                        end_ns,
                        max(group[-1][0] + period_ns, group[0][0] + 1),
                    ),
                    "refresh_rate_hz": rate,
                    "frame_budget_ms": 1000.0 / rate,
                    "observed_period_ms": period_ns / 1_000_000.0,
                    "source": source,
                    "sample_count": len(group),
                    "confidence": 0.95 if source.endswith("duration") else 0.75,
                }
            )
        return segments, []

    @classmethod
    def _normalized_refresh(cls, period_ns: int) -> tuple[float, int]:
        raw_hz = 1_000_000_000 / period_ns
        closest = min(cls._COMMON_REFRESH_RATES, key=lambda hz: abs(hz - raw_hz))
        if abs(closest - raw_hz) / closest <= 0.08:
            return closest, period_ns
        return round(raw_hz, 3), period_ns

    @staticmethod
    def _dominant_refresh(
        segments: list[dict[str, Any]],
    ) -> float | None:
        if not segments:
            return None
        weights: Counter[float] = Counter()
        for item in segments:
            weights[float(item["refresh_rate_hz"])] += max(
                int(item.get("sample_count") or 0), 1
            )
        return weights.most_common(1)[0][0]

    @staticmethod
    def _budget_for_timestamp(
        timestamp_ns: int,
        *,
        cadence_segments: list[dict[str, Any]],
        dominant_budget_ns: int | None,
    ) -> int | None:
        for item in cadence_segments:
            if item["start_ns"] <= timestamp_ns < item["end_ns"]:
                return int(round(item["frame_budget_ms"] * 1_000_000))
        return dominant_budget_ns

    def _frame_facts(
        self,
        *,
        actual_frames: list[dict[str, Any]],
        expected_frames: list[dict[str, Any]],
        mappings: dict[int, list[dict[str, Any]]],
        render_expectations: dict[int, dict[str, Any]],
        gpu_by_frame: dict[int, int],
        cadence_segments: list[dict[str, Any]],
        dominant_budget_ns: int | None,
    ) -> list[dict[str, Any]]:
        expected_by_vsync = {
            row.get("vsync"): row
            for row in expected_frames
            if row.get("vsync") is not None
        }
        facts = []
        for app in sorted(actual_frames, key=lambda row: (row["ts"], row["id"])):
            app_start = int(app["ts"])
            app_duration = int(app["dur"])
            app_end = app_start + app_duration
            budget_ns = self._budget_for_timestamp(
                app_start,
                cadence_segments=cadence_segments,
                dominant_budget_ns=dominant_budget_ns,
            )
            expected = expected_by_vsync.get(app.get("vsync"))
            if expected is not None and int(expected.get("dur") or 0) > 0:
                budget_ns = int(expected["dur"])
            app_deadline = (
                int(expected["ts"]) + int(expected["dur"])
                if expected is not None and int(expected.get("dur") or 0) > 0
                else None
            )
            render_rows = mappings.get(int(app["id"]), [])
            render = max(
                render_rows,
                key=lambda row: int(row["ts"]) + int(row["dur"]),
                default=None,
            )
            render_expected = None
            if expected is not None and expected.get("dst") is not None:
                render_expected = render_expectations.get(int(expected["dst"]))
            render_end = (
                int(render["ts"]) + int(render["dur"])
                if render is not None
                else None
            )
            render_deadline = (
                int(render_expected["ts"]) + int(render_expected["dur"])
                if render_expected is not None
                and int(render_expected.get("dur") or 0) > 0
                else None
            )
            final_end = render_end if render_end is not None else app_end
            final_deadline = (
                render_deadline if render_deadline is not None else app_deadline
            )
            lateness_ns = (
                max(0, final_end - final_deadline)
                if final_deadline is not None
                else 0
            )
            missed = (
                int(math.ceil(lateness_ns / budget_ns))
                if lateness_ns > 0 and budget_ns
                else (
                    max(0, int(math.ceil(app_duration / budget_ns)) - 1)
                    if budget_ns
                    else 0
                )
            )
            app_late = app_deadline is not None and app_end > app_deadline
            render_over_budget = (
                render is not None
                and render_expected is not None
                and int(render["dur"]) > int(render_expected["dur"])
            )
            if app_late and render_over_budget:
                stage = "mixed-application-and-render-service"
            elif app_late:
                stage = "application-production"
            elif render_over_budget or (
                render_deadline is not None
                and render_end is not None
                and render_end > render_deadline
            ):
                stage = "render-service-presentation"
            elif missed > 0:
                stage = "application-frame-duration-fallback"
            else:
                stage = "on-time-or-unproven"
            flagged = app.get("flag") == 1 or (
                render is not None and render.get("flag") == 1
            )
            is_jank = missed > 0 or bool(flagged)
            gpu_duration_ns = (
                gpu_by_frame.get(int(render["id"])) if render is not None else None
            )
            facts.append(
                {
                    "application_frame_id": int(app["id"]),
                    "application_source_id": f"frame_slice:{app['id']}",
                    "vsync": app.get("vsync"),
                    "application_itid": int(app["itid"]),
                    "application_tid": (
                        int(app["os_tid"])
                        if app.get("os_tid") is not None
                        else None
                    ),
                    "application_thread": str(app.get("thread_name") or "unknown"),
                    "application_start_ns": app_start,
                    "application_end_ns": app_end,
                    "application_duration_ms": app_duration / 1_000_000.0,
                    "application_deadline_ns": app_deadline,
                    "render_frame_id": int(render["id"]) if render is not None else None,
                    "render_source_id": (
                        f"frame_slice:{render['id']}" if render is not None else None
                    ),
                    "render_itid": (
                        int(render["itid"])
                        if render is not None and render.get("itid") is not None
                        else None
                    ),
                    "render_tid": (
                        int(render["render_tid"])
                        if render is not None and render.get("render_tid") is not None
                        else None
                    ),
                    "render_thread": (
                        str(render.get("render_thread_name") or "unknown")
                        if render is not None
                        else None
                    ),
                    "render_process": (
                        str(render.get("render_process_name") or "unknown")
                        if render is not None
                        else None
                    ),
                    "render_start_ns": int(render["ts"]) if render is not None else None,
                    "render_end_ns": render_end,
                    "render_duration_ms": (
                        int(render["dur"]) / 1_000_000.0
                        if render is not None
                        else None
                    ),
                    "presentation_deadline_ns": final_deadline,
                    "presentation_end_ns": render_end,
                    "presentation_lateness_ms": lateness_ns / 1_000_000.0,
                    "pipeline_duration_ms": (final_end - app_start) / 1_000_000.0,
                    "frame_budget_ms": (
                        budget_ns / 1_000_000.0 if budget_ns else None
                    ),
                    "missed_vsyncs": missed,
                    "is_slow_application_frame": bool(
                        budget_ns is not None and app_duration > budget_ns
                    ),
                    "is_jank": is_jank,
                    "jank_evidence": (
                        "deadline-and-cadence"
                        if final_deadline is not None and budget_ns is not None
                        else "trace-flag-or-duration-fallback"
                    ),
                    "observable_delay_stage": stage,
                    "gpu_duration_ms": (
                        gpu_duration_ns / 1_000_000.0
                        if gpu_duration_ns is not None
                        else None
                    ),
                    "analysis_start_ns": min(
                        int(expected["ts"]) if expected is not None else app_start,
                        app_start,
                    ),
                    "analysis_end_ns": final_end,
                }
            )
        return facts

    def _frame_metrics(
        self,
        facts: list[dict[str, Any]],
        *,
        expected_frames: list[dict[str, Any]],
        interval_start_ns: int,
        interval_end_ns: int,
        dominant_budget_ns: int | None,
        render_service_interval: dict[str, Any],
    ) -> dict[str, Any]:
        app_points = [int(item["application_end_ns"]) for item in facts]
        presentation_points = [
            int(item["presentation_end_ns"])
            for item in facts
            if item["presentation_end_ns"] is not None
        ]
        app_activity = self._activity_metric(app_points)
        presentation_activity = self._activity_metric(presentation_points)
        interval_ns = interval_end_ns - interval_start_ns
        coverage = (
            presentation_activity["active_duration_ns"] / interval_ns
            if interval_ns > 0
            else 0.0
        )
        interval_fps_meaningful = (
            presentation_activity["frame_count"] >= 3 and coverage >= 0.8
        )
        actual_vsyncs = {
            item["vsync"] for item in facts if item.get("vsync") is not None
        }
        expected_vsyncs = {
            row.get("vsync")
            for row in expected_frames
            if row.get("vsync") is not None
        }
        dropped = sorted(expected_vsyncs - actual_vsyncs)
        durations = [float(item["application_duration_ms"]) for item in facts]
        pipeline = [float(item["pipeline_duration_ms"]) for item in facts]
        jank_count = sum(bool(item["is_jank"]) for item in facts)
        if presentation_points:
            problem_interval_frame_count = len(presentation_points)
            problem_interval_fps = (
                problem_interval_frame_count * 1_000_000_000 / interval_ns
                if interval_ns > 0
                else None
            )
            problem_interval_fps_source = "target-mapped-render-service"
            problem_interval_target_attributed = True
        elif render_service_interval.get("available"):
            problem_interval_frame_count = int(
                render_service_interval.get("actual_frames") or 0
            )
            problem_interval_fps = render_service_interval.get(
                "problem_interval_fps"
            )
            problem_interval_fps_source = "global-render-service-output"
            problem_interval_target_attributed = False
        else:
            problem_interval_frame_count = len(facts)
            problem_interval_fps = (
                problem_interval_frame_count * 1_000_000_000 / interval_ns
                if interval_ns > 0
                else None
            )
            problem_interval_fps_source = "application-frame-owner-fallback"
            problem_interval_target_attributed = False
        return {
            "application_actual_frames": len(facts),
            "mapped_presented_frames": len(presentation_points),
            "expected_frame_slots": len(expected_vsyncs),
            "dropped_frame_candidates": len(dropped),
            "dropped_vsync_candidates": dropped[:100],
            "drop_semantics": (
                "expected application vsync without an actual frame in the selected "
                "producer pipeline; candidate only, not a presentation-drop proof"
            ),
            "slow_application_frames": sum(
                bool(item["is_slow_application_frame"]) for item in facts
            ),
            "jank_frames": jank_count,
            "jank_rate": jank_count / len(facts) if facts else None,
            "total_missed_vsyncs": sum(int(item["missed_vsyncs"]) for item in facts),
            "maximum_missed_vsyncs": max(
                (int(item["missed_vsyncs"]) for item in facts), default=0
            ),
            "application_production_fps": app_activity["fps"],
            "effective_presentation_fps": presentation_activity["fps"],
            "effective_fps_meaningful": presentation_activity["meaningful"],
            "active_render_intervals": presentation_activity["intervals"],
            "interval_average_presentation_fps": (
                len(presentation_points) * 1_000_000_000 / interval_ns
                if interval_fps_meaningful and interval_ns > 0
                else None
            ),
            "interval_average_fps_meaningful": interval_fps_meaningful,
            "interval_active_coverage": coverage,
            "problem_interval_fps": problem_interval_fps,
            "problem_interval_frame_count": problem_interval_frame_count,
            "problem_interval_fps_source": problem_interval_fps_source,
            "problem_interval_target_attributed": (
                problem_interval_target_attributed
            ),
            "render_service_interval": render_service_interval,
            "fps_semantics": (
                "effective FPS uses (frames - one per active cluster) / summed "
                "first-to-last presentation time; whole-interval FPS is withheld "
                "unless mapped presentation activity covers at least 80%; "
                "problem_interval_fps counts completed frames across the complete "
                "user-selected interval, preferring target-mapped RenderService, "
                "then global RenderService output, then application frame owner"
            ),
            "frame_budget_ms": (
                dominant_budget_ns / 1_000_000.0
                if dominant_budget_ns is not None
                else None
            ),
            "application_frame_duration_ms": self._distribution(durations),
            "pipeline_duration_ms": self._distribution(pipeline),
        }

    def _activity_metric(self, points: Iterable[int]) -> dict[str, Any]:
        ordered = sorted(set(points))
        clusters: list[list[int]] = []
        current: list[int] = []
        for point in ordered:
            if current and point - current[-1] > self._ACTIVE_GAP_NS:
                clusters.append(current)
                current = []
            current.append(point)
        if current:
            clusters.append(current)
        intervals = []
        active_duration_ns = 0
        rate_numerator = 0
        for cluster in clusters:
            span = cluster[-1] - cluster[0] if len(cluster) >= 2 else 0
            active_duration_ns += span
            rate_numerator += max(len(cluster) - 1, 0)
            intervals.append(
                {
                    "start_ns": cluster[0],
                    "end_ns": cluster[-1],
                    "duration_ms": span / 1_000_000.0,
                    "frame_count": len(cluster),
                    "fps": (
                        (len(cluster) - 1) * 1_000_000_000 / span
                        if span > 0 and len(cluster) >= 2
                        else None
                    ),
                }
            )
        meaningful = rate_numerator >= 2 and active_duration_ns > 0
        return {
            "frame_count": len(ordered),
            "active_duration_ns": active_duration_ns,
            "fps": (
                rate_numerator * 1_000_000_000 / active_duration_ns
                if meaningful
                else None
            ),
            "meaningful": meaningful,
            "intervals": intervals,
        }

    @staticmethod
    def _distribution(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {
                "count": 0,
                "p50": None,
                "p90": None,
                "p95": None,
                "p99": None,
                "maximum": None,
            }
        ordered = sorted(values)

        def percentile(fraction: float) -> float:
            position = (len(ordered) - 1) * fraction
            lower = math.floor(position)
            upper = math.ceil(position)
            if lower == upper:
                return ordered[lower]
            weight = position - lower
            return ordered[lower] * (1 - weight) + ordered[upper] * weight

        return {
            "count": len(ordered),
            "p50": percentile(0.50),
            "p90": percentile(0.90),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "maximum": ordered[-1],
        }

    @staticmethod
    def _bad_frame_clusters(
        bad_frames: list[dict[str, Any]],
        *,
        dominant_budget_ns: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        ordered = sorted(bad_frames, key=lambda item: item["application_start_ns"])
        gap_ns = max((dominant_budget_ns or 16_666_667) * 3, 50_000_000)
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for item in ordered:
            if current and (
                item["application_start_ns"] - current[-1]["analysis_end_ns"]
                > gap_ns
            ):
                groups.append(current)
                current = []
            current.append(item)
        if current:
            groups.append(current)
        clusters = [
            {
                "start_ns": min(item["analysis_start_ns"] for item in group),
                "end_ns": max(item["analysis_end_ns"] for item in group),
                "duration_ms": (
                    max(item["analysis_end_ns"] for item in group)
                    - min(item["analysis_start_ns"] for item in group)
                )
                / 1_000_000.0,
                "bad_frame_count": len(group),
                "total_missed_vsyncs": sum(item["missed_vsyncs"] for item in group),
                "maximum_missed_vsyncs": max(item["missed_vsyncs"] for item in group),
                "dominant_delay_stage": Counter(
                    item["observable_delay_stage"] for item in group
                ).most_common(1)[0][0],
                "application_frame_ids": [
                    item["application_frame_id"] for item in group
                ],
            }
            for group in groups
        ]
        clusters.sort(
            key=lambda item: (
                -item["total_missed_vsyncs"],
                -item["bad_frame_count"],
                item["start_ns"],
            )
        )
        return clusters[:limit]

    def _render_architecture(
        self,
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        process: dict[str, Any],
        target_ipid: int,
        start_ns: int,
        end_ns: int,
        producer_candidates: list[dict[str, Any]],
        selected_itid: int,
        mappings: dict[int, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        thread_rows: list[dict[str, Any]] = []
        if "thread" in tables:
            rows = connection.execute(
                "SELECT t.itid, t.tid, t.name, p.pid, p.name AS process_name "
                "FROM thread AS t LEFT JOIN process AS p ON p.ipid = t.ipid "
                "WHERE t.ipid = ? ORDER BY t.itid",
                (target_ipid,),
            ).fetchall()
            thread_rows = [dict(row) for row in rows]
        render_threads: dict[int, dict[str, Any]] = {}
        for rows in mappings.values():
            for row in rows:
                if row.get("itid") is None:
                    continue
                render_threads[int(row["itid"])] = {
                    "itid": int(row["itid"]),
                    "tid": (
                        int(row["render_tid"])
                        if row.get("render_tid") is not None
                        else None
                    ),
                    "thread_name": str(row.get("render_thread_name") or "unknown"),
                    "process_name": str(row.get("render_process_name") or "unknown"),
                }
        application_callstack_names = self._callstack_names(
            connection,
            tables=tables,
            target_ipid=target_ipid,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        runtime_signals = self._thread_runtime_signals(
            connection,
            tables=tables,
            thread_rows=thread_rows,
            target_ipid=target_ipid,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        render_callstack_names = self._callstack_names_for_itids(
            connection,
            tables=tables,
            itids=set(render_threads),
            start_ns=start_ns,
            end_ns=end_ns,
        )
        application_corpus = "\n".join(
            [
                *(str(row.get("name") or "") for row in thread_rows),
                *application_callstack_names,
                *(
                    name
                    for item in runtime_signals.values()
                    for name in item.get("render_signal_names", [])
                ),
            ]
        ).lower()
        render_corpus = "\n".join(
            [
                *(item["thread_name"] for item in render_threads.values()),
                *render_callstack_names,
            ]
        ).lower()
        framework, framework_matches = self._infer_framework(
            application_corpus
        )
        roles_by_itid: dict[int, dict[str, Any]] = {}

        def add_role(
            *,
            itid: int,
            tid: int | None,
            thread_name: str,
            process_name: str,
            role: str,
            source: str,
            confidence: float,
        ) -> None:
            item = roles_by_itid.setdefault(
                itid,
                {
                    "itid": itid,
                    "tid": tid,
                    "thread_name": thread_name,
                    "process_name": process_name,
                    "roles": [],
                    "evidence": [],
                    "confidence": 0.0,
                },
            )
            if role not in item["roles"]:
                item["roles"].append(role)
            if source not in item["evidence"]:
                item["evidence"].append(source)
            item["confidence"] = max(item["confidence"], confidence)

        candidate_by_itid = {item["itid"]: item for item in producer_candidates}
        for itid, candidate in candidate_by_itid.items():
            add_role(
                itid=itid,
                tid=candidate["tid"],
                thread_name=candidate["thread_name"],
                process_name=str(process.get("name") or "unknown"),
                role=(
                    "selected-application-frame-producer"
                    if itid == selected_itid
                    else "application-frame-producer-candidate"
                ),
                source="frame_slice actual-frame ownership",
                confidence=0.98 if itid == selected_itid else 0.85,
            )
        for row in thread_rows:
            itid = int(row["itid"])
            tid = int(row["tid"]) if row.get("tid") is not None else None
            name = str(row.get("name") or "unknown")
            lower = name.lower()
            if process.get("pid") is not None and tid == process["pid"]:
                add_role(
                    itid=itid,
                    tid=tid,
                    thread_name=name,
                    process_name=str(process.get("name") or "unknown"),
                    role="application-main-thread",
                    source="OS tid equals process pid; not treated as UI proof",
                    confidence=0.95,
                )
            inferred_role = self._thread_name_role(lower)
            if inferred_role is not None:
                add_role(
                    itid=itid,
                    tid=tid,
                    thread_name=name,
                    process_name=str(process.get("name") or "unknown"),
                    role=inferred_role,
                    source="framework/thread-name hint; requires frame or stack corroboration",
                    confidence=(
                        0.85
                        if any(
                            token in lower
                            for token in ("useragent", "qtmainthread")
                        )
                        else 0.55
                    ),
                )
            signal = runtime_signals.get(itid, {})
            if int(signal.get("render_signal_count") or 0) > 0:
                add_role(
                    itid=itid,
                    tid=tid,
                    thread_name=name,
                    process_name=str(process.get("name") or "unknown"),
                    role="surface-submit-or-ui-paint-candidate",
                    source=(
                        "Trace Slice contains UI paint or buffer-submit "
                        "operations"
                    ),
                    confidence=0.92,
                )
        for itid, item in render_threads.items():
            add_role(
                itid=itid,
                tid=item["tid"],
                thread_name=item["thread_name"],
                process_name=item["process_name"],
                role="target-frame-mapped-render-service",
                source="frame_maps app-to-RenderService association",
                confidence=0.99,
            )

        unifying_tokens = ("unirender", "rsunirender", "unified render")
        unified_matches = [
            token for token in unifying_tokens if token in render_corpus
        ]
        unified_proven = bool(unified_matches and render_threads)
        mapped_count = sum(bool(rows) for rows in mappings.values())
        for itid, item in roles_by_itid.items():
            signal = runtime_signals.get(itid, {})
            item.update(
                {
                    "running_ms": float(signal.get("running_ms") or 0.0),
                    "running_share": float(
                        signal.get("running_share") or 0.0
                    ),
                    "perf_samples": int(signal.get("perf_samples") or 0),
                    "perf_sample_share": float(
                        signal.get("perf_sample_share") or 0.0
                    ),
                    "render_signal_count": int(
                        signal.get("render_signal_count") or 0
                    ),
                    "render_signal_names": list(
                        signal.get("render_signal_names") or []
                    ),
                }
            )
            if item["running_share"] >= 0.05:
                item["evidence"].append(
                    "Running time is at least 5% of the problem window"
                )
            if item["perf_sample_share"] >= 0.05:
                item["evidence"].append(
                    "Perf samples are at least 5% of the target process"
                )

        required_itids = {
            itid
            for itid, item in roles_by_itid.items()
            if (
                "selected-application-frame-producer" in item["roles"]
                or "target-frame-mapped-render-service" in item["roles"]
            )
        }
        ui_candidates = sorted(
            (
                item
                for item in roles_by_itid.values()
                if any(
                    role in item["roles"]
                    for role in (
                        "framework-ui-or-js-candidate",
                        "framework-ui-event-loop-candidate",
                        "surface-submit-or-ui-paint-candidate",
                    )
                )
            ),
            key=lambda item: (
                -int(item["render_signal_count"] > 0),
                -max(item["perf_sample_share"], item["running_share"]),
                -item["perf_samples"],
                item["itid"],
            ),
        )
        selected_itids = set(required_itids)
        selected_itids.update(
            item["itid"]
            for item in ui_candidates[:2]
            if (
                item["render_signal_count"] > 0
                or item["running_share"] >= 0.005
                or item["perf_sample_share"] >= 0.005
            )
        )
        strongest_ui_candidate = next(
            (
                item
                for item in ui_candidates
                if item["itid"] != selected_itid
                and item["render_signal_count"] > 0
                and max(
                    item["running_share"], item["perf_sample_share"]
                )
                >= 0.05
            ),
            None,
        )
        frame_producer_ui_mismatch = {
            "status": (
                "strong-ui-candidate-differs-from-frame-owner"
                if strongest_ui_candidate is not None
                else "not-proven"
            ),
            "frame_owner_itid": selected_itid,
            "ui_candidate_itid": (
                strongest_ui_candidate["itid"]
                if strongest_ui_candidate is not None
                else None
            ),
            "ui_candidate_tid": (
                strongest_ui_candidate["tid"]
                if strongest_ui_candidate is not None
                else None
            ),
            "ui_candidate_thread_name": (
                strongest_ui_candidate["thread_name"]
                if strongest_ui_candidate is not None
                else None
            ),
            "interpretation": (
                "frame_slice ownership may describe a platform or wrapper "
                "pipeline rather than the framework UI surface; do not treat "
                "its frame count as definitive target-UI FPS"
                if strongest_ui_candidate is not None
                else "no independently corroborated UI/frame-owner mismatch"
            ),
        }
        perf_tids = sorted(
            {
                int(roles_by_itid[itid]["tid"])
                for itid in selected_itids
                if roles_by_itid[itid].get("tid") is not None
            }
        )
        return {
            "framework_family": framework,
            "framework_confidence": (
                min(0.45 + len(framework_matches) * 0.15, 0.9)
                if framework != "unknown"
                else 0.0
            ),
            "framework_evidence_tokens": framework_matches,
            "thread_roles": sorted(
                roles_by_itid.values(),
                key=lambda item: (
                    "selected-application-frame-producer" not in item["roles"],
                    "target-frame-mapped-render-service" not in item["roles"],
                    item["itid"],
                ),
            ),
            "ui_thread_equals_main_thread_assumed": False,
            "unified_rendering": {
                "status": (
                    "proven"
                    if unified_proven
                    else (
                        "render-service-mapped-but-unified-mode-unproven"
                        if render_threads
                        else "unavailable"
                    )
                ),
                "mapped_application_frames": mapped_count,
                "mapped_render_threads": sorted(render_threads.values(), key=lambda x: x["itid"]),
                "evidence_tokens": unified_matches,
                "attribution_rule": (
                    "only frame_maps-linked RenderService work belongs to the target "
                    "pipeline; whole-process RenderService load is excluded"
                ),
            },
            "surface_identity_available": False,
            "frame_producer_ui_mismatch": frame_producer_ui_mismatch,
            "recommended_perf_thread_ids": perf_tids,
        }

    @staticmethod
    def _thread_runtime_signals(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        thread_rows: list[dict[str, Any]],
        target_ipid: int,
        start_ns: int,
        end_ns: int,
    ) -> dict[int, dict[str, Any]]:
        duration_ms = max(0.0, (end_ns - start_ns) / 1_000_000.0)
        signals: dict[int, dict[str, Any]] = {
            int(row["itid"]): {
                "running_ms": 0.0,
                "running_share": 0.0,
                "perf_samples": 0,
                "perf_sample_share": 0.0,
                "render_signal_count": 0,
                "render_signal_names": [],
            }
            for row in thread_rows
        }
        if "sched_slice" in tables:
            rows = connection.execute(
                "SELECT s.itid, SUM(MIN(s.ts+s.dur, ?) - MAX(s.ts, ?)) "
                "AS running_ns FROM sched_slice AS s JOIN thread AS t "
                "ON t.itid=s.itid WHERE t.ipid=? AND s.dur>0 "
                "AND s.ts<? AND s.ts+s.dur>? GROUP BY s.itid",
                (end_ns, start_ns, target_ipid, end_ns, start_ns),
            ).fetchall()
            for row in rows:
                itid = int(row["itid"])
                if itid not in signals:
                    continue
                running_ms = float(row["running_ns"] or 0) / 1_000_000.0
                signals[itid]["running_ms"] = running_ms
                signals[itid]["running_share"] = (
                    running_ms / duration_ms if duration_ms > 0 else 0.0
                )
        if "callstack" in tables:
            rows = connection.execute(
                "SELECT c.callid AS itid, c.name, COUNT(*) AS occurrences "
                "FROM callstack AS c JOIN thread AS t ON t.itid=c.callid "
                "WHERE t.ipid=? AND c.dur>0 AND c.ts<? AND c.ts+c.dur>? "
                "AND (LOWER(c.name) LIKE '%nativewindowflushbuffer%' "
                "OR LOWER(c.name) LIKE '%qbackingstore%' "
                "OR LOWER(c.name) LIKE '%qrasterpaintengine%' "
                "OR LOWER(c.name) LIKE '%drawwidget%' "
                "OR LOWER(c.name) LIKE '%eglswapbuffers%' "
                "OR LOWER(c.name) LIKE '%swapbuffer%') "
                "GROUP BY c.callid,c.name ORDER BY occurrences DESC "
                "LIMIT 2000",
                (target_ipid, end_ns, start_ns),
            ).fetchall()
            for row in rows:
                itid = int(row["itid"])
                if itid not in signals:
                    continue
                signals[itid]["render_signal_count"] += int(
                    row["occurrences"] or 0
                )
                if len(signals[itid]["render_signal_names"]) < 8:
                    signals[itid]["render_signal_names"].append(
                        str(row["name"])
                    )
        if {"perf_sample", "perf_thread"} <= tables:
            sample_columns = FrameJankRepository._columns(
                connection, "perf_sample"
            )
            if {"thread_id", "timestamp_trace"} <= sample_columns:
                rows = connection.execute(
                    "SELECT ps.thread_id, COUNT(*) AS samples "
                    "FROM perf_sample AS ps JOIN perf_thread AS pt "
                    "ON pt.thread_id=ps.thread_id "
                    "WHERE pt.process_id=? AND ps.timestamp_trace>=? "
                    "AND ps.timestamp_trace<? GROUP BY ps.thread_id",
                    (
                        next(
                            (
                                int(row["pid"])
                                for row in thread_rows
                                if row.get("pid") is not None
                            ),
                            -1,
                        ),
                        start_ns,
                        end_ns,
                    ),
                ).fetchall()
                total_samples = sum(int(row["samples"] or 0) for row in rows)
                itid_by_tid = {
                    int(row["tid"]): int(row["itid"])
                    for row in thread_rows
                    if row.get("tid") is not None
                }
                for row in rows:
                    itid = itid_by_tid.get(int(row["thread_id"]))
                    if itid not in signals:
                        continue
                    samples = int(row["samples"] or 0)
                    signals[itid]["perf_samples"] = samples
                    signals[itid]["perf_sample_share"] = (
                        samples / total_samples if total_samples else 0.0
                    )
        return signals

    @staticmethod
    def _callstack_names(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        target_ipid: int,
        start_ns: int,
        end_ns: int,
    ) -> list[str]:
        if not {"callstack", "thread"} <= tables:
            return []
        rows = connection.execute(
            "SELECT DISTINCT c.name FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "WHERE c.ts < ? AND c.ts + MAX(c.dur, 0) > ? "
            "AND t.ipid = ? LIMIT 1000",
            (end_ns, start_ns, target_ipid),
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]

    @staticmethod
    def _callstack_names_for_itids(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        itids: set[int],
        start_ns: int,
        end_ns: int,
    ) -> list[str]:
        if not {"callstack", "thread"} <= tables or not itids:
            return []
        values = sorted(itids)
        placeholders = ",".join("?" for _ in values)
        rows = connection.execute(
            "SELECT DISTINCT c.name FROM callstack AS c "
            "JOIN thread AS t ON t.itid = c.callid "
            "WHERE c.ts < ? AND c.ts + MAX(c.dur, 0) > ? "
            f"AND t.itid IN ({placeholders}) LIMIT 1000",
            (end_ns, start_ns, *values),
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]

    @staticmethod
    def _infer_framework(corpus: str) -> tuple[str, list[str]]:
        tokens = {
            "flutter": ("flutter", "dart ui", "dart_worker", "rasterizer"),
            "react-native": ("reactnative", "react native", "hermes", "fabricui"),
            "qt": (
                "qtquick",
                "qtmainthread",
                "qsg",
                "qeventloop",
                "qbackingstore",
                "qrasterpaintengine",
                "scene graph",
            ),
            "compose-multiplatform": ("compose", "skiko", "kotlinx.coroutines"),
            "arkui": ("arkui", "aceengine", "jsuiengine", "declarativefrontend"),
        }
        matches = {
            framework: [token for token in values if token in corpus]
            for framework, values in tokens.items()
        }
        framework, found = max(matches.items(), key=lambda item: len(item[1]))
        return (framework, found) if found else ("unknown", [])

    @staticmethod
    def _thread_name_role(name: str) -> str | None:
        if any(
            token in name
            for token in (
                "useragent",
                "qtmainthread",
                "flutterui",
                "flutter ui",
                "dart ui",
                "js thread",
                "js_thread",
            )
        ):
            return "framework-ui-event-loop-candidate"
        if any(token in name for token in ("raster", "qsg", "skia", "render")):
            return "framework-render-or-raster-candidate"
        if any(token in name for token in ("dart", "hermes", "js thread", "js_thread")):
            return "framework-ui-or-js-candidate"
        if "io" in name and len(name) <= 64:
            return "framework-io-candidate"
        return None

    @staticmethod
    def _hidump_samples(
        connection: sqlite3.Connection,
        *,
        tables: set[str],
        start_ns: int,
        end_ns: int,
    ) -> dict[str, Any]:
        if "hidump" not in tables:
            return {"available": False, "sample_count": 0}
        columns = FrameJankRepository._columns(connection, "hidump")
        if not {"ts", "fps"} <= columns:
            return {"available": False, "sample_count": 0}
        rows = connection.execute(
            "SELECT ts, fps FROM hidump WHERE ts >= ? AND ts < ? "
            "ORDER BY ts LIMIT 1000",
            (start_ns, end_ns),
        ).fetchall()
        values = [float(row["fps"]) for row in rows if row["fps"] is not None]
        return {
            "available": bool(values),
            "sample_count": len(values),
            "average_fps": sum(values) / len(values) if values else None,
            "minimum_fps": min(values) if values else None,
            "maximum_fps": max(values) if values else None,
            "usage": (
                "supporting sampled display metric only; not used as per-frame "
                "root-cause evidence or refresh-budget proof"
            ),
        }
