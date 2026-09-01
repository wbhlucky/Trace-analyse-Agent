from __future__ import annotations

from trace_agent.evidence import EvidenceIndex
from trace_agent.models import (
    AnalysisResult,
    ColdStartAnalysis,
    EvidenceRecord,
    TraceBoundary,
)


class ColdStartMetricNormalizer:
    """Apply the platform metric only when no app-specific contract won."""

    _APPLICATION_DEFINED_KINDS = frozenset(
        {
            "application-defined-start",
            "application-marker-start",
            "application-defined-completion",
            "application-marker-end",
            "stable-home-frame",
        }
    )

    def normalize(
        self,
        analysis: AnalysisResult,
        evidence: list[EvidenceRecord],
    ) -> AnalysisResult:
        cold = analysis.cold_start
        if cold is None:
            return analysis
        application_defined_contract = (
            cold.start_boundary.kind in self._APPLICATION_DEFINED_KINDS
            or cold.end_boundary.kind in self._APPLICATION_DEFINED_KINDS
        )
        if application_defined_contract:
            record = self._timeline_record(cold, evidence)
            self._normalize_application_defined_metrics(analysis, record)
            return analysis
        record = self._timeline_record(cold, evidence)
        if record is None:
            return analysis
        boundary_evidence = record.data.get("boundary_evidence")
        if not isinstance(boundary_evidence, dict):
            return analysis
        metric = boundary_evidence.get("metric_candidate")
        if (
            not isinstance(metric, dict)
            or metric.get("status") != "proven_platform_boundary_pair"
        ):
            return analysis

        start = metric.get("start")
        application = metric.get("application_complete")
        presentation = metric.get("presentation_complete")
        if not isinstance(start, dict) or not isinstance(application, dict):
            return analysis
        start_ns = start.get("ts")
        app_end_ns = application.get("timestamp_ns")
        if (
            not isinstance(start_ns, int)
            or not isinstance(app_end_ns, int)
            or app_end_ns <= start_ns
        ):
            return analysis

        previous_start_ns = cold.start_boundary.timestamp_ns
        previous_end_ns = cold.end_boundary.timestamp_ns
        evidence_ids = self._merge_evidence_ids(
            cold.start_boundary.evidence_ids,
            record.evidence_id,
        )
        cold.start_boundary = TraceBoundary(
            name=str(start.get("name") or "platform launch marker"),
            timestamp_ns=start_ns,
            source=str(start.get("source") or "callstack"),
            source_id=self._optional_string(start.get("source_id")),
            kind="platform-launch",
            confidence=max(cold.start_boundary.confidence, 0.95),
            evidence_ids=evidence_ids,
        )
        cold.end_boundary = TraceBoundary(
            name=str(
                application.get("name")
                or "main-thread application first-frame completion"
            ),
            timestamp_ns=app_end_ns,
            source=str(application.get("source") or "callstack"),
            source_id=self._optional_string(
                application.get("source_id")
            ),
            kind="application-first-frame",
            confidence=max(cold.end_boundary.confidence, 0.95),
            evidence_ids=self._merge_evidence_ids(
                cold.end_boundary.evidence_ids,
                record.evidence_id,
            ),
        )
        cold.total_duration_ms = (app_end_ns - start_ns) / 1_000_000.0

        if isinstance(presentation, dict):
            presentation_ns = presentation.get("timestamp_ns")
            if (
                isinstance(presentation_ns, int)
                and presentation_ns >= app_end_ns
            ):
                old_presentation = cold.presentation_boundary
                cold.presentation_boundary = TraceBoundary(
                    name=str(
                        presentation.get("name")
                        or "mapped render-service frame completion"
                    ),
                    timestamp_ns=presentation_ns,
                    source=str(
                        presentation.get("source") or "frame_slice"
                    ),
                    source_id=self._optional_string(
                        presentation.get("source_id")
                    ),
                    kind="presentation",
                    confidence=max(
                        (
                            old_presentation.confidence
                            if old_presentation is not None
                            else 0.0
                        ),
                        0.95,
                    ),
                    evidence_ids=self._merge_evidence_ids(
                        (
                            old_presentation.evidence_ids
                            if old_presentation is not None
                            else []
                        ),
                        record.evidence_id,
                    ),
                )
                cold.presentation_duration_ms = (
                    presentation_ns - start_ns
                ) / 1_000_000.0

        normalized_stages = []
        clipped_stage_names: list[str] = []
        for stage in cold.stages:
            next_start = max(stage.start_ns, start_ns)
            next_end = min(stage.end_ns, app_end_ns)
            if next_end <= next_start:
                clipped_stage_names.append(stage.name)
                continue
            boundaries_changed = (
                next_start != stage.start_ns or next_end != stage.end_ns
            )
            if boundaries_changed:
                if stage.critical_threads:
                    stage.critical_threads = []
                clipped_stage_names.append(stage.name)
            stage.start_ns = next_start
            stage.end_ns = next_end
            stage.duration_ms = (
                next_end - next_start
            ) / 1_000_000.0
            normalized_stages.append(stage)
        cold.stages = normalized_stages

        if previous_start_ns != start_ns or previous_end_ns != app_end_ns:
            standard_summary = (
                "标准冷启动口径（AppSpawn→应用首帧）为 "
                f"{cold.total_duration_ms:.3f}ms"
            )
            if cold.presentation_duration_ms is not None:
                standard_summary += (
                    "，到屏幕呈现完成为 "
                    f"{cold.presentation_duration_ms:.3f}ms"
                )
            standard_summary += "。"
            if not analysis.summary.startswith("标准冷启动口径"):
                analysis.summary = standard_summary + analysis.summary
            if not cold.critical_path_summary.startswith(
                "标准冷启动口径"
            ):
                cold.critical_path_summary = (
                    standard_summary + cold.critical_path_summary
                )
            note = (
                "标准冷启动指标已按工具证明的平台边界规范化："
                f"{start_ns} → {app_end_ns}；Agent 选择的其他时间点可作为"
                "独立候选，但不覆盖主指标。"
            )
            if clipped_stage_names:
                note += " 已裁剪越界阶段：" + "、".join(clipped_stage_names)
            if note not in analysis.limitations:
                analysis.limitations.append(note)
        return analysis

    def _normalize_application_defined_metrics(
        self,
        analysis: AnalysisResult,
        timeline_record: EvidenceRecord | None,
    ) -> None:
        """Normalize derived values without replacing app-defined boundaries.

        A stable-home or application-marker completion can occur after the
        standardized technical first frame.  The technical presentation metric
        remains useful in that case, but its duration and all stages still need
        to use the selected cold-start start boundary consistently.
        """

        cold = analysis.cold_start
        assert cold is not None
        start_ns = cold.start_boundary.timestamp_ns
        interval = analysis.problem_interval
        if (
            interval is not None
            and interval.end_boundary.source == "explicit_time_range"
            and cold.end_boundary.kind in self._APPLICATION_DEFINED_KINDS
        ):
            explicit_end_ns = interval.end_boundary.timestamp_ns
            if explicit_end_ns > start_ns:
                previous_end = cold.end_boundary
                cold.end_boundary = TraceBoundary(
                    name=previous_end.name,
                    timestamp_ns=explicit_end_ns,
                    source="explicit_time_range",
                    source_id="time_range",
                    kind=previous_end.kind,
                    confidence=1.0,
                    evidence_ids=list(
                        dict.fromkeys(
                            [
                                *previous_end.evidence_ids,
                                *interval.end_boundary.evidence_ids,
                            ]
                        )
                    ),
                )

        self._apply_proven_presentation(cold, timeline_record)
        end_ns = cold.end_boundary.timestamp_ns
        if end_ns <= start_ns:
            return

        changes: list[str] = []
        expected_total_ms = (end_ns - start_ns) / 1_000_000.0
        if cold.total_duration_ms != expected_total_ms:
            cold.total_duration_ms = expected_total_ms
            changes.append("total_duration_ms")

        if cold.presentation_boundary is not None:
            presentation_ns = cold.presentation_boundary.timestamp_ns
            if presentation_ns >= start_ns:
                expected_presentation_ms = (
                    presentation_ns - start_ns
                ) / 1_000_000.0
                if cold.presentation_duration_ms != expected_presentation_ms:
                    cold.presentation_duration_ms = expected_presentation_ms
                    changes.append("presentation_duration_ms")

        normalized_stages = []
        clipped_stage_names: list[str] = []
        corrected_duration_names: list[str] = []
        for stage in cold.stages:
            next_start = max(stage.start_ns, start_ns)
            next_end = min(stage.end_ns, end_ns)
            if next_end <= next_start:
                clipped_stage_names.append(stage.name)
                continue
            boundaries_changed = (
                next_start != stage.start_ns or next_end != stage.end_ns
            )
            expected_duration_ms = (
                next_end - next_start
            ) / 1_000_000.0
            if boundaries_changed:
                if stage.critical_threads:
                    stage.critical_threads = []
                clipped_stage_names.append(stage.name)
            elif stage.duration_ms != expected_duration_ms:
                corrected_duration_names.append(stage.name)
            stage.start_ns = next_start
            stage.end_ns = next_end
            stage.duration_ms = expected_duration_ms
            normalized_stages.append(stage)
        cold.stages = normalized_stages

        if clipped_stage_names:
            changes.append(
                "clipped_stages=" + "、".join(clipped_stage_names)
            )
        if corrected_duration_names:
            changes.append(
                "recomputed_stage_durations="
                + "、".join(corrected_duration_names)
            )
        if changes:
            note = (
                "应用定义的冷启动边界已保留；确定性归一化已重新计算派生指标"
                "并将阶段裁剪到所选边界内：" + "，".join(changes) + "。"
            )
            if note not in analysis.limitations:
                analysis.limitations.append(note)

    def _apply_proven_presentation(
        self,
        cold: ColdStartAnalysis,
        timeline_record: EvidenceRecord | None,
    ) -> None:
        if timeline_record is None:
            return
        boundary_evidence = timeline_record.data.get("boundary_evidence")
        if not isinstance(boundary_evidence, dict):
            return
        metric = boundary_evidence.get("metric_candidate")
        if (
            not isinstance(metric, dict)
            or metric.get("status") != "proven_platform_boundary_pair"
        ):
            return
        presentation = metric.get("presentation_complete")
        if not isinstance(presentation, dict):
            return
        presentation_ns = presentation.get("timestamp_ns")
        if (
            not isinstance(presentation_ns, int)
            or presentation_ns < cold.start_boundary.timestamp_ns
        ):
            return
        previous = cold.presentation_boundary
        cold.presentation_boundary = TraceBoundary(
            name=str(
                presentation.get("name")
                or "mapped render-service frame completion"
            ),
            timestamp_ns=presentation_ns,
            source=str(presentation.get("source") or "frame_slice"),
            source_id=self._optional_string(presentation.get("source_id")),
            kind="presentation",
            confidence=max(
                previous.confidence if previous is not None else 0.0,
                0.95,
            ),
            evidence_ids=self._merge_evidence_ids(
                previous.evidence_ids if previous is not None else [],
                timeline_record.evidence_id,
            ),
        )

    @staticmethod
    def _timeline_record(
        cold: ColdStartAnalysis,
        evidence: list[EvidenceRecord],
    ) -> EvidenceRecord | None:
        index = EvidenceIndex(evidence)
        matched = index.cold_start_timeline(
            ipid=cold.resolved_process.ipid,
            start_ns=cold.start_boundary.timestamp_ns,
            end_ns=cold.end_boundary.timestamp_ns,
        )
        if matched is not None:
            return matched

        def same_process(record: EvidenceRecord) -> bool:
            target = record.data.get("target_process")
            return (
                isinstance(target, dict)
                and target.get("ipid") == cold.resolved_process.ipid
            )

        fallback = index.last("inspect_cold_start_timeline", same_process)
        if fallback is not None:
            return fallback
        records = index.all("inspect_cold_start_timeline")
        return records[0] if len(records) == 1 else None

    @staticmethod
    def _merge_evidence_ids(values: list[str], required: str) -> list[str]:
        return list(dict.fromkeys([*values, required]))

    @staticmethod
    def _optional_string(value: object) -> str | None:
        return str(value) if value is not None else None
