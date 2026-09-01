from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import sqlite3
from typing import Any

from trace_agent.database import (
    CausalThreadDependencyRepository,
    FrameJankRepository,
    PerfAnalysisRepository,
    PerfProjectionError,
    PerfTraceRepository,
    SQLiteTraceRepository,
    StartupModuleRepository,
    ThreadExecutionRepository,
    TraceQueryError,
)
from trace_agent.evidence import EvidenceIndex
from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
    EvidenceRecord,
    utc_now,
)
from trace_agent.scenarios import scenario_definition


class ReportProjectionBuilder:
    """Build deterministic presentation data from Analysis and Trace facts."""

    _CPU_PALETTE = (
        "#227c9d",
        "#17a398",
        "#4c956c",
        "#7a9e35",
        "#c98b2e",
        "#d46f4c",
        "#b85c86",
        "#8064a2",
        "#3973b8",
        "#398c83",
        "#678d58",
        "#9a7b34",
    )

    def build(
        self,
        *,
        request: AnalyzeRequest,
        analysis: AnalysisResult,
        evidence: list[EvidenceRecord],
        database_path: Path | None,
    ) -> dict[str, Any]:
        cold = analysis.cold_start
        cold_view: dict[str, Any] | None = None
        if cold is not None:
            dominant_stage = max(
                cold.stages,
                key=lambda stage: stage.duration_ms,
                default=None,
            )
            critical_thread = (
                dominant_stage.critical_threads[0]
                if dominant_stage
                and dominant_stage.critical_threads
                else None
            )
            state_total_ms = 0.0
            running_share: float | None = None
            if critical_thread is not None:
                states = critical_thread.state_breakdown
                state_total_ms = (
                    states.running_ms
                    + states.runnable_ms
                    + states.sleeping_ms
                    + states.uninterruptible_io_ms
                    + states.uninterruptible_other_ms
                    + states.other_ms
                )
                if state_total_ms > 0:
                    running_share = states.running_ms / state_total_ms
            top_cpu = (
                max(
                    critical_thread.cpu_distribution,
                    key=lambda item: item.share,
                    default=None,
                )
                if critical_thread is not None
                else None
            )
            evidence_ids = set(cold.evidence_ids)
            for finding in analysis.findings:
                evidence_ids.update(finding.evidence_ids)
            problem_interval = analysis.problem_interval
            application_defined = (
                cold.start_boundary.kind
                in {
                    "application-defined-start",
                    "application-marker-start",
                }
                or cold.end_boundary.kind
                in {
                    "application-defined-completion",
                    "application-marker-end",
                    "stable-home-frame",
                }
            )
            cold_view = {
                "dominant_stage": dominant_stage,
                "critical_thread": critical_thread,
                "top_cpu": top_cpu,
                "running_share": running_share,
                "state_total_ms": state_total_ms,
                "boundary_confidence": (
                    cold.start_boundary.confidence
                    + cold.end_boundary.confidence
                )
                / 2,
                "confidence_label": self._confidence_label(
                    (
                        cold.start_boundary.confidence
                        + cold.end_boundary.confidence
                    )
                    / 2
                ),
                "evidence_count": len(evidence_ids),
                "metric_label": (
                    problem_interval.metric_definition
                    if problem_interval is not None
                    else cold.end_boundary.name
                    if application_defined
                    else "应用首帧完成"
                ),
                "metric_note": (
                    f"{cold.start_boundary.name} → "
                    f"{cold.end_boundary.name}"
                ),
                "end_boundary_label": (
                    cold.end_boundary.name
                    if application_defined
                    else "应用首帧"
                ),
                "presentation_duration_ms": (
                    cold.presentation_duration_ms
                    if cold.presentation_boundary is not None
                    else None
                ),
                "stages": [
                    {
                        "name": stage.name,
                        "duration_ms": stage.duration_ms,
                        "share": (
                            stage.duration_ms / cold.total_duration_ms
                            if cold.total_duration_ms > 0
                            else 0
                        ),
                        "critical": stage is dominant_stage,
                        "assessment": stage.assessment,
                        "evidence_ids": stage.evidence_ids,
                    }
                    for stage in cold.stages
                ],
            }

        evidence_index = EvidenceIndex(evidence)
        return {
            "title": scenario_definition(
                request.scenario_type
            ).report_title,
            "scenario_code": request.scenario_type.value.upper(),
            "trace_name": request.trace_path.name,
            "generated_at": utc_now().astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
            "cold": cold_view,
            "startup_modules": self._build_startup_module_view(
                analysis=analysis,
                database_path=database_path,
            ),
            "frame": self._build_frame_jank_view(
                evidence_index=evidence_index,
                database_path=database_path,
            ),
            "perf": self._build_perf_view(
                analysis=analysis,
                database_path=database_path,
                evidence_index=evidence_index,
            ),
            "timeline": self._build_timeline(
                analysis=analysis,
                evidence_index=evidence_index,
                database_path=database_path,
            ),
        }

    @staticmethod
    def _build_frame_jank_view(
        *,
        evidence_index: EvidenceIndex,
        database_path: Path | None,
    ) -> dict[str, Any]:
        records = [
            record
            for record in evidence_index.records
            if record.tool == "inspect_frame_jank"
        ]
        if not records:
            return {"available": False, "reason": "没有帧率分析 Evidence。"}
        record = next(
            (
                candidate
                for candidate in reversed(records)
                if candidate.data.get("available")
            ),
            records[-1],
        )
        data = record.data
        interval = data.get("interval") or {}
        process = data.get("target_process") or {}
        if database_path is not None:
            try:
                data = FrameJankRepository(database_path).inspect(
                    target_ipid=int(process["ipid"]),
                    interval_start_ns=int(interval["start_ns"]),
                    interval_end_ns=int(interval["end_ns"]),
                    refresh_rate_hz=(
                        (data.get("cadence") or {}).get(
                            "dominant_refresh_rate_hz"
                        )
                    ),
                    frame_producer_itid=None,
                    max_bad_frames=30,
                    max_clusters=20,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                FileNotFoundError,
                sqlite3.DatabaseError,
            ):
                data = record.data
        if not data.get("available"):
            return {
                "available": False,
                "reason": "；".join(data.get("limitations") or [])
                or "帧率数据不可用。",
            }

        cadence = data.get("cadence") or {}
        metrics = data.get("metrics") or {}
        architecture = data.get("render_architecture") or {}
        mismatch = architecture.get("frame_producer_ui_mismatch") or {}
        mismatch_proven = (
            mismatch.get("status")
            == "strong-ui-candidate-differs-from-frame-owner"
        )
        surface_available = bool(
            architecture.get("surface_identity_available")
        )
        presentation_fps = metrics.get("effective_presentation_fps")
        application_fps = metrics.get("application_production_fps")
        interval_data = data.get("interval") or {}
        duration_ms = float(interval_data.get("duration_ms") or 0.0)
        actual_frames = int(metrics.get("application_actual_frames") or 0)
        problem_interval_frame_count = int(
            metrics.get("problem_interval_frame_count") or actual_frames
        )
        problem_interval_fps = metrics.get("problem_interval_fps")
        if problem_interval_fps is None and duration_ms > 0:
            problem_interval_fps = (
                problem_interval_frame_count * 1000.0 / duration_ms
            )
        problem_interval_source = str(
            metrics.get("problem_interval_fps_source")
            or "application-frame-owner-fallback"
        )
        render_service_interval = (
            metrics.get("render_service_interval") or {}
        )
        duration_stats = metrics.get("application_frame_duration_ms") or {}
        segments: list[dict[str, Any]] = []
        for index, segment in enumerate(cadence.get("segments") or []):
            start_ns = segment.get("start_ns")
            end_ns = segment.get("end_ns")
            segment_duration_ms = (
                (end_ns - start_ns) / 1_000_000.0
                if isinstance(start_ns, int)
                and isinstance(end_ns, int)
                and end_ns > start_ns
                else None
            )
            segments.append(
                {
                    "index": index + 1,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "duration_ms": segment_duration_ms,
                    "refresh_rate_hz": segment.get("refresh_rate_hz"),
                    "frame_budget_ms": segment.get("frame_budget_ms"),
                    "frame_count": int(segment.get("sample_count") or 0),
                    "source": segment.get("source"),
                    "confidence": segment.get("confidence"),
                }
            )

        return {
            "available": True,
            "source_evidence_id": record.evidence_id,
            "refresh_rate_hz": cadence.get("dominant_refresh_rate_hz"),
            "frame_budget_ms": cadence.get("dominant_frame_budget_ms"),
            "dynamic_refresh_detected": bool(
                cadence.get("dynamic_refresh_detected")
            ),
            "application_production_fps": application_fps,
            "application_fps_label": (
                "平台/包装层活动产帧 FPS"
                if mismatch_proven
                else "应用活动产帧 FPS"
            ),
            "effective_presentation_fps": presentation_fps,
            "presentation_fps_available": presentation_fps is not None,
            "problem_interval_fps": problem_interval_fps,
            "problem_interval_frame_count": problem_interval_frame_count,
            "problem_interval_fps_source": problem_interval_source,
            "problem_interval_source_label": {
                "target-mapped-render-service": "目标帧映射 RenderService",
                "global-render-service-output": "RenderService 输出线程",
                "application-frame-owner-fallback": "应用 Frame 归属线程回退",
            }.get(problem_interval_source, problem_interval_source),
            "problem_interval_target_attributed": bool(
                metrics.get("problem_interval_target_attributed")
            ),
            "problem_interval_duration_ms": duration_ms,
            "whole_window_frame_rate": problem_interval_fps,
            "whole_window_rate_meaningful": bool(
                metrics.get("interval_average_fps_meaningful")
            ),
            "application_actual_frames": actual_frames,
            "mapped_presented_frames": int(
                metrics.get("mapped_presented_frames") or 0
            ),
            "render_service_actual_frames": int(
                render_service_interval.get("actual_frames") or 0
            ),
            "render_service_expected_frames": int(
                render_service_interval.get("expected_frames") or 0
            ),
            "render_service_dropped_frame_candidates": int(
                render_service_interval.get("dropped_frame_candidates") or 0
            ),
            "expected_frame_slots": int(
                metrics.get("expected_frame_slots") or 0
            ),
            "dropped_frame_candidates": int(
                metrics.get("dropped_frame_candidates") or 0
            ),
            "slow_application_frames": int(
                metrics.get("slow_application_frames") or 0
            ),
            "jank_frames": int(metrics.get("jank_frames") or 0),
            "jank_rate": float(metrics.get("jank_rate") or 0.0),
            "total_missed_vsyncs": int(
                metrics.get("total_missed_vsyncs") or 0
            ),
            "p50_ms": duration_stats.get("p50"),
            "p95_ms": duration_stats.get("p95"),
            "p99_ms": duration_stats.get("p99"),
            "maximum_ms": duration_stats.get("maximum"),
            "segments": segments,
            "surface_identity_available": surface_available,
            "frame_owner_ui_mismatch": mismatch_proven,
            "fps_semantics": metrics.get("fps_semantics"),
            "interpretation": (
                "问题区间帧率按 RenderService 输出线程在完整问题区间内完成的 actual "
                "frame 计算；它代表屏幕合成输出，但缺少 frame_maps 时不能证明所有帧"
                "均由目标 Qt Surface 贡献。活动产帧 FPS 仍只描述平台/包装层管线。"
                if problem_interval_source == "global-render-service-output"
                else (
                    "当前 frame_slice 归属线程与 Qt UI/绘制线程不同；这里的活动产帧 FPS "
                    "只能描述已选平台/包装层管线，不能代替目标 Qt Surface 的最终呈现 FPS。"
                    if mismatch_proven
                    else (
                        "活动产帧 FPS 仅统计发生连续产帧的交互簇，静止时间不计入分母。"
                    )
                )
            ),
        }

    @staticmethod
    def _build_startup_module_view(
        *,
        analysis: AnalysisResult,
        database_path: Path | None,
    ) -> dict[str, Any]:
        cold = analysis.cold_start
        if cold is None:
            return {
                "available": False,
                "reason": "当前问题类型没有冷启动区间。",
                "modules": [],
            }
        if database_path is None:
            return {
                "available": False,
                "reason": "没有可用 Trace DB，无法聚合启动模块。",
                "modules": [],
            }

        thread_itids: set[int] = set()
        if cold.resolved_process.main_itid is not None:
            thread_itids.add(cold.resolved_process.main_itid)
        for stage in cold.stages:
            thread_itids.update(
                thread.itid for thread in stage.critical_threads
            )
        try:
            return StartupModuleRepository(database_path).inspect(
                interval_start_ns=cold.start_boundary.timestamp_ns,
                interval_end_ns=cold.end_boundary.timestamp_ns,
                thread_itids=thread_itids,
            )
        except (FileNotFoundError, ValueError, TraceQueryError) as exc:
            return {
                "available": False,
                "reason": str(exc),
                "modules": [],
            }

    @staticmethod
    def _build_perf_view(
        *,
        analysis: AnalysisResult,
        database_path: Path | None,
        evidence_index: EvidenceIndex,
    ) -> dict[str, Any]:
        perf = analysis.perf
        if perf is None:
            return {
                "available": False,
                "reason": "当前 Trace 没有 Perf 分析结果。",
                "event_name": None,
                "hotspots": [],
                "threads": [],
                "modules": [],
                "flame_root": None,
                "flame_max_depth": 0,
                "min_node_share": 0.001,
                "diagnosis": {"available": False},
            }

        event = perf.events[0] if perf.events else None
        hotspots = []
        if event is not None:
            hotspots = ReportProjectionBuilder._actionable_perf_hotspots(
                event=event,
                evidence_index=evidence_index,
            )
        view: dict[str, Any] = {
            "available": True,
            "reason": None,
            "event_name": event.event_name if event is not None else None,
            "event_type_id": (
                event.event_type_id if event is not None else None
            ),
            "hotspots": hotspots,
            "threads": [],
            "modules": [],
            "flame_root": None,
            "flame_max_depth": 0,
            "min_node_share": 0.001,
            "projected_nodes": 0,
            "omitted_nodes": 0,
            "sample_count": perf.sample_count,
            "total_event_count": (
                event.total_event_count if event is not None else 0
            ),
            "scope": perf.collection.scope,
            "selected_thread_ids": [],
            "excluded_thread_count": 0,
            "thread_scope_reason": None,
            "thread_profiles": [],
            "state_profiles": [],
            "active_profile_key": "combined",
            "hotspot_profile_source": "evidence-bottom-up",
            "diagnosis": ReportProjectionBuilder._build_perf_diagnosis(
                analysis,
                hotspots,
            ),
        }
        if database_path is None:
            view["reason"] = "没有可用 Trace DB，无法生成调用树火焰图。"
            return view
        if event is None:
            view["reason"] = (
                "Perf 分析没有事件数据，无法生成线程分布和调用树。"
            )
            return view

        perf_focus = ReportProjectionBuilder._build_perf_focus(
            analysis=analysis,
            evidence_index=evidence_index,
            database_path=database_path,
        )
        selected_thread_ids = perf_focus["selected_thread_ids"]
        thread_scope_reason = perf_focus["selection_reason"]
        known_thread_ids = set(perf.thread_ids)
        view["selected_thread_ids"] = selected_thread_ids
        view["excluded_thread_count"] = len(
            known_thread_ids - set(selected_thread_ids)
        )
        view["thread_scope_reason"] = thread_scope_reason
        view["focus"] = perf_focus
        state_start_ns = perf.interval_start_ns
        state_end_ns = perf.interval_end_ns
        if analysis.problem_interval is not None:
            state_start_ns = (
                analysis.problem_interval.start_boundary.timestamp_ns
            )
            state_end_ns = (
                analysis.problem_interval.end_boundary.timestamp_ns
            )
        view["state_profiles"] = (
            ReportProjectionBuilder._build_perf_thread_state_profiles(
                database_path=database_path,
                interval_start_ns=state_start_ns,
                interval_end_ns=state_end_ns,
                perf_focus=perf_focus,
            )
        )

        try:
            projection = PerfTraceRepository(database_path).project(
                interval_start_ns=perf.interval_start_ns,
                interval_end_ns=perf.interval_end_ns,
                event_type_id=event.event_type_id,
                process_ids=perf.process_ids,
                thread_ids=selected_thread_ids,
            )
        except (FileNotFoundError, ValueError, PerfProjectionError) as exc:
            view["reason"] = str(exc)
            return view

        view.update(projection.as_dict())
        primary_thread_id = perf_focus.get("primary_thread_id")
        try:
            fresh_profile = PerfAnalysisRepository(database_path).inspect(
                interval_start_ns=perf.interval_start_ns,
                interval_end_ns=perf.interval_end_ns,
                process_ids=perf.process_ids,
                thread_ids=selected_thread_ids,
                max_hotspots=50,
                include_thread_profiles=True,
            )
            fresh_event = next(
                (
                    item
                    for item in fresh_profile.get("event_profiles") or []
                    if isinstance(item, dict)
                    and item.get("event_type_id") == event.event_type_id
                ),
                None,
            )
            profile_rows: list[dict[str, Any]] = []
            if fresh_event is not None:
                combined_hotspots = (
                    ReportProjectionBuilder._actionable_perf_hotspots(
                        event=event,
                        evidence_index=evidence_index,
                        raw_event_override=fresh_event,
                        limit=10,
                    )
                )
                profile_rows.append(
                    {
                        "profile_key": "combined",
                        "thread_id": None,
                        "thread_name": "关键线程合并",
                        "sample_count": int(
                            fresh_event.get("sample_count") or 0
                        ),
                        "hotspots": combined_hotspots,
                        "roles": ["多线程概览"],
                        "reasons": ["相关线程 Perf 合并视图"],
                        "has_perf_samples": bool(
                            fresh_event.get("sample_count")
                        ),
                        "is_primary": False,
                    }
                )
            focus_by_tid = {
                item["thread_id"]: item
                for item in perf_focus.get("threads") or []
            }
            for thread_event in fresh_profile.get("thread_profiles") or []:
                if (
                    not isinstance(thread_event, dict)
                    or thread_event.get("event_type_id")
                    != event.event_type_id
                ):
                    continue
                thread_id = thread_event.get("thread_id")
                if not isinstance(thread_id, int):
                    continue
                focus_item = focus_by_tid.get(thread_id, {})
                profile_rows.append(
                    {
                        "profile_key": str(thread_id),
                        "thread_id": thread_id,
                        "thread_name": (
                            focus_item.get("thread_name")
                            or thread_event.get("thread_name")
                            or f"TID {thread_id}"
                        ),
                        "sample_count": int(
                            thread_event.get("sample_count") or 0
                        ),
                        "roles": list(focus_item.get("roles") or []),
                        "reasons": list(focus_item.get("reasons") or []),
                        "has_perf_samples": bool(
                            thread_event.get("sample_count")
                        ),
                        "hotspots": (
                            ReportProjectionBuilder._actionable_perf_hotspots(
                                event=event,
                                evidence_index=evidence_index,
                                raw_event_override=thread_event,
                                limit=10,
                            )
                        ),
                        "is_primary": thread_id == primary_thread_id,
                    }
                )
            present_profile_keys = {
                item["profile_key"] for item in profile_rows
            }
            for focus_item in perf_focus.get("threads") or []:
                thread_id = focus_item.get("thread_id")
                profile_key = str(thread_id)
                if not isinstance(thread_id, int) or (
                    profile_key in present_profile_keys
                ):
                    continue
                profile_rows.append(
                    {
                        "profile_key": profile_key,
                        "thread_id": thread_id,
                        "thread_name": (
                            focus_item.get("thread_name")
                            or f"TID {thread_id}"
                        ),
                        "sample_count": int(
                            focus_item.get("sample_count") or 0
                        ),
                        "roles": list(focus_item.get("roles") or []),
                        "reasons": list(focus_item.get("reasons") or []),
                        "has_perf_samples": False,
                        "hotspots": [],
                        "is_primary": thread_id == primary_thread_id,
                    }
                )
            profile_rows.sort(
                key=lambda item: (
                    item["profile_key"] == "combined",
                    not item["is_primary"],
                    -item["sample_count"],
                )
            )
            active_profile = next(
                (item for item in profile_rows if item["is_primary"]),
                profile_rows[0] if profile_rows else None,
            )
            if active_profile is not None:
                view["thread_profiles"] = profile_rows
                view["active_profile_key"] = active_profile["profile_key"]
                view["hotspots"] = active_profile["hotspots"]
                view["hotspot_profile_source"] = (
                    "selected-thread-deterministic-bottom-up"
                )
                view["diagnosis"] = (
                    ReportProjectionBuilder._build_perf_diagnosis(
                        analysis,
                        active_profile["hotspots"],
                    )
                )
        except (FileNotFoundError, ValueError, RuntimeError):
            pass
        return view

    @staticmethod
    def _build_perf_thread_state_profiles(
        *,
        database_path: Path,
        interval_start_ns: int,
        interval_end_ns: int,
        perf_focus: dict[str, Any],
    ) -> list[dict[str, Any]]:
        focus_threads = [
            item
            for item in perf_focus.get("threads") or []
            if isinstance(item.get("thread_id"), int)
        ]
        if not focus_threads or interval_end_ns <= interval_start_ns:
            return []
        tids = [int(item["thread_id"]) for item in focus_threads]
        placeholders = ",".join("?" for _ in tids)
        try:
            rows = SQLiteTraceRepository(
                database_path,
                query_timeout_seconds=10,
                default_max_rows=64,
                hard_max_rows=128,
                max_result_bytes=256 * 1024,
            ).query(
                "SELECT tid,itid,name,ipid FROM thread "
                f"WHERE tid IN ({placeholders}) ORDER BY tid,itid",
                tids,
                max_rows=128,
            ).rows
        except (FileNotFoundError, TraceQueryError):
            return []
        itid_by_tid: dict[int, int] = {}
        for row in rows:
            tid = row.get("tid")
            itid = row.get("itid")
            if isinstance(tid, int) and isinstance(itid, int):
                itid_by_tid.setdefault(tid, itid)

        state_specs = (
            ("running_ms", "Running", "#21876f"),
            ("runnable_ms", "Runnable", "#d99a2b"),
            ("sleeping_ms", "Sleep", "#cbd7d5"),
            ("uninterruptible_io_ms", "D-state I/O", "#d95d54"),
            (
                "uninterruptible_other_ms",
                "D-state Other",
                "#8b68b6",
            ),
            ("other_ms", "Other", "#6f8796"),
        )
        interval_duration_ms = (
            interval_end_ns - interval_start_ns
        ) / 1_000_000.0
        repository = ThreadExecutionRepository(database_path)
        profiles: list[dict[str, Any]] = []
        for focus_item in focus_threads:
            tid = int(focus_item["thread_id"])
            itid = itid_by_tid.get(tid)
            if itid is None:
                profiles.append(
                    {
                        "profile_key": str(tid),
                        "mode": "thread",
                        "available": False,
                        "thread_id": tid,
                        "thread_name": focus_item.get("thread_name"),
                        "roles": list(focus_item.get("roles") or []),
                        "reason": "Trace DB 中没有匹配的线程身份。",
                    }
                )
                continue
            try:
                raw = repository.inspect(
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                    itid=itid,
                )
            except (FileNotFoundError, ValueError, RuntimeError):
                profiles.append(
                    {
                        "profile_key": str(tid),
                        "mode": "thread",
                        "available": False,
                        "thread_id": tid,
                        "thread_name": focus_item.get("thread_name"),
                        "roles": list(focus_item.get("roles") or []),
                        "reason": "线程调度状态投影失败。",
                    }
                )
                continue
            execution = raw["thread_execution"]
            breakdown = execution["state_breakdown"]
            observed_ms = sum(
                float(breakdown.get(key) or 0.0)
                for key, _, _ in state_specs
            )
            unknown_ms = max(0.0, interval_duration_ms - observed_ms)
            states = [
                {
                    "key": key,
                    "label": label,
                    "color": color,
                    "duration_ms": float(breakdown.get(key) or 0.0),
                    "share": (
                        float(breakdown.get(key) or 0.0)
                        / interval_duration_ms
                        if interval_duration_ms > 0
                        else 0.0
                    ),
                }
                for key, label, color in state_specs
            ]
            if unknown_ms > 0.0005:
                states.append(
                    {
                        "key": "unknown_ms",
                        "label": "Unknown",
                        "color": "#edf1f1",
                        "duration_ms": unknown_ms,
                        "share": (
                            unknown_ms / interval_duration_ms
                            if interval_duration_ms > 0
                            else 0.0
                        ),
                    }
                )
            cpu_distribution = list(execution.get("cpu_distribution") or [])
            profiles.append(
                {
                    "profile_key": str(tid),
                    "mode": "thread",
                    "available": True,
                    "thread_id": tid,
                    "itid": itid,
                    "thread_name": (
                        focus_item.get("thread_name")
                        or execution.get("thread_name")
                        or f"TID {tid}"
                    ),
                    "process_name": execution.get("process_name"),
                    "roles": list(focus_item.get("roles") or []),
                    "reasons": list(focus_item.get("reasons") or []),
                    "sample_count": int(
                        focus_item.get("sample_count") or 0
                    ),
                    "interval_duration_ms": interval_duration_ms,
                    "states": states,
                    "coverage": (
                        min(observed_ms, interval_duration_ms)
                        / interval_duration_ms
                        if interval_duration_ms > 0
                        else 0.0
                    ),
                    "longest_running_ms": float(
                        execution.get("longest_running_ms") or 0.0
                    ),
                    "longest_runnable_ms": float(
                        execution.get("longest_runnable_ms") or 0.0
                    ),
                    "longest_sleep_ms": float(
                        execution.get("longest_sleep_ms") or 0.0
                    ),
                    "diagnosis": str(execution.get("diagnosis") or ""),
                    "assessment": str(execution.get("assessment") or ""),
                    "priority": execution.get("priority") or {},
                    "cpu_migrations": int(
                        execution.get("cpu_migrations") or 0
                    ),
                    "schedule_slices": int(
                        execution.get("schedule_slices") or 0
                    ),
                    "cpu_distribution": cpu_distribution,
                    "top_cpu": (
                        cpu_distribution[0] if cpu_distribution else None
                    ),
                    "limitations": list(raw.get("limitations") or []),
                }
            )
        if not profiles:
            return []
        return [
            {
                "profile_key": "combined",
                "mode": "comparison",
                "available": True,
                "thread_name": "关键线程对比",
                "threads": profiles,
                "interval_duration_ms": interval_duration_ms,
                "note": (
                    "每一行都以完整问题区间为 100%；线程可并行，"
                    "不同线程的状态占比不能横向相加。"
                ),
            },
            *profiles,
        ]

    @staticmethod
    def _build_perf_focus(
        *,
        analysis: AnalysisResult,
        evidence_index: EvidenceIndex,
        database_path: Path | None,
        max_threads: int = 6,
    ) -> dict[str, Any]:
        perf = analysis.perf
        if perf is None or not perf.thread_ids:
            return {
                "primary_thread_id": None,
                "selected_thread_ids": [],
                "threads": [],
                "selection_reason": "当前没有可用的关键线程 Perf 范围。",
                "decision_source": "unavailable",
            }

        original_thread_ids = {int(value) for value in perf.thread_ids}
        eligible = list(dict.fromkeys(original_thread_ids))
        frame_roles_by_tid: dict[int, list[str]] = {}
        frame_main_tid: int | None = None
        frame_record = evidence_index.last("inspect_frame_jank")
        if database_path is not None and frame_record is not None:
            frame_data = frame_record.data
            frame_interval = frame_data.get("interval") or {}
            frame_process = frame_data.get("target_process") or {}
            try:
                fresh_frame = FrameJankRepository(database_path).inspect(
                    target_ipid=int(frame_process["ipid"]),
                    interval_start_ns=int(frame_interval["start_ns"]),
                    interval_end_ns=int(frame_interval["end_ns"]),
                    refresh_rate_hz=(
                        (frame_data.get("cadence") or {}).get(
                            "dominant_refresh_rate_hz"
                        )
                    ),
                    frame_producer_itid=None,
                    max_bad_frames=20,
                    max_clusters=10,
                )
                frame_architecture = (
                    fresh_frame.get("render_architecture") or {}
                )
                eligible = list(
                    dict.fromkeys(
                        [
                            *eligible,
                            *(
                                frame_architecture.get(
                                    "recommended_perf_thread_ids"
                                )
                                or []
                            ),
                        ]
                    )
                )
                for raw in frame_architecture.get("thread_roles") or []:
                    tid = raw.get("tid")
                    if not isinstance(tid, int):
                        continue
                    roles = [str(role) for role in raw.get("roles") or []]
                    frame_roles_by_tid[tid] = roles
                    if "application-main-thread" in roles:
                        frame_main_tid = tid
            except (
                KeyError,
                TypeError,
                ValueError,
                FileNotFoundError,
                sqlite3.DatabaseError,
            ):
                pass
        stats: dict[int, dict[str, Any]] = {
            tid: {
                "thread_id": tid,
                "thread_name": f"TID {tid}",
                "sample_count": 0,
                "event_count": 0,
                "sample_share": 0.0,
                "event_share": 0.0,
                "running_ms": 0.0,
                "running_share": 0.0,
                "roles": [],
                "reasons": ["inspect_perf_profile 已限定的关键线程"],
            }
            for tid in eligible
        }

        profile_record = evidence_index.perf_profile()
        raw_event: dict[str, Any] | None = None
        if profile_record is not None:
            target_event_id = (
                perf.events[0].event_type_id if perf.events else None
            )
            raw_event = next(
                (
                    item
                    for item in profile_record.data.get("event_profiles") or []
                    if isinstance(item, dict)
                    and (
                        target_event_id is None
                        or item.get("event_type_id") == target_event_id
                    )
                ),
                None,
            )
        if raw_event is not None:
            for row in raw_event.get("threads") or []:
                if not isinstance(row, dict):
                    continue
                tid = row.get("thread_id")
                if not isinstance(tid, int) or tid not in stats:
                    continue
                stats[tid].update(
                    {
                        "thread_name": row.get("thread_name") or f"TID {tid}",
                        "sample_count": int(row.get("sample_count") or 0),
                        "event_count": int(row.get("event_count") or 0),
                        "sample_share": float(row.get("sample_share") or 0),
                        "event_share": float(row.get("event_share") or 0),
                    }
                )

        main_tid: int | None = None
        role_threads: list[Any] = []
        if analysis.cold_start is not None:
            main_tid = analysis.cold_start.resolved_process.main_tid
            role_threads = [
                thread
                for stage in analysis.cold_start.stages
                for thread in stage.critical_threads
            ]
        elif analysis.completion_latency is not None:
            main_tid = analysis.completion_latency.resolved_process.main_tid
            role_threads = [
                thread
                for phase in analysis.completion_latency.phases
                for thread in phase.critical_threads
            ]
        else:
            main_tid = frame_main_tid
        if main_tid in stats:
            stats[main_tid]["roles"].append("进程主线程")
            stats[main_tid]["reasons"].append("主线程始终作为上下文保留")

        for tid, roles in frame_roles_by_tid.items():
            if tid not in stats:
                continue
            if any(
                role in roles
                for role in (
                    "framework-ui-or-js-candidate",
                    "framework-ui-event-loop-candidate",
                    "surface-submit-or-ui-paint-candidate",
                )
            ):
                stats[tid]["roles"].append("UI / 绘制关键线程")
                stats[tid]["reasons"].append(
                    "线程名与绘制/Buffer 提交证据共同证明框架角色"
                )
            if "target-frame-mapped-render-service" in roles:
                stats[tid]["roles"].append("目标帧 RenderService")
                stats[tid]["reasons"].append("frame_maps 目标帧映射")

        ui_tokens = (
            "useragent",
            "qtmainthread",
            "flutterui",
            "flutter ui",
            "dart ui",
            "ui thread",
            "uithread",
            "raster",
        )
        for tid, item in stats.items():
            lowered = str(item["thread_name"]).lower()
            if any(token in lowered for token in ui_tokens):
                item["roles"].append("UI / 事件循环线程")
                item["reasons"].append("框架线程名称与 UI 角色提示匹配")
        for thread in role_threads:
            if thread.tid not in stats:
                continue
            item = stats[thread.tid]
            if "阶段关键线程" not in item["roles"]:
                item["roles"].append("阶段关键线程")
            item["reasons"].append("确定性阶段线程 Evidence")

        interval_start_ns = perf.interval_start_ns
        interval_end_ns = perf.interval_end_ns
        if analysis.problem_interval is not None:
            interval_start_ns = (
                analysis.problem_interval.start_boundary.timestamp_ns
            )
            interval_end_ns = (
                analysis.problem_interval.end_boundary.timestamp_ns
            )
        if database_path is not None and eligible:
            placeholders = ",".join("?" for _ in eligible)
            try:
                result = SQLiteTraceRepository(
                    database_path,
                    query_timeout_seconds=10,
                    default_max_rows=max_threads * 4,
                    hard_max_rows=64,
                    max_result_bytes=512 * 1024,
                ).query(
                    "SELECT t.tid, t.name AS thread_name, "
                    "SUM(MIN(s.ts + s.dur, ?) - MAX(s.ts, ?)) AS running_ns "
                    "FROM sched_slice AS s JOIN thread AS t ON t.itid = s.itid "
                    f"WHERE t.tid IN ({placeholders}) AND s.dur > 0 "
                    "AND s.ts < ? AND s.ts + s.dur > ? "
                    "GROUP BY t.tid, t.name ORDER BY running_ns DESC",
                    [
                        interval_end_ns,
                        interval_start_ns,
                        *eligible,
                        interval_end_ns,
                        interval_start_ns,
                    ],
                    max_rows=min(64, max_threads * 4),
                )
                duration_ms = (
                    interval_end_ns - interval_start_ns
                ) / 1_000_000.0
                for row in result.rows:
                    tid = row.get("tid")
                    running_ns = row.get("running_ns")
                    if tid not in stats or not isinstance(
                        running_ns, (int, float)
                    ):
                        continue
                    running_ms = float(running_ns) / 1_000_000.0
                    stats[tid]["running_ms"] = running_ms
                    stats[tid]["running_share"] = (
                        running_ms / duration_ms if duration_ms > 0 else 0.0
                    )
                    if stats[tid]["thread_name"] == f"TID {tid}":
                        stats[tid]["thread_name"] = (
                            row.get("thread_name") or f"TID {tid}"
                        )
            except (FileNotFoundError, TraceQueryError):
                pass

            try:
                perf_rows = SQLiteTraceRepository(
                    database_path,
                    query_timeout_seconds=10,
                    default_max_rows=max_threads * 4,
                    hard_max_rows=64,
                    max_result_bytes=512 * 1024,
                ).query(
                    "SELECT pt.thread_id, pt.thread_name, COUNT(*) AS samples, "
                    "SUM(ps.event_count) AS event_count "
                    "FROM perf_sample AS ps JOIN perf_thread AS pt "
                    "ON pt.thread_id=ps.thread_id "
                    f"WHERE pt.thread_id IN ({placeholders}) "
                    "AND ps.timestamp_trace>=? AND ps.timestamp_trace<? "
                    "GROUP BY pt.thread_id,pt.thread_name",
                    [*eligible, interval_start_ns, interval_end_ns],
                    max_rows=min(64, max_threads * 4),
                ).rows
                total_samples = sum(
                    int(row.get("samples") or 0) for row in perf_rows
                )
                total_events = sum(
                    int(row.get("event_count") or 0) for row in perf_rows
                )
                for row in perf_rows:
                    tid = row.get("thread_id")
                    if tid not in stats:
                        continue
                    samples = int(row.get("samples") or 0)
                    event_count = int(row.get("event_count") or 0)
                    stats[tid]["sample_count"] = samples
                    stats[tid]["event_count"] = event_count
                    stats[tid]["sample_share"] = (
                        samples / total_samples if total_samples else 0.0
                    )
                    stats[tid]["event_share"] = (
                        event_count / total_events if total_events else 0.0
                    )
                    stats[tid]["thread_name"] = (
                        row.get("thread_name")
                        or stats[tid]["thread_name"]
                    )
            except (FileNotFoundError, TraceQueryError):
                pass

        sampled_stats = [
            item for item in stats.values() if item["sample_count"] > 0
        ]
        structural_stats = [
            item for item in stats.values() if item["roles"]
        ]
        candidate_stats = list(
            {
                item["thread_id"]: item
                for item in [*sampled_stats, *structural_stats]
            }.values()
        ) or list(stats.values())
        candidate_ids = {
            item["thread_id"] for item in candidate_stats
        }
        perf_ranked = sorted(
            candidate_stats,
            key=lambda item: (
                -max(item["sample_share"], item["event_share"]),
                -item["sample_count"],
                item["thread_id"],
            ),
        )
        running_ranked = sorted(
            candidate_stats,
            key=lambda item: (
                -item["running_ms"],
                item["thread_id"],
            ),
        )
        selected: set[int] = {
            item["thread_id"]
            for item in candidate_stats
            if item["roles"]
        }
        selected.update(
            item["thread_id"]
            for item in perf_ranked[:2]
            if item["sample_count"] > 0
        )
        selected.update(
            item["thread_id"]
            for item in perf_ranked
            if max(item["sample_share"], item["event_share"]) >= 0.05
        )
        selected.update(
            item["thread_id"]
            for item in running_ranked[:2]
            if item["running_ms"] > 0
        )
        selected.update(
            item["thread_id"]
            for item in running_ranked
            if item["running_share"] >= 0.05
        )
        if len(candidate_ids) <= max_threads:
            selected.update(candidate_ids)

        ordered = sorted(
            (stats[tid] for tid in selected),
            key=lambda item: (
                not bool(item["roles"]),
                -max(item["sample_share"], item["event_share"]),
                -item["running_ms"],
                item["thread_id"],
            ),
        )[:max_threads]
        if main_tid in candidate_ids and main_tid in selected and all(
            item["thread_id"] != main_tid for item in ordered
        ):
            ordered[-1] = stats[main_tid]

        selected_ids = [item["thread_id"] for item in ordered]
        hinted_primary = None
        if raw_event is not None:
            hinted_primary = raw_event.get("primary_thread_id")
        if hinted_primary not in selected_ids and profile_record is not None:
            hinted_primary = profile_record.data.get("primary_thread_id")
        if hinted_primary in selected_ids:
            primary_tid = int(hinted_primary)
            decision_source = "agent-skill-hint"
            primary_reason = "Agent/Skill 在确定性候选集合内选择"
        else:
            primary = max(
                ordered,
                key=lambda item: (
                    max(item["sample_share"], item["event_share"]),
                    item["running_share"],
                    item["running_ms"],
                ),
                default=None,
            )
            primary_tid = (
                primary["thread_id"] if primary is not None else None
            )
            decision_source = "deterministic-fallback"
            primary_reason = (
                "按 Perf 权重、Running 占比和 Running 时长依次选择"
            )

        for item in ordered:
            item["is_primary"] = item["thread_id"] == primary_tid
            if item["sample_share"] >= 0.05:
                item["reasons"].append(
                    f"Perf samples {item['sample_share'] * 100:.1f}%"
                )
            if item["running_share"] >= 0.05:
                item["reasons"].append(
                    f"Running 占问题窗口 {item['running_share'] * 100:.1f}%"
                )

        return {
            "primary_thread_id": primary_tid,
            "selected_thread_ids": selected_ids,
            "threads": ordered,
            "selection_reason": (
                "角色/因果线程必选，Perf Top 2 或占比≥5%、"
                "Running Top 2 或占问题窗口≥5%自动补选；最多 6 个线程。"
            ),
            "decision_source": decision_source,
            "primary_reason": primary_reason,
        }

    @staticmethod
    def _actionable_perf_hotspots(
        *,
        event: Any,
        evidence_index: EvidenceIndex,
        raw_event_override: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Prefer actionable Bottom-up leaves over generic inclusive roots."""

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        record = evidence_index.perf_profile()
        raw_event: dict[str, Any] | None = raw_event_override
        if raw_event is None and record is not None:
            for candidate in record.data.get("event_profiles") or []:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("event_type_id")
                    == event.event_type_id
                ):
                    raw_event = candidate
                    break

        diagnostics = (
            raw_event.get("bottom_up_diagnostics") or []
            if raw_event is not None
            else []
        )
        ranked_diagnostics = sorted(
            (item for item in diagnostics if isinstance(item, dict)),
            key=lambda item: (
                -float(
                    item.get("self_event_share")
                    or item.get("self_sample_share")
                    or 0
                ),
                -int(item.get("self_samples") or 0),
            ),
        )
        for item in ranked_diagnostics:
            supporting_symbols = [
                value
                for value in item.get("supporting_symbols") or []
                if isinstance(value, dict) and value.get("symbol")
            ]
            symbol = str(
                item.get("application_function")
                or (
                    supporting_symbols[0].get("symbol")
                    if supporting_symbols
                    else None
                )
                or item.get("operation")
                or ""
            ).strip()
            if (
                not symbol
                or ReportProjectionBuilder._is_generic_perf_symbol(symbol)
                or symbol.lower() in seen
            ):
                continue
            share = float(
                item.get("self_event_share")
                or item.get("self_sample_share")
                or 0
            )
            if share < 0.005 or int(item.get("self_samples") or 0) < 3:
                continue
            reverse_path = [
                str(value)
                for value in item.get("representative_reverse_path") or []
                if str(value).strip()
            ]
            rows.append(
                {
                    "symbol": symbol,
                    "file_path": None,
                    "layer": "actionable-bottom-up",
                    "self_samples": int(item.get("self_samples") or 0),
                    "inclusive_samples": int(
                        item.get("representative_path_samples")
                        or item.get("self_samples")
                        or 0
                    ),
                    "self_share": share,
                    "inclusive_share": share,
                    "critical_path_relevance": (
                        f"Bottom-up 聚合：{item.get('operation') or symbol}；"
                        "需与当前关键阶段和线程交叉验证"
                    ),
                    "aggregation_source": "bottom-up",
                    "aggregation_group": item.get("operation") or symbol,
                    "supporting_symbols": supporting_symbols,
                    "representative_reverse_path": reverse_path,
                    "optimization_direction": item.get(
                        "optimization_direction"
                    ),
                }
            )
            seen.add(symbol.lower())
            if len(rows) >= limit:
                return rows

        specific_top_down = sorted(
            (
                item
                for item in event.hotspots
                if not ReportProjectionBuilder._is_generic_perf_symbol(
                    item.symbol
                )
            ),
            key=lambda item: (
                -item.self_samples,
                -item.inclusive_share,
                -item.inclusive_samples,
            ),
        )
        for item in specific_top_down:
            if item.symbol.lower() in seen:
                continue
            rows.append(
                {
                    "symbol": item.symbol,
                    "file_path": item.file_path,
                    "layer": item.layer,
                    "self_samples": item.self_samples,
                    "inclusive_samples": item.inclusive_samples,
                    "self_share": (
                        item.self_samples / event.sample_count
                        if event.sample_count
                        else 0
                    ),
                    "inclusive_share": item.inclusive_share,
                    "critical_path_relevance": item.critical_path_relevance,
                    "aggregation_source": "top-down",
                    "aggregation_group": None,
                    "supporting_symbols": [],
                    "representative_reverse_path": [],
                    "optimization_direction": None,
                }
            )
            seen.add(item.symbol.lower())
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _is_generic_perf_symbol(symbol: str) -> bool:
        label = str(symbol or "").strip().lower()
        if not label or label in {
            ".plt",
            ".got",
            "[unknown]",
            "unknown",
        }:
            return True
        if re.search(r"\+0x[0-9a-f]+$", label) or label.endswith(
            "+offsets"
        ):
            return True
        generic_markers = (
            "mainthread::start",
            "eventrunner::run",
            "appspawn",
            "__libc_start",
            "start_thread",
            "qcoreapplication::exec",
            "qeventloop::exec",
            "qeventdispatcher",
            "qapplicationprivate::notify_helper",
            "qcoreapplication::notifyinternal",
            "qcoreapplicationprivate::sendpostedevents",
            "qthreadpostobject",
            "runmain",
            "applauncher::run",
            "dtapplication::exec",
            "thread trampoline",
        )
        return any(marker in label for marker in generic_markers)

    @staticmethod
    def _build_perf_diagnosis(
        analysis: AnalysisResult,
        hotspots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        perf = analysis.perf
        if perf is None:
            return {"available": False}

        perf_evidence_ids = set(perf.evidence_ids)
        for event in perf.events:
            perf_evidence_ids.update(event.evidence_ids)
            for hotspot in event.hotspots:
                perf_evidence_ids.update(hotspot.evidence_ids)

        severity_rank = {
            "low": 1,
            "medium": 2,
            "high": 3,
            "critical": 4,
        }
        status_rank = {
            "observed": 1,
            "suspected": 2,
            "confirmed": 3,
        }
        finding_candidates = []
        for finding in analysis.findings:
            overlap = perf_evidence_ids.intersection(
                finding.evidence_ids
            )
            if not overlap:
                continue
            finding_candidates.append(
                (
                    len(overlap),
                    status_rank.get(finding.status.value, 0),
                    severity_rank.get(finding.severity.value, 0),
                    finding.confidence,
                    finding,
                )
            )
        primary_finding = (
            max(finding_candidates, key=lambda item: item[:4])[-1]
            if finding_candidates
            else None
        )

        cold = analysis.cold_start
        completion = analysis.completion_latency
        intervals = (
            list(cold.stages)
            if cold is not None
            else list(completion.phases)
            if completion is not None
            else []
        )
        total_duration_ms = (
            cold.total_duration_ms
            if cold is not None
            else completion.completion_latency_ms
            if completion is not None
            else None
        )
        dominant_stage = max(
            intervals,
            key=lambda stage: stage.duration_ms,
            default=None,
        )
        critical_thread = (
            dominant_stage.critical_threads[0]
            if dominant_stage is not None
            and dominant_stage.critical_threads
            else None
        )
        stage = None
        if dominant_stage is not None:
            stage = {
                "name": dominant_stage.name,
                "duration_ms": dominant_stage.duration_ms,
                "total_share": (
                    dominant_stage.duration_ms / total_duration_ms
                    if total_duration_ms is not None
                    and total_duration_ms > 0
                    else None
                ),
                "assessment": dominant_stage.assessment,
            }

        diagnosis_labels = {
            "cpu-bound": "CPU 自身执行",
            "cpu-contention": "CPU 调度竞争",
            "blocked-wait": "阻塞等待",
            "io-wait": "I/O 等待",
            "mixed": "混合型",
            "inconclusive": "证据不足",
        }
        thread = None
        if critical_thread is not None:
            states = critical_thread.state_breakdown
            diagnosis_value = critical_thread.diagnosis.value
            thread = {
                "name": (
                    critical_thread.thread_name
                    or critical_thread.process_name
                ),
                "tid": critical_thread.tid,
                "diagnosis": diagnosis_value,
                "diagnosis_label": diagnosis_labels.get(
                    diagnosis_value,
                    diagnosis_value,
                ),
                "running_ms": states.running_ms,
                "runnable_ms": states.runnable_ms,
                "sleeping_ms": states.sleeping_ms,
                "assessment": critical_thread.assessment,
            }

        generic_roots = (
            "mainthread::start",
            "eventrunner::run",
            "appspawn",
            "libbegetutil",
            "ld-musl",
        )
        anchors = [
            hotspot
            for hotspot in hotspots
            if not any(
                marker in str(hotspot["symbol"]).lower()
                for marker in generic_roots
            )
        ][:4]
        if not anchors:
            anchors = hotspots[:4]

        evidence_ids: list[str] = []
        for evidence_id in (
            list(primary_finding.evidence_ids)
            if primary_finding is not None
            else list(perf.evidence_ids)
        ):
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)

        return {
            "available": bool(primary_finding or anchors or stage),
            "title": (
                primary_finding.title
                if primary_finding is not None
                else "Perf 热点已定位，尚未形成独立根因结论"
            ),
            "status": (
                primary_finding.status.value
                if primary_finding is not None
                else "observed"
            ),
            "confidence": (
                primary_finding.confidence
                if primary_finding is not None
                else perf.confidence
            ),
            "analysis": (
                primary_finding.analysis
                if primary_finding is not None
                else perf.assessment
            ),
            "recommendation": (
                primary_finding.recommendation
                if primary_finding is not None
                else None
            ),
            "verification": (
                primary_finding.verification
                if primary_finding is not None
                else None
            ),
            "stage": stage,
            "thread": thread,
            "anchors": anchors,
            "evidence_ids": evidence_ids[:8],
        }

    @staticmethod
    def _relevant_perf_thread_scope(
        analysis: AnalysisResult,
    ) -> tuple[list[int], str]:
        perf = analysis.perf
        if perf is None:
            return [], "当前没有 Perf 分析结果。"

        selected: set[int] = set()
        cold = analysis.cold_start
        if cold is not None:
            if cold.resolved_process.main_tid is not None:
                selected.add(cold.resolved_process.main_tid)
            for stage in cold.stages:
                selected.update(
                    thread.tid
                    for thread in stage.critical_threads
                )
        completion = analysis.completion_latency
        if completion is not None:
            if completion.resolved_process.main_tid is not None:
                selected.add(completion.resolved_process.main_tid)
            for phase in completion.phases:
                selected.update(
                    thread.tid
                    for thread in phase.critical_threads
                )
        if perf.thread_ids:
            available = set(perf.thread_ids)
            selected &= available

        if selected:
            return (
                sorted(selected),
                "仅展示已被 Trace 关键阶段证明相关的线程；"
                "目标进程其他 Perf 线程不进入分布图和火焰图。",
            )
        if 0 < len(perf.thread_ids) <= 4:
            return (
                sorted(set(perf.thread_ids)),
                "Perf 结果只声明了少量显式线程，全部视为已选相关线程。",
            )
        return (
            [],
            "缺少可证明的关键线程，暂按目标进程范围投影；"
            "该结果不应被解释为所有线程都与问题相关。",
        )

    def _build_timeline(
        self,
        *,
        analysis: AnalysisResult,
        evidence_index: EvidenceIndex,
        database_path: Path | None,
    ) -> dict[str, Any]:
        if analysis.completion_latency is not None:
            return self._build_completion_timeline(
                analysis=analysis,
                evidence_index=evidence_index,
                database_path=database_path,
            )
        if analysis.cold_start is not None:
            return self._build_cold_start_timeline(
                analysis=analysis,
                evidence_index=evidence_index,
                database_path=database_path,
            )
        frame_record = evidence_index.last("inspect_frame_jank")
        if frame_record is not None:
            return self._build_frame_jank_timeline(
                frame_record=frame_record,
                database_path=database_path,
            )
        return {
            "available": False,
            "reason": "当前问题类型没有可用的时间线 Evidence。",
            "duration_ms": 0.0,
            "max_depth": 0,
            "source_evidence_id": None,
            "cpu_distribution": [],
            "lanes": [],
            "boundaries": [],
            "links": [],
        }

    def _build_frame_jank_timeline(
        self,
        *,
        frame_record: EvidenceRecord,
        database_path: Path | None,
    ) -> dict[str, Any]:
        data = frame_record.data
        interval = data.get("interval") or {}
        process = data.get("target_process") or {}
        start_ns = interval.get("start_ns")
        end_ns = interval.get("end_ns")
        target_ipid = process.get("ipid")
        if (
            not isinstance(start_ns, int)
            or not isinstance(end_ns, int)
            or end_ns <= start_ns
            or not isinstance(target_ipid, int)
        ):
            return {
                "available": False,
                "reason": "帧率 Evidence 缺少有效进程或问题区间。",
                "duration_ms": 0.0,
                "max_depth": 0,
                "source_evidence_id": frame_record.evidence_id,
                "cpu_distribution": [],
                "lanes": [],
                "boundaries": [],
                "links": [],
            }

        projection_data = data
        if database_path is not None:
            try:
                projection_data = FrameJankRepository(database_path).inspect(
                    target_ipid=target_ipid,
                    interval_start_ns=start_ns,
                    interval_end_ns=end_ns,
                    refresh_rate_hz=(
                        (data.get("cadence") or {}).get(
                            "dominant_refresh_rate_hz"
                        )
                    ),
                    frame_producer_itid=None,
                    max_bad_frames=30,
                    max_clusters=20,
                )
            except (FileNotFoundError, ValueError, sqlite3.DatabaseError):
                projection_data = data

        architecture = projection_data.get("render_architecture") or {}
        thread_roles = architecture.get("thread_roles") or []
        role_labels = {
            "application-main-thread": "进程主线程",
            "selected-application-frame-producer": "Frame 归属线程",
            "application-frame-producer-candidate": "Frame 归属候选",
            "framework-ui-or-js-candidate": "框架 UI / JS 候选",
            "framework-ui-event-loop-candidate": "UI / 事件循环线程",
            "surface-submit-or-ui-paint-candidate": "绘制 / Buffer 提交线程",
            "framework-render-or-raster-candidate": "框架渲染线程",
            "target-frame-mapped-render-service": "目标帧 RenderService",
        }
        specs: list[dict[str, Any]] = []
        for raw in thread_roles:
            if not isinstance(raw, dict) or not isinstance(
                raw.get("itid"), int
            ):
                continue
            roles = [
                role_labels.get(str(role), str(role))
                for role in raw.get("roles") or []
            ]
            if not roles:
                continue
            role_set = set(raw.get("roles") or [])
            if "application-main-thread" in role_set:
                order = 0
            elif role_set & {
                "framework-ui-or-js-candidate",
                "framework-ui-event-loop-candidate",
                "surface-submit-or-ui-paint-candidate",
            }:
                order = 1
            elif "selected-application-frame-producer" in role_set:
                order = 2
            elif "target-frame-mapped-render-service" in role_set:
                order = 3
            else:
                continue
            specs.append(
                {
                    "itid": raw["itid"],
                    "tid": raw.get("tid"),
                    "thread_name": raw.get("thread_name")
                    or f"itid {raw['itid']}",
                    "process_name": raw.get("process_name")
                    or process.get("name")
                    or "unknown",
                    "roles": roles,
                    "reasons": list(raw.get("evidence") or []),
                    "order": order,
                    "running_ms": float(raw.get("running_ms") or 0.0),
                }
            )
        deduplicated: dict[int, dict[str, Any]] = {}
        for spec in sorted(
            specs,
            key=lambda item: (
                item["order"],
                -item["running_ms"],
                item["itid"],
            ),
        ):
            existing = deduplicated.get(spec["itid"])
            if existing is None:
                deduplicated[spec["itid"]] = spec
                continue
            for role in spec["roles"]:
                if role not in existing["roles"]:
                    existing["roles"].append(role)
            for reason in spec["reasons"]:
                if reason not in existing["reasons"]:
                    existing["reasons"].append(reason)
            existing["order"] = min(existing["order"], spec["order"])
            existing["running_ms"] = max(
                existing["running_ms"], spec["running_ms"]
            )
        recommended_tids = {
            int(tid)
            for tid in architecture.get("recommended_perf_thread_ids") or []
            if isinstance(tid, int)
        }
        timeline_threads = sorted(
            (
                spec
                for spec in deduplicated.values()
                if spec.get("tid") in recommended_tids
                or any(
                    role in spec["roles"]
                    for role in (
                        role_labels["application-main-thread"],
                        role_labels["selected-application-frame-producer"],
                        role_labels["target-frame-mapped-render-service"],
                    )
                )
            ),
            key=lambda item: (
                item["order"],
                -item["running_ms"],
                item["itid"],
            ),
        )[:6]

        lanes: list[dict[str, Any]] = []
        cadence = projection_data.get("cadence") or {}
        activity_items: list[dict[str, Any]] = []
        for index, segment in enumerate(cadence.get("segments") or []):
            item = self._interval_item(
                item_id=f"frame-activity-{index}",
                start_ns=segment.get("start_ns"),
                end_ns=segment.get("end_ns"),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=f"帧活动簇 {index + 1}",
                role="critical",
                detail={"type": "frame-activity", **segment},
            )
            if item is not None:
                activity_items.append(item)
        if activity_items:
            lanes.append(
                {
                    "key": "frame-activity",
                    "label": "滑动帧活动簇",
                    "sublabel": (
                        f"{len(activity_items)} 个活动区间 · "
                        f"{cadence.get('dominant_refresh_rate_hz') or '未知'}Hz"
                    ),
                    "kind": "stage",
                    "height": 42,
                    "items": activity_items,
                }
            )

        representative_scheduling: dict[str, Any] | None = None
        for spec in timeline_threads:
            itid = spec["itid"]
            scheduling = self._merge_thread_scheduling_lanes(
                cpu_running_lane=self._read_cpu_running_lane(
                    database_path=database_path,
                    target_itid=itid,
                    start_ns=start_ns,
                    end_ns=end_ns,
                ),
                thread_state_lane=self._read_thread_state_lane(
                    database_path=database_path,
                    target_itid=itid,
                    start_ns=start_ns,
                    end_ns=end_ns,
                ),
            )
            role_label = " / ".join(spec["roles"])
            reason_label = "；".join(spec["reasons"])
            if scheduling is not None:
                scheduling["key"] = f"frame-thread-{itid}-scheduling"
                scheduling["label"] = (
                    f"{spec['thread_name']} · CPU / State"
                )
                scheduling["sublabel"] = (
                    f"{role_label} · {scheduling['sublabel']}"
                    + (f" · {reason_label}" if reason_label else "")
                )
                lanes.append(scheduling)
                if spec["order"] == 1 or representative_scheduling is None:
                    representative_scheduling = scheduling
            slices = self._read_main_thread_slice_lane(
                database_path=database_path,
                target_itid=itid,
                process_name=spec["process_name"],
                thread_name=spec["thread_name"],
                role_label=role_label,
                lane_key=f"frame-thread-{itid}-callstack",
                start_ns=start_ns,
                end_ns=end_ns,
            )
            if slices is not None:
                if reason_label:
                    slices["sublabel"] += f" · {reason_label}"
                lanes.append(slices)

        frame_lanes, frame_links = self._read_frame_jank_frame_lanes(
            database_path=database_path,
            target_ipid=target_ipid,
            start_ns=start_ns,
            end_ns=end_ns,
            process_name=str(process.get("name") or "目标应用"),
        )
        lanes.extend(frame_lanes)
        return {
            "available": bool(lanes),
            "reason": "" if lanes else "问题窗口内没有可绘制的帧率证据。",
            "title": "帧卡顿多架构关键线程泳道",
            "subtitle": (
                "主线程、实际 UI/事件循环、绘制提交线程、App/Render 帧"
                "共享同一时间轴；主线程不默认等同于 UI 线程。"
            ),
            "start_ns": start_ns,
            "end_ns": end_ns,
            "duration_ms": (end_ns - start_ns) / 1_000_000.0,
            "max_depth": max(
                (int(lane.get("max_depth") or 0) for lane in lanes),
                default=0,
            ),
            "cpu_distribution": (
                representative_scheduling.get("cpu_distribution", [])
                if representative_scheduling is not None
                else []
            ),
            "lanes": lanes,
            "boundaries": [
                {
                    "label": "问题开始",
                    "kind": "input",
                    "position": 0,
                    "timestamp_ns": start_ns,
                    "source": "frame-jank.interval",
                    "confidence": 1.0,
                },
                {
                    "label": "问题结束",
                    "kind": "completion",
                    "position": 100,
                    "timestamp_ns": end_ns,
                    "source": "frame-jank.interval",
                    "confidence": 1.0,
                },
            ],
            "links": frame_links[:160],
            "source_evidence_id": frame_record.evidence_id,
        }

    def _read_frame_jank_frame_lanes(
        self,
        *,
        database_path: Path | None,
        target_ipid: int,
        start_ns: int,
        end_ns: int,
        process_name: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        if database_path is None:
            return [], []
        try:
            repository = SQLiteTraceRepository(
                database_path,
                query_timeout_seconds=10,
                default_max_rows=1000,
                hard_max_rows=2000,
                max_result_bytes=2 * 1024 * 1024,
            )
            frames = repository.query(
                "SELECT id,ts,dur,ts+dur AS end_ns,vsync,itid,"
                "type,type_desc,flag,frame_no FROM frame_slice "
                "WHERE ipid=? AND ts<? AND ts+dur>? "
                "ORDER BY ts,id LIMIT 1000",
                [target_ipid, end_ns, start_ns],
                max_rows=1000,
            ).rows
        except (FileNotFoundError, TraceQueryError):
            return [], []

        items: list[dict[str, Any]] = []
        frame_ids: set[int] = set()
        for frame in frames:
            frame_id = frame.get("id")
            if not isinstance(frame_id, int):
                continue
            type_desc = str(frame.get("type_desc") or "").lower()
            actual = type_desc in {"actual", "actural"} or (
                not type_desc and frame.get("type") == 0
            )
            item = self._interval_item(
                item_id=f"frame-jank-app-{frame_id}",
                start_ns=frame.get("ts"),
                end_ns=frame.get("end_ns"),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label="Actual Frame" if actual else "Expected Frame",
                role="frame-actual" if actual else "frame-expected",
                detail={"type": "frame", **frame},
            )
            if item is not None:
                items.append(item)
                frame_ids.add(frame_id)
        lanes: list[dict[str, Any]] = []
        if items:
            lanes.append(
                {
                    "key": "frame-jank-app-frames",
                    "label": "App Frame",
                    "sublabel": f"{process_name} · actual / expected",
                    "kind": "frame",
                    "height": 38,
                    "items": items,
                }
            )
        if not frame_ids:
            return lanes, []

        try:
            mapped = repository.query(
                "SELECT m.id AS map_id,app.id AS app_id,"
                "render.id AS render_id,render.ts,render.dur,"
                "render.ts+render.dur AS end_ns,render.type_desc,"
                "p.name AS process_name FROM frame_maps AS m "
                "JOIN frame_slice AS app ON app.id=m.src_row "
                "JOIN frame_slice AS render ON render.id=m.dst_row "
                "LEFT JOIN process AS p ON p.ipid=render.ipid "
                "WHERE app.ipid=? AND app.ts<? AND app.ts+app.dur>? "
                "ORDER BY render.ts,m.id LIMIT 1000",
                [target_ipid, end_ns, start_ns],
                max_rows=1000,
            ).rows
        except TraceQueryError:
            return lanes, []
        render_items: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        seen: set[int] = set()
        render_process = "RenderService"
        for row in mapped:
            app_id = row.get("app_id")
            render_id = row.get("render_id")
            if (
                not isinstance(app_id, int)
                or not isinstance(render_id, int)
                or app_id not in frame_ids
            ):
                continue
            item_id = f"frame-jank-render-{render_id}"
            if render_id not in seen:
                render_process = str(
                    row.get("process_name") or render_process
                )
                item = self._interval_item(
                    item_id=item_id,
                    start_ns=row.get("ts"),
                    end_ns=row.get("end_ns"),
                    window_start_ns=start_ns,
                    window_end_ns=end_ns,
                    label="RS Frame",
                    role="render-frame",
                    detail={"type": "related-frame", **row},
                )
                if item is not None:
                    render_items.append(item)
                    seen.add(render_id)
            if render_id in seen:
                links.append(
                    {
                        "source": f"frame-jank-app-{app_id}",
                        "target": item_id,
                    }
                )
        if render_items:
            lanes.append(
                {
                    "key": "frame-jank-render-frames",
                    "label": render_process,
                    "sublabel": "frame_maps 关联目标帧",
                    "kind": "render",
                    "height": 38,
                    "items": render_items,
                }
            )
        return lanes, links

    def _build_cold_start_timeline(
        self,
        *,
        analysis: AnalysisResult,
        evidence_index: EvidenceIndex,
        database_path: Path | None,
    ) -> dict[str, Any]:
        cold = analysis.cold_start
        if cold is None:
            return {
                "available": False,
                "reason": "当前问题类型没有冷启动边界。",
                "duration_ms": 0.0,
                "max_depth": 0,
                "source_evidence_id": None,
                "cpu_distribution": [],
                "lanes": [],
                "boundaries": [],
                "links": [],
            }

        start_ns = cold.start_boundary.timestamp_ns
        app_end_ns = cold.end_boundary.timestamp_ns
        presentation_ns = (
            cold.presentation_boundary.timestamp_ns
            if cold.presentation_boundary is not None
            else None
        )
        end_ns = max(app_end_ns, presentation_ns or app_end_ns)
        if end_ns <= start_ns:
            return {
                "available": False,
                "reason": "冷启动起止边界无效，无法绘制泳道。",
                "duration_ms": 0.0,
                "max_depth": 0,
                "source_evidence_id": None,
                "cpu_distribution": [],
                "lanes": [],
                "boundaries": [],
                "links": [],
            }

        timeline_record = evidence_index.cold_start_timeline(
            ipid=cold.resolved_process.ipid,
            start_ns=start_ns,
            end_ns=app_end_ns,
        )
        timeline_data = (
            timeline_record.data if timeline_record is not None else {}
        )
        target_process = timeline_data.get("target_process", {})
        target_ipid = target_process.get(
            "ipid",
            cold.resolved_process.ipid,
        )

        lanes: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        stage_items = [
            self._interval_item(
                item_id=f"stage-{index}",
                start_ns=stage.start_ns,
                end_ns=stage.end_ns,
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=stage.name,
                role=(
                    "critical"
                    if stage.duration_ms
                    == max(
                        (
                            candidate.duration_ms
                            for candidate in cold.stages
                        ),
                        default=-1,
                    )
                    else "stage"
                ),
                detail={
                    "type": "stage",
                    "duration_ms": stage.duration_ms,
                    "assessment": stage.assessment,
                    "evidence_ids": stage.evidence_ids,
                },
            )
            for index, stage in enumerate(cold.stages)
            if self._interval_overlaps(
                stage.start_ns,
                stage.end_ns,
                start_ns,
                end_ns,
            )
        ]
        stage_items = [item for item in stage_items if item is not None]
        if stage_items:
            lanes.append(
                {
                    "key": "stages",
                    "label": "启动阶段",
                    "sublabel": "Agent 选定",
                    "kind": "stage",
                    "height": 42,
                    "items": stage_items,
                }
            )

        cpu_running_lane = self._read_cpu_running_lane(
            database_path=database_path,
            target_itid=cold.resolved_process.main_itid,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        thread_state_lane = self._read_thread_state_lane(
            database_path=database_path,
            target_itid=cold.resolved_process.main_itid,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        if thread_state_lane is None:
            thread_state_lane = self._thread_state_lane(
                evidence=evidence_index.records,
                target_itid=cold.resolved_process.main_itid,
                start_ns=start_ns,
                end_ns=end_ns,
            )
        thread_scheduling_lane = self._merge_thread_scheduling_lanes(
            cpu_running_lane=cpu_running_lane,
            thread_state_lane=thread_state_lane,
        )
        if thread_scheduling_lane is not None:
            lanes.append(thread_scheduling_lane)

        detailed_main_lane = self._read_main_thread_slice_lane(
            database_path=database_path,
            target_itid=cold.resolved_process.main_itid,
            process_name=cold.resolved_process.name,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        if detailed_main_lane is not None:
            lanes.append(detailed_main_lane)

        dependency_lanes, dependency_links, dependencies = (
            self._build_cold_causal_dependency_lanes(
                cold=cold,
                database_path=database_path,
                start_ns=start_ns,
                end_ns=end_ns,
                main_scheduling_lane=thread_scheduling_lane,
            )
        )
        lanes.extend(dependency_lanes)
        links.extend(dependency_links)
        dependency_itids = {
            int(dependency["itid"]) for dependency in dependencies
        }

        grouped_events: dict[
            tuple[Any, Any, Any],
            list[dict[str, Any]],
        ] = defaultdict(list)
        for event in timeline_data.get("timeline_events", []):
            if event.get("source") != "callstack":
                continue
            if (
                detailed_main_lane is not None
                and event.get("itid")
                == cold.resolved_process.main_itid
            ):
                continue
            if event.get("itid") in dependency_itids:
                continue
            event_start = event.get("ts")
            event_end = event.get("end_ns")
            if not self._interval_overlaps(
                event_start,
                event_end,
                start_ns,
                end_ns,
            ):
                continue
            key = (
                event.get("ipid"),
                event.get("itid"),
                event.get("process_name") or "未知进程",
            )
            grouped_events[key].append(event)

        ranked_groups = sorted(
            grouped_events.items(),
            key=lambda pair: (
                0 if pair[0][0] == target_ipid else 1,
                -len(pair[1]),
                str(pair[0][2]),
            ),
        )[:6]
        for group_index, ((ipid, itid, process_name), events) in enumerate(
            ranked_groups
        ):
            thread_name = next(
                (
                    str(event.get("thread_name"))
                    for event in events
                    if event.get("thread_name")
                ),
                None,
            )
            items: list[dict[str, Any]] = []
            max_depth = 0
            for event in events[:80]:
                depth = event.get("depth")
                normalized_depth = (
                    min(max(int(depth), 0), 2)
                    if isinstance(depth, int)
                    else 0
                )
                max_depth = max(max_depth, normalized_depth)
                item = self._interval_item(
                    item_id=(
                        f"slice-{group_index}-"
                        f"{event.get('source_id', len(items))}"
                    ),
                    start_ns=event.get("ts"),
                    end_ns=event.get("end_ns"),
                    window_start_ns=start_ns,
                    window_end_ns=end_ns,
                    label=str(event.get("name") or "未命名 Slice"),
                    role=(
                        "target"
                        if ipid == target_ipid
                        else (
                            "cross"
                            if "target_package_name_match"
                            in event.get("candidate_reasons", [])
                            else "system"
                        )
                    ),
                    depth=normalized_depth,
                    detail={
                        "type": "slice",
                        **event,
                    },
                )
                if item is not None:
                    items.append(item)
            if not items:
                continue
            lanes.append(
                {
                    "key": f"process-{group_index}",
                    "label": (
                        cold.resolved_process.name
                        if ipid == target_ipid
                        else str(process_name)
                    ),
                    "sublabel": (
                        f"{thread_name or '线程'} · itid {itid}"
                    ),
                    "kind": (
                        "target" if ipid == target_ipid else "process"
                    ),
                    "height": 34 + max_depth * 18,
                    "items": items,
                }
            )

        frame_items: list[dict[str, Any]] = []
        frame_ids: set[int] = set()
        for frame in timeline_data.get("frame_candidates", [])[:120]:
            frame_id = frame.get("id")
            if not isinstance(frame_id, int):
                continue
            item = self._interval_item(
                item_id=f"frame-{frame_id}",
                start_ns=frame.get("ts"),
                end_ns=(
                    frame.get("end_ns")
                    if isinstance(frame.get("end_ns"), int)
                    else self._safe_add(
                        frame.get("ts"),
                        frame.get("dur"),
                    )
                ),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=(
                    "Actual Frame"
                    if frame.get("type_desc") == "actural"
                    else "Expected Frame"
                ),
                role=(
                    "frame-actual"
                    if frame.get("type_desc") == "actural"
                    else "frame-expected"
                ),
                detail={"type": "frame", **frame},
            )
            if item is not None:
                frame_items.append(item)
                frame_ids.add(frame_id)
        if frame_items:
            lanes.append(
                {
                    "key": "app-frames",
                    "label": "App Frame",
                    "sublabel": cold.resolved_process.name,
                    "kind": "frame",
                    "height": 38,
                    "items": frame_items,
                }
            )

        related_by_process: dict[str, list[dict[str, Any]]] = defaultdict(
            list
        )
        seen_related: set[tuple[str, int]] = set()
        for link in timeline_data.get("frame_links", [])[:120]:
            src_is_target = link.get("src_ipid") == target_ipid
            dst_is_target = link.get("dst_ipid") == target_ipid
            if src_is_target == dst_is_target:
                continue
            if src_is_target:
                app_row = link.get("src_row")
                related_row = link.get("dst_row")
                related_ts = link.get("dst_ts")
                related_dur = link.get("dst_dur_ns")
                related_process = (
                    link.get("dst_process_name") or "关联渲染进程"
                )
                related_type = link.get("dst_type_desc")
            else:
                app_row = link.get("dst_row")
                related_row = link.get("src_row")
                related_ts = link.get("src_ts")
                related_dur = link.get("src_dur_ns")
                related_process = (
                    link.get("src_process_name") or "关联渲染进程"
                )
                related_type = link.get("src_type_desc")
            if (
                not isinstance(app_row, int)
                or not isinstance(related_row, int)
                or app_row not in frame_ids
            ):
                continue
            related_key = (str(related_process), related_row)
            related_item_id = f"related-frame-{related_row}"
            if related_key not in seen_related:
                item = self._interval_item(
                    item_id=related_item_id,
                    start_ns=related_ts,
                    end_ns=self._safe_add(
                        related_ts,
                        related_dur,
                    ),
                    window_start_ns=start_ns,
                    window_end_ns=end_ns,
                    label=(
                        "RS Actual Frame"
                        if related_type == "actural"
                        else "RS Frame"
                    ),
                    role="render-frame",
                    detail={
                        "type": "related-frame",
                        "process_name": related_process,
                        "frame_row": related_row,
                        "timestamp_ns": related_ts,
                        "duration_ns": related_dur,
                        "map_id": link.get("map_id"),
                    },
                )
                if item is not None:
                    related_by_process[str(related_process)].append(item)
                    seen_related.add(related_key)
            if related_key in seen_related:
                links.append(
                    {
                        "source": f"frame-{app_row}",
                        "target": related_item_id,
                    }
                )
        for process_name, items in list(
            related_by_process.items()
        )[:3]:
            lanes.append(
                {
                    "key": f"related-{len(lanes)}",
                    "label": process_name,
                    "sublabel": "frame_maps 关联帧",
                    "kind": "render",
                    "height": 38,
                    "items": items,
                }
            )

        return {
            "available": bool(lanes),
            "reason": (
                ""
                if lanes
                else "没有可落入已选冷启动边界的时间线证据。"
            ),
            "start_ns": start_ns,
            "end_ns": end_ns,
            "title": "跨进程冷启动泳道",
            "subtitle": (
                "阶段、跨进程 Slice、App/Render 帧及可用线程状态"
                "共享同一时间轴。"
            ),
            "duration_ms": (end_ns - start_ns) / 1_000_000,
            "max_depth": max(
                (
                    int(lane.get("max_depth") or 0)
                    for lane in lanes
                ),
                default=0,
            ),
            "cpu_distribution": (
                thread_scheduling_lane.get("cpu_distribution", [])
                if thread_scheduling_lane is not None
                else []
            ),
            "lanes": lanes,
            "boundaries": [
                {
                    "label": "启动",
                    "position": 0,
                    "timestamp_ns": start_ns,
                    "source": cold.start_boundary.source,
                    "confidence": cold.start_boundary.confidence,
                },
                {
                    "label": (
                        cold.end_boundary.name
                        if cold.end_boundary.kind
                        in {
                            "application-defined-completion",
                            "application-marker-end",
                            "stable-home-frame",
                        }
                        else "应用首帧"
                    ),
                    "kind": "application",
                    "position": (
                        (app_end_ns - start_ns)
                        / (end_ns - start_ns)
                        * 100
                    ),
                    "timestamp_ns": app_end_ns,
                    "source": cold.end_boundary.source,
                    "confidence": cold.end_boundary.confidence,
                },
                *(
                    [
                        {
                            "label": "屏幕呈现",
                            "kind": "presentation",
                            "position": (
                                (cold.presentation_boundary.timestamp_ns - start_ns)
                                / (end_ns - start_ns)
                                * 100
                            ),
                            "timestamp_ns": cold.presentation_boundary.timestamp_ns,
                            "source": cold.presentation_boundary.source,
                            "confidence": (
                                cold.presentation_boundary.confidence
                            ),
                        }
                    ]
                    if cold.presentation_boundary is not None
                    else []
                ),
            ],
            "links": links[:80],
            "causal_dependencies": dependencies,
            "source_evidence_id": (
                timeline_record.evidence_id
                if timeline_record is not None
                else None
            ),
        }

    def _build_cold_causal_dependency_lanes(
        self,
        *,
        cold: Any,
        database_path: Path | None,
        start_ns: int,
        end_ns: int,
        main_scheduling_lane: dict[str, Any] | None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, str]],
        list[dict[str, Any]],
    ]:
        if database_path is None:
            return [], [], []
        root_itids = {
            itid
            for itid in [cold.resolved_process.main_itid]
            if isinstance(itid, int)
        }
        for stage in cold.stages:
            root_itids.update(
                thread.itid
                for thread in stage.critical_threads
                if isinstance(thread.itid, int)
            )
        try:
            dependencies = CausalThreadDependencyRepository(
                database_path
            ).select_significant(
                interval_start_ns=start_ns,
                interval_end_ns=end_ns,
                root_itids=root_itids,
                max_dependencies=3,
            )
        except (FileNotFoundError, ValueError):
            return [], [], []

        lanes: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        for dependency_index, dependency in enumerate(dependencies):
            dependency_itid = int(dependency["itid"])
            dependency_key = f"cold-causal-{dependency_itid}"
            dependency_label = str(
                dependency.get("thread_name") or f"itid {dependency_itid}"
            )
            impact_label = (
                f"显著依赖 · 阻塞贡献 {dependency['total_wait_ms']:.1f} ms"
                f" ({dependency['wait_share']:.1%})"
            )
            scheduling_lane = self._merge_thread_scheduling_lanes(
                cpu_running_lane=self._read_cpu_running_lane(
                    database_path=database_path,
                    target_itid=dependency_itid,
                    start_ns=start_ns,
                    end_ns=end_ns,
                ),
                thread_state_lane=self._read_thread_state_lane(
                    database_path=database_path,
                    target_itid=dependency_itid,
                    start_ns=start_ns,
                    end_ns=end_ns,
                ),
            )
            if scheduling_lane is not None:
                scheduling_lane.update(
                    {
                        "key": f"{dependency_key}-scheduling",
                        "label": f"{dependency_label} · Scheduling",
                        "sublabel": impact_label,
                        "kind": "causal",
                        "causal_dependency": dependency,
                    }
                )
            callstack_lane = self._read_main_thread_slice_lane(
                database_path=database_path,
                target_itid=dependency_itid,
                process_name=str(dependency.get("process_name") or "unknown"),
                start_ns=start_ns,
                end_ns=end_ns,
                thread_name=dependency_label,
                role_label="显著依赖",
                lane_key=f"{dependency_key}-callstack",
                item_id_prefix=f"causal-{dependency_itid}-slice",
            )
            if callstack_lane is not None:
                callstack_lane.update(
                    {
                        "sublabel": (
                            f"{impact_label} · {callstack_lane['sublabel']}"
                        ),
                        "kind": "causal",
                        "causal_dependency": dependency,
                    }
                )

            marker_lane = scheduling_lane or callstack_lane
            for wait_index, wait in enumerate(dependency.get("waits", [])):
                wakeup_ns = wait.get("wakeup_ns")
                if not isinstance(wakeup_ns, int):
                    continue
                marker_start = max(start_ns, wakeup_ns - 1)
                marker_end = min(end_ns, wakeup_ns + 1)
                if marker_end <= marker_start:
                    continue
                source_id = (
                    f"causal-wakeup-source-{dependency_index}-{wait_index}"
                )
                target_id = (
                    f"causal-wakeup-target-{dependency_index}-{wait_index}"
                )
                marker_detail = {
                    "type": "causal-wakeup",
                    "dependency_itid": dependency_itid,
                    "waiting_itid": wait.get("waiting_itid"),
                    "wait_duration_ms": (
                        float(wait.get("wait_duration_ns") or 0)
                        / 1_000_000.0
                    ),
                    "waiting_slice": wait.get("waiting_slice"),
                    "waker_slice": wait.get("waker_slice"),
                    "confidence": wait.get("confidence"),
                }
                source_item = self._interval_item(
                    item_id=source_id,
                    start_ns=marker_start,
                    end_ns=marker_end,
                    window_start_ns=start_ns,
                    window_end_ns=end_ns,
                    label="Wakeup",
                    role="wakeup-source",
                    detail=marker_detail,
                )
                if marker_lane is not None and source_item is not None:
                    marker_lane["items"].append(source_item)
                if (
                    main_scheduling_lane is not None
                    and wait.get("waiting_itid")
                    == cold.resolved_process.main_itid
                ):
                    target_item = self._interval_item(
                        item_id=target_id,
                        start_ns=marker_start,
                        end_ns=marker_end,
                        window_start_ns=start_ns,
                        window_end_ns=end_ns,
                        label="被唤醒",
                        role="wakeup-target",
                        detail=marker_detail,
                    )
                    if target_item is not None:
                        main_scheduling_lane["items"].append(target_item)
                        if marker_lane is not None and source_item is not None:
                            links.append({"source": source_id, "target": target_id})
            if scheduling_lane is not None:
                lanes.append(scheduling_lane)
            if callstack_lane is not None:
                lanes.append(callstack_lane)
        return lanes, links, dependencies

    @staticmethod
    def _completion_timeline_thread_specs(
        completion: Any,
    ) -> list[dict[str, Any]]:
        grouped: dict[int, dict[str, Any]] = {}
        target_ipid = completion.resolved_process.ipid

        def add(
            *,
            itid: int | None,
            name: str | None,
            process_name: str | None,
            role: str,
            reason: str,
            order: int,
        ) -> None:
            if itid is None:
                return
            item = grouped.setdefault(
                itid,
                {
                    "itid": itid,
                    "thread_name": name or f"itid {itid}",
                    "process_name": process_name or "unknown",
                    "roles": [],
                    "reasons": [],
                    "running_ms": 0.0,
                    "order": order,
                },
            )
            if role not in item["roles"]:
                item["roles"].append(role)
            if reason not in item["reasons"]:
                item["reasons"].append(reason)
            item["order"] = min(item["order"], order)

        main_itid = completion.resolved_process.main_itid
        add(
            itid=main_itid,
            name=completion.resolved_process.name,
            process_name=completion.resolved_process.name,
            role="进程主线程",
            reason="主线程始终保留，不假定其等同于 UI 线程",
            order=0,
        )

        profiles: dict[int, Any] = {}
        phase_profiles: list[tuple[str, Any]] = []
        for phase in completion.phases:
            for thread in phase.critical_threads:
                phase_profiles.append((phase.name, thread))
                profiles.setdefault(thread.itid, thread)
                item = grouped.setdefault(
                    thread.itid,
                    {
                        "itid": thread.itid,
                        "thread_name": (
                            thread.thread_name
                            or completion.resolved_process.name
                        ),
                        "process_name": thread.process_name,
                        "roles": [],
                        "reasons": [],
                        "running_ms": 0.0,
                        "order": 3,
                    },
                )
                item["running_ms"] += thread.state_breakdown.running_ms

        ui_tokens = (
            "useragent",
            "qtmainthread",
            "flutterui",
            "flutter ui",
            "dart ui",
            "ui thread",
            "uithread",
            "qsg render",
            "raster",
        )
        ui_itids: set[int] = set()
        for itid, thread in profiles.items():
            if thread.ipid != target_ipid:
                continue
            lowered = str(thread.thread_name or "").lower()
            if any(token in lowered for token in ui_tokens):
                ui_itids.add(itid)
                add(
                    itid=itid,
                    name=thread.thread_name,
                    process_name=thread.process_name,
                    role="UI / 事件循环候选",
                    reason="框架线程名与关键阶段线程 Evidence 匹配",
                    order=1,
                )

        causal_roots = {value for value in (main_itid, *ui_itids) if value}
        for phase_name, thread in phase_profiles:
            if thread.itid not in causal_roots:
                continue
            for hop in thread.wakeup_chain:
                if hop.waker_itid is None:
                    continue
                is_direct = hop.depth == 1
                is_application_upstream = (
                    hop.waker_process == completion.resolved_process.name
                )
                if not is_direct and not is_application_upstream:
                    continue
                add(
                    itid=hop.waker_itid,
                    name=hop.waker_thread,
                    process_name=hop.waker_process,
                    role=(
                        "直接唤醒 / 依赖线程"
                        if is_direct
                        else "应用内上游依赖线程"
                    ),
                    reason=(
                        f"{phase_name} 阶段 Wakeup Chain depth "
                        f"{hop.depth} 关联 itid {thread.itid}"
                    ),
                    order=2,
                )

        for thread in profiles.values():
            if thread.ipid == target_ipid:
                continue
            process_name = str(thread.process_name or "").lower()
            if "render_service" not in process_name:
                continue
            add(
                itid=thread.itid,
                name=thread.thread_name,
                process_name=thread.process_name,
                role="目标帧映射 RenderService",
                reason="完成阶段工具仅通过 frame_maps 加入 RenderService 线程",
                order=3,
            )

        values = [item for item in grouped.values() if item["roles"]]
        values.sort(
            key=lambda item: (
                item["order"],
                -item["running_ms"],
                item["itid"],
            )
        )
        return values[:8]

    @staticmethod
    def _completion_cpu_supplement_specs(
        *,
        completion: Any,
        database_path: Path | None,
        required_specs: list[dict[str, Any]],
        observation_end_ns: int | None = None,
        max_supplements: int = 2,
        max_total: int = 8,
    ) -> list[dict[str, Any]]:
        """Fill remaining lanes with high-Running application threads."""

        if (
            database_path is None
            or completion.resolved_process.ipid is None
            or max_supplements <= 0
            or len(required_specs) >= max_total
        ):
            return required_specs[:max_total]
        start_ns = completion.input_boundary.timestamp_ns
        terminal = (
            completion.completion_boundary
            or completion.response_boundary
        )
        terminal_ns = (
            terminal.timestamp_ns
            if terminal is not None
            else observation_end_ns
        )
        if terminal_ns is None or terminal_ns <= start_ns:
            return required_specs[:max_total]
        end_ns = terminal_ns
        try:
            result = SQLiteTraceRepository(
                database_path,
                query_timeout_seconds=10,
                default_max_rows=32,
                hard_max_rows=64,
                max_result_bytes=512 * 1024,
            ).query(
                "SELECT t.itid, t.tid, t.name AS thread_name, "
                "p.name AS process_name, "
                "SUM(MIN(s.ts + s.dur, ?) - MAX(s.ts, ?)) "
                "AS running_ns "
                "FROM sched_slice AS s "
                "JOIN thread AS t ON t.itid = s.itid "
                "JOIN process AS p ON p.ipid = t.ipid "
                "WHERE t.ipid = ? AND s.dur > 0 "
                "AND s.ts < ? AND s.ts + s.dur > ? "
                "GROUP BY t.itid, t.tid, t.name, p.name "
                "ORDER BY running_ns DESC, t.itid LIMIT 32",
                [
                    end_ns,
                    start_ns,
                    completion.resolved_process.ipid,
                    end_ns,
                    start_ns,
                ],
                max_rows=32,
            )
        except (FileNotFoundError, TraceQueryError):
            return required_specs[:max_total]

        selected_itids = {item["itid"] for item in required_specs}
        supplements: list[dict[str, Any]] = []
        for rank, row in enumerate(result.rows, start=1):
            itid = row.get("itid")
            running_ns = row.get("running_ns")
            if (
                not isinstance(itid, int)
                or itid in selected_itids
                or not isinstance(running_ns, int | float)
                or running_ns <= 0
            ):
                continue
            running_ms = float(running_ns) / 1_000_000.0
            supplements.append(
                {
                    "itid": itid,
                    "thread_name": row.get("thread_name") or f"itid {itid}",
                    "process_name": row.get("process_name") or "unknown",
                    "roles": ["CPU 高贡献补选"],
                    "reasons": [
                        f"问题区间应用线程 Running 排名 #{rank}，"
                        f"累计 {running_ms:.3f}ms；仅表示负载贡献，不定义线程角色"
                    ],
                    "running_ms": running_ms,
                    "order": 4,
                }
            )
            selected_itids.add(itid)
            if (
                len(supplements) >= max_supplements
                or len(required_specs) + len(supplements) >= max_total
            ):
                break
        return [*required_specs, *supplements]

    def _build_completion_timeline(
        self,
        *,
        analysis: AnalysisResult,
        evidence_index: EvidenceIndex,
        database_path: Path | None,
    ) -> dict[str, Any]:
        completion = analysis.completion_latency
        if completion is None:
            return {
                "available": False,
                "reason": "当前问题类型没有完成时延边界。",
                "title": "完成时延关键路径泳道",
                "subtitle": "当前没有可绘制的完成时延结果。",
                "duration_ms": 0.0,
                "max_depth": 0,
                "source_evidence_id": None,
                "cpu_distribution": [],
                "lanes": [],
                "boundaries": [],
                "links": [],
            }

        start_ns = completion.input_boundary.timestamp_ns
        final_boundary = (
            completion.completion_boundary
            if completion.completion_boundary is not None
            else completion.response_boundary
        )
        observation_only = final_boundary is None
        observation_end_ns = (
            analysis.problem_interval.end_boundary.timestamp_ns
            if observation_only
            and analysis.problem_interval is not None
            and analysis.problem_interval.start_boundary.timestamp_ns
            == start_ns
            else None
        )
        end_ns = (
            final_boundary.timestamp_ns
            if final_boundary is not None
            else observation_end_ns
        )
        if end_ns is None or end_ns <= start_ns:
            return {
                "available": False,
                "reason": "完成时延尚无可绘制的响应或完成边界。",
                "title": "完成时延关键路径泳道",
                "subtitle": "未观察到响应或完成，且缺少有效观察窗口。",
                "duration_ms": 0.0,
                "max_depth": 0,
                "source_evidence_id": None,
                "cpu_distribution": [],
                "lanes": [],
                "boundaries": [],
                "links": [],
            }
        phase_record = evidence_index.completion_phases(
            ipid=completion.resolved_process.ipid,
            exact_boundaries=False,
        )
        candidate_record = evidence_index.completion_candidate(
            ipid=completion.resolved_process.ipid,
        )
        candidate_data = (
            candidate_record.data if candidate_record is not None else {}
        )

        lanes: list[dict[str, Any]] = []
        longest_phase_ms = max(
            (phase.duration_ms for phase in completion.phases),
            default=-1.0,
        )
        phase_items = [
            self._interval_item(
                item_id=f"latency-phase-{index}",
                start_ns=phase.start_ns,
                end_ns=phase.end_ns,
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=phase.name,
                role=(
                    "critical"
                    if phase.duration_ms == longest_phase_ms
                    else "stage"
                ),
                detail={
                    "type": "latency-phase",
                    "duration_ms": phase.duration_ms,
                    "assessment": phase.assessment,
                    "evidence_ids": phase.evidence_ids,
                },
            )
            for index, phase in enumerate(completion.phases)
            if self._interval_overlaps(
                phase.start_ns,
                phase.end_ns,
                start_ns,
                end_ns,
            )
        ]
        phase_items = [item for item in phase_items if item is not None]
        if phase_items:
            lanes.append(
                {
                    "key": "latency-phases",
                    "label": "时延阶段",
                    "sublabel": "输入 → 响应 → 完成",
                    "kind": "stage",
                    "height": 42,
                    "items": phase_items,
                }
            )

        timeline_threads = self._completion_timeline_thread_specs(completion)
        timeline_threads = self._completion_cpu_supplement_specs(
            completion=completion,
            database_path=database_path,
            required_specs=timeline_threads,
            observation_end_ns=end_ns,
        )
        thread_scheduling_lane: dict[str, Any] | None = None
        for thread_spec in timeline_threads:
            thread_itid = thread_spec["itid"]
            cpu_lane = self._read_cpu_running_lane(
                database_path=database_path,
                target_itid=thread_itid,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            state_lane = self._read_thread_state_lane(
                database_path=database_path,
                target_itid=thread_itid,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            scheduling_lane = self._merge_thread_scheduling_lanes(
                cpu_running_lane=cpu_lane,
                thread_state_lane=state_lane,
            )
            role_label = " / ".join(thread_spec["roles"])
            reason_label = "；".join(thread_spec["reasons"])
            if scheduling_lane is not None:
                scheduling_lane["key"] = (
                    f"thread-{thread_itid}-scheduling"
                )
                scheduling_lane["label"] = (
                    f"{thread_spec['thread_name']} · CPU / State"
                )
                scheduling_lane["sublabel"] = (
                    f"{role_label} · {scheduling_lane['sublabel']} · "
                    f"{reason_label}"
                )
                lanes.append(scheduling_lane)
                if (
                    thread_scheduling_lane is None
                    or "UI / 事件循环候选" in thread_spec["roles"]
                ):
                    thread_scheduling_lane = scheduling_lane
            slice_lane = self._read_main_thread_slice_lane(
                database_path=database_path,
                target_itid=thread_itid,
                process_name=thread_spec["process_name"],
                thread_name=thread_spec["thread_name"],
                role_label=role_label,
                lane_key=f"thread-{thread_itid}-callstack",
                start_ns=start_ns,
                end_ns=end_ns,
            )
            if slice_lane is not None:
                slice_lane["sublabel"] += f" · {reason_label}"
                lanes.append(slice_lane)

        frame_items: list[dict[str, Any]] = []
        frame_ids: set[int] = set()
        for frame in candidate_data.get("app_frames", [])[:240]:
            frame_id = frame.get("id")
            if not isinstance(frame_id, int):
                continue
            item = self._interval_item(
                item_id=f"frame-{frame_id}",
                start_ns=frame.get("ts"),
                end_ns=(
                    frame.get("end_ns")
                    if isinstance(frame.get("end_ns"), int)
                    else self._safe_add(frame.get("ts"), frame.get("dur"))
                ),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=(
                    "Actual Frame"
                    if frame.get("type_desc") == "actural"
                    else "Expected Frame"
                ),
                role=(
                    "frame-actual"
                    if frame.get("type_desc") == "actural"
                    else "frame-expected"
                ),
                detail={"type": "frame", **frame},
            )
            if item is not None:
                frame_items.append(item)
                frame_ids.add(frame_id)
        if frame_items:
            lanes.append(
                {
                    "key": "app-frames",
                    "label": "App Frame",
                    "sublabel": completion.resolved_process.name,
                    "kind": "frame",
                    "height": 38,
                    "items": frame_items,
                }
            )

        render_items: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        seen_render_ids: set[int] = set()
        render_process_name = "RenderService"
        for link in candidate_data.get("frame_links", [])[:240]:
            app_row = link.get("src_row")
            render_row = link.get("dst_row")
            if (
                not isinstance(app_row, int)
                or not isinstance(render_row, int)
                or app_row not in frame_ids
            ):
                continue
            render_item_id = f"render-frame-{render_row}"
            if render_row not in seen_render_ids:
                render_process_name = str(
                    link.get("dst_process_name") or render_process_name
                )
                item = self._interval_item(
                    item_id=render_item_id,
                    start_ns=link.get("dst_ts"),
                    end_ns=(
                        link.get("dst_end_ns")
                        if isinstance(link.get("dst_end_ns"), int)
                        else self._safe_add(
                            link.get("dst_ts"), link.get("dst_dur_ns")
                        )
                    ),
                    window_start_ns=start_ns,
                    window_end_ns=end_ns,
                    label="RS Frame",
                    role="render-frame",
                    detail={"type": "related-frame", **link},
                )
                if item is not None:
                    render_items.append(item)
                    seen_render_ids.add(render_row)
            if render_row in seen_render_ids:
                links.append(
                    {
                        "source": f"frame-{app_row}",
                        "target": render_item_id,
                    }
                )
        if render_items:
            lanes.append(
                {
                    "key": "render-frames",
                    "label": render_process_name,
                    "sublabel": "frame_maps 关联帧",
                    "kind": "render",
                    "height": 38,
                    "items": render_items,
                }
            )

        boundaries = [
            {
                "label": "输入",
                "kind": "input",
                "position": 0,
                "timestamp_ns": start_ns,
                "source": completion.input_boundary.source,
                "confidence": completion.input_boundary.confidence,
            }
        ]
        if completion.response_boundary is not None:
            boundaries.append(
                {
                    "label": "首个响应",
                    "kind": "response",
                    "position": (
                        (completion.response_boundary.timestamp_ns - start_ns)
                        / (end_ns - start_ns)
                        * 100
                    ),
                    "timestamp_ns": completion.response_boundary.timestamp_ns,
                    "source": completion.response_boundary.source,
                    "confidence": completion.response_boundary.confidence,
                }
            )
        if completion.completion_boundary is not None:
            boundaries.append(
                {
                    "label": "完成",
                    "kind": "completion",
                    "position": 100,
                    "timestamp_ns": completion.completion_boundary.timestamp_ns,
                    "source": completion.completion_boundary.source,
                    "confidence": completion.completion_boundary.confidence,
                }
            )
        elif observation_only:
            boundaries.append(
                {
                    "label": "Trace 观察结束（未完成）",
                    "kind": "observation-end",
                    "position": 100,
                    "timestamp_ns": end_ns,
                    "source": (
                        analysis.problem_interval.end_boundary.source
                        if analysis.problem_interval is not None
                        else "problem-interval"
                    ),
                    "confidence": 1.0,
                }
            )

        return {
            "available": bool(lanes),
            "reason": (
                "" if lanes else "没有可落入所选完成时延边界的时间线证据。"
            ),
            "title": (
                "未完成操作观察窗口泳道"
                if observation_only
                else "完成时延关键路径泳道"
            ),
            "subtitle": (
                (
                    "输入后直到 Trace 结束仍未观察到有效完成；"
                    "终点仅为观察结束，不计作响应或业务完成。"
                    if observation_only
                    else "输入、首个响应、业务完成、关键应用线程、进程主线程上下文与 "
                    "App/Render 帧共享同一时间轴。"
                )
            ),
            "start_ns": start_ns,
            "end_ns": end_ns,
            "duration_ms": (end_ns - start_ns) / 1_000_000,
            "max_depth": max(
                (int(lane.get("max_depth") or 0) for lane in lanes),
                default=0,
            ),
            "cpu_distribution": (
                thread_scheduling_lane.get("cpu_distribution", [])
                if thread_scheduling_lane is not None
                else []
            ),
            "lanes": lanes,
            "boundaries": boundaries,
            "links": links[:160],
            "source_evidence_id": (
                phase_record.evidence_id
                if phase_record is not None
                else (
                    candidate_record.evidence_id
                    if candidate_record is not None
                    else None
                )
            ),
        }

    def _read_main_thread_slice_lane(
        self,
        *,
        database_path: Path | None,
        target_itid: int | None,
        process_name: str,
        start_ns: int,
        end_ns: int,
        thread_name: str | None = None,
        role_label: str = "Main",
        lane_key: str = "main-thread-callstack",
        item_id_prefix: str = "main-slice",
    ) -> dict[str, Any] | None:
        if database_path is None or target_itid is None:
            return None
        display_limit = 10_000
        try:
            repository = SQLiteTraceRepository(
                database_path,
                query_timeout_seconds=10,
                default_max_rows=display_limit,
                hard_max_rows=display_limit,
                max_result_bytes=16 * 1024 * 1024,
            )
            count_result = repository.query(
                "SELECT COUNT(*) AS event_count FROM callstack "
                "WHERE callid = ? AND dur > 0 "
                "AND ts < ? AND ts + dur > ? "
                "AND depth BETWEEN 0 AND 32 "
                "AND NOT (depth = 0 AND parent_id = ? AND child_callid = ?)",
                [
                    target_itid,
                    end_ns,
                    start_ns,
                    target_itid,
                    target_itid,
                ],
                max_rows=1,
            )
            total_event_count = int(
                (count_result.rows[0] if count_result.rows else {}).get(
                    "event_count"
                )
                or 0
            )
            downsampled = total_event_count > display_limit
            if downsampled:
                result = repository.query(
                    "WITH scoped AS ("
                    "SELECT id, ts, dur, ts + dur AS end_ns, name, cat, "
                    "depth, parent_id, child_callid, "
                    "CASE WHEN ts <= ? THEN 0 ELSE MIN(63, CAST("
                    "(ts - ?) * 64 / ? AS INTEGER)) END AS time_bucket "
                    "FROM callstack WHERE callid = ? AND dur > 0 "
                    "AND ts < ? AND ts + dur > ? "
                    "AND depth BETWEEN 0 AND 32 "
                    "AND NOT (depth = 0 AND parent_id = ? "
                    "AND child_callid = ?)), "
                    "ranked AS (SELECT *, ROW_NUMBER() OVER ("
                    "PARTITION BY time_bucket, depth "
                    "ORDER BY dur DESC, id) AS bucket_depth_rank "
                    "FROM scoped) "
                    "SELECT id, ts, dur, end_ns, name, cat, depth, "
                    "parent_id, child_callid FROM ranked "
                    "WHERE bucket_depth_rank <= 4 "
                    "ORDER BY ts, depth, dur DESC, id LIMIT 10000",
                    [
                        start_ns,
                        start_ns,
                        max(1, end_ns - start_ns),
                        target_itid,
                        end_ns,
                        start_ns,
                        target_itid,
                        target_itid,
                    ],
                    max_rows=display_limit,
                )
            else:
                result = repository.query(
                    "SELECT id, ts, dur, ts + dur AS end_ns, name, cat, "
                    "depth, parent_id, child_callid "
                    "FROM callstack "
                    "WHERE callid = ? AND dur > 0 "
                    "AND ts < ? AND ts + dur > ? "
                    "AND depth BETWEEN 0 AND 32 "
                    "AND NOT (depth = 0 AND parent_id = ? "
                    "AND child_callid = ?) "
                    "ORDER BY ts, depth, dur DESC, id LIMIT 10000",
                    [
                        target_itid,
                        end_ns,
                        start_ns,
                        target_itid,
                        target_itid,
                    ],
                    max_rows=display_limit,
                )
        except (FileNotFoundError, TraceQueryError):
            return None

        items: list[dict[str, Any]] = []
        max_depth = 0
        for row in result.rows:
            event_id = row.get("id")
            depth = row.get("depth")
            if not isinstance(event_id, int):
                continue
            normalized_depth = (
                max(0, int(depth)) if isinstance(depth, int) else 0
            )
            max_depth = max(max_depth, normalized_depth)
            item = self._interval_item(
                item_id=f"{item_id_prefix}-{event_id}",
                start_ns=row.get("ts"),
                end_ns=row.get("end_ns"),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=str(row.get("name") or "Unnamed Slice"),
                role="target",
                depth=normalized_depth,
                detail={
                    "type": "main-thread-slice",
                    "source": "callstack",
                    "source_id": event_id,
                    "itid": target_itid,
                    **row,
                },
            )
            if item is not None:
                items.append(item)
        if not items:
            return None
        return {
            "key": lane_key,
            "label": f"{thread_name or process_name} · {role_label}",
            "sublabel": (
                f"itid {target_itid} · "
                + (
                    f"{len(items)} / {total_event_count} representative "
                    "slices · full-window balanced · "
                    if downsampled
                    else f"{len(items)} slices · "
                )
                + f"depth 0–{max_depth}"
            ),
            "kind": "target",
            "height": 42 + max_depth * 16,
            "max_depth": max_depth,
            "event_count": len(items),
            "truncated": result.truncated or downsampled,
            "downsampled": downsampled,
            "source_event_count": total_event_count,
            "sampling_strategy": (
                "time-bucket-depth-balanced" if downsampled else "complete"
            ),
            "items": items,
        }

    def _read_thread_state_lane(
        self,
        *,
        database_path: Path | None,
        target_itid: int | None,
        start_ns: int,
        end_ns: int,
    ) -> dict[str, Any] | None:
        if database_path is None or target_itid is None:
            return None
        try:
            result = SQLiteTraceRepository(
                database_path,
                query_timeout_seconds=10,
                default_max_rows=10_000,
                hard_max_rows=10_000,
                max_result_bytes=8 * 1024 * 1024,
            ).query(
                "SELECT id, ts, dur, ts + dur AS end_ns, cpu, "
                "itid, tid, pid, state "
                "FROM thread_state "
                "WHERE itid = ? AND dur > 0 "
                "AND ts < ? AND ts + dur > ? "
                "ORDER BY ts, id LIMIT 10000",
                [target_itid, end_ns, start_ns],
                max_rows=10_000,
            )
        except (FileNotFoundError, TraceQueryError):
            return None

        items: list[dict[str, Any]] = []
        state_counts: dict[str, int] = defaultdict(int)
        for row in result.rows:
            event_id = row.get("id")
            state = row.get("state")
            if not isinstance(event_id, int) or not isinstance(state, str):
                continue
            if state in {"S", "Sleep", "Sleeping"}:
                continue
            item = self._interval_item(
                item_id=f"thread-state-{event_id}",
                start_ns=row.get("ts"),
                end_ns=row.get("end_ns"),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=state,
                role=self._state_role(state),
                detail={
                    "type": "thread-state",
                    "source": "thread_state",
                    "source_id": event_id,
                    **row,
                },
            )
            if item is not None:
                items.append(item)
                state_counts[state] += 1
        if not items:
            return None
        state_summary = " / ".join(
            f"{state} {count}"
            for state, count in sorted(
                state_counts.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )[:4]
        )
        return {
            "key": "thread-state",
            "label": "Thread State",
            "sublabel": state_summary,
            "kind": "state",
            "height": 38,
            "event_count": len(items),
            "truncated": result.truncated,
            "items": items,
        }

    def _read_cpu_running_lane(
        self,
        *,
        database_path: Path | None,
        target_itid: int | None,
        start_ns: int,
        end_ns: int,
    ) -> dict[str, Any] | None:
        if database_path is None or target_itid is None:
            return None
        try:
            result = SQLiteTraceRepository(
                database_path,
                query_timeout_seconds=10,
                default_max_rows=10_000,
                hard_max_rows=10_000,
                max_result_bytes=8 * 1024 * 1024,
            ).query(
                "SELECT id, ts, dur, ts + dur AS end_ns, cpu, "
                "itid, ipid, priority, end_state "
                "FROM sched_slice "
                "WHERE itid = ? AND dur > 0 "
                "AND ts < ? AND ts + dur > ? "
                "ORDER BY ts, id LIMIT 10000",
                [target_itid, end_ns, start_ns],
                max_rows=10_000,
            )
        except (FileNotFoundError, TraceQueryError):
            return None

        items: list[dict[str, Any]] = []
        observed_cpus: set[int] = set()
        for row in result.rows:
            event_id = row.get("id")
            cpu = row.get("cpu")
            if not isinstance(event_id, int) or not isinstance(cpu, int):
                continue
            item = self._interval_item(
                item_id=f"cpu-running-{event_id}",
                start_ns=row.get("ts"),
                end_ns=row.get("end_ns"),
                window_start_ns=start_ns,
                window_end_ns=end_ns,
                label=f"CPU {cpu}",
                role="cpu-running",
                detail={
                    "type": "cpu-running",
                    "source": "sched_slice",
                    "source_id": event_id,
                    **row,
                },
            )
            if item is not None:
                items.append(item)
                observed_cpus.add(cpu)
        if not items:
            return None
        cpu_summary = ", ".join(f"CPU {cpu}" for cpu in sorted(observed_cpus))
        return {
            "key": "cpu-running",
            "label": "CPU Running",
            "sublabel": f"{len(items)} slices · {cpu_summary}",
            "kind": "cpu",
            "height": 38,
            "event_count": len(items),
            "truncated": result.truncated,
            "items": items,
        }

    @classmethod
    def _merge_thread_scheduling_lanes(
        cls,
        *,
        cpu_running_lane: dict[str, Any] | None,
        thread_state_lane: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        cpu_items = (
            list(cpu_running_lane.get("items", []))
            if cpu_running_lane is not None
            else []
        )
        state_items = (
            list(thread_state_lane.get("items", []))
            if thread_state_lane is not None
            else []
        )
        if cpu_items:
            state_items = [
                item
                for item in state_items
                if item.get("role") != "state-running"
            ]
        items = sorted(
            [*cpu_items, *state_items],
            key=lambda item: (
                float(item.get("start_ms") or 0),
                float(item.get("duration_ms") or 0),
            ),
        )
        if not items:
            return None

        cpu_ids = sorted(
            {
                cpu
                for item in cpu_items
                if isinstance((cpu := item.get("detail", {}).get("cpu")), int)
            }
        )
        cpu_label = ""
        if cpu_ids:
            if cpu_ids == list(range(cpu_ids[0], cpu_ids[-1] + 1)):
                cpu_label = f"CPU {cpu_ids[0]}–{cpu_ids[-1]}"
            else:
                cpu_label = ", ".join(f"CPU {cpu}" for cpu in cpu_ids)
        cpu_totals: dict[int, dict[str, float | int]] = defaultdict(
            lambda: {"running_ms": 0.0, "slices": 0}
        )
        for item in cpu_items:
            cpu = item.get("detail", {}).get("cpu")
            if not isinstance(cpu, int):
                continue
            cpu_totals[cpu]["running_ms"] = float(
                cpu_totals[cpu]["running_ms"]
            ) + float(item.get("duration_ms") or 0)
            cpu_totals[cpu]["slices"] = int(
                cpu_totals[cpu]["slices"]
            ) + 1
        total_running_ms = sum(
            float(stat["running_ms"]) for stat in cpu_totals.values()
        )
        cpu_distribution = [
            {
                "cpu": cpu,
                "running_ms": float(stat["running_ms"]),
                "share": (
                    float(stat["running_ms"]) / total_running_ms
                    if total_running_ms > 0
                    else 0
                ),
                "slices": int(stat["slices"]),
                "color": cls._CPU_PALETTE[
                    abs(cpu) % len(cls._CPU_PALETTE)
                ],
            }
            for cpu, stat in sorted(cpu_totals.items())
        ]
        state_counts: dict[str, int] = defaultdict(int)
        for item in state_items:
            state_counts[str(item.get("label") or "Other")] += 1
        state_label = " / ".join(
            f"{state} {count}"
            for state, count in sorted(
                state_counts.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )[:3]
        )
        sublabel_parts = [
            part
            for part in (
                f"{len(cpu_items)} running" if cpu_items else "",
                cpu_label,
                state_label,
            )
            if part
        ]
        return {
            "key": "thread-scheduling",
            "label": "Thread Scheduling",
            "sublabel": " · ".join(sublabel_parts),
            "kind": "cpu",
            "height": 38,
            "event_count": len(items),
            "cpu_distribution": cpu_distribution,
            "cpu_running_ms": total_running_ms,
            "truncated": bool(
                (cpu_running_lane or {}).get("truncated")
                or (thread_state_lane or {}).get("truncated")
            ),
            "items": items,
        }

    def _thread_state_lane(
        self,
        *,
        evidence: tuple[EvidenceRecord, ...],
        target_itid: int | None,
        start_ns: int,
        end_ns: int,
    ) -> dict[str, Any] | None:
        items: list[dict[str, Any]] = []
        for record in evidence:
            if record.tool != "query_trace_sql":
                continue
            data = record.data
            rows = data.get("rows")
            if not isinstance(rows, list):
                continue
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                state = row.get("state")
                ts = row.get("ts")
                duration = row.get("dur")
                if (
                    not isinstance(state, str)
                    or not isinstance(ts, int)
                    or not isinstance(duration, int)
                    or (
                        target_itid is not None
                        and isinstance(row.get("itid"), int)
                        and row["itid"] != target_itid
                    )
                ):
                    continue
                if state in {"S", "Sleep", "Sleeping"}:
                    continue
                item = self._interval_item(
                    item_id=(
                        f"state-{record.evidence_id}-{row_index}"
                    ),
                    start_ns=ts,
                    end_ns=ts + duration,
                    window_start_ns=start_ns,
                    window_end_ns=end_ns,
                    label=state,
                    role=self._state_role(state),
                    detail={
                        "type": "thread-state",
                        "evidence_id": record.evidence_id,
                        **row,
                    },
                )
                if item is not None:
                    items.append(item)
            if items:
                break
        if not items:
            return None
        return {
            "key": "thread-state",
            "label": "Thread State",
            "sublabel": "主线程原始状态",
            "kind": "state",
            "height": 38,
            "items": items[:160],
        }

    @staticmethod
    def _interval_item(
        *,
        item_id: str,
        start_ns: Any,
        end_ns: Any,
        window_start_ns: int,
        window_end_ns: int,
        label: str,
        role: str,
        detail: dict[str, Any],
        depth: int = 0,
    ) -> dict[str, Any] | None:
        if not isinstance(start_ns, int) or not isinstance(end_ns, int):
            return None
        clipped_start = max(start_ns, window_start_ns)
        clipped_end = min(end_ns, window_end_ns)
        if clipped_end <= clipped_start:
            return None
        window_duration = window_end_ns - window_start_ns
        return {
            "id": item_id,
            "label": label,
            "role": role,
            "depth": depth,
            "left": (
                (clipped_start - window_start_ns)
                / window_duration
                * 100
            ),
            "width": max(
                (clipped_end - clipped_start)
                / window_duration
                * 100,
                0.25,
            ),
            "start_ms": (
                start_ns - window_start_ns
            )
            / 1_000_000,
            "duration_ms": (end_ns - start_ns) / 1_000_000,
            "continued_before": start_ns < window_start_ns,
            "continued_after": end_ns > window_end_ns,
            "detail": detail,
        }

    @staticmethod
    def _interval_overlaps(
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

    @staticmethod
    def _safe_add(left: Any, right: Any) -> int | None:
        if isinstance(left, int) and isinstance(right, int):
            return left + max(right, 1)
        return None

    @staticmethod
    def _state_role(state: str) -> str:
        if state == "Running":
            return "state-running"
        if state in {"R", "R+"}:
            return "state-runnable"
        if state == "S":
            return "state-sleep"
        if state.startswith("D"):
            return "state-blocked"
        return "state-other"

    @staticmethod
    def _confidence_label(confidence: float) -> str:
        if confidence >= 0.85:
            return "高置信"
        if confidence >= 0.65:
            return "中等置信"
        return "低置信"
