from __future__ import annotations

from typing import Any

from trace_agent.evidence import EvidenceIndex
from trace_agent.models import (
    AnalysisResult,
    EvidenceRecord,
    FindingStatus,
    PerfAnalysis,
    PerfCollectionMetadata,
    PerfEventProfile,
    PerfHotspot,
    ThreadExecutionAnalysis,
)


class DeterministicEvidenceNormalizer:
    """Bind deterministic tool output to the model result.

    Evidence IDs and deterministic aggregates are transport data.  The LLM
    decides what is relevant and explains it; it does not need to reproduce
    foreign keys or large numeric profiles without error.
    """

    def normalize(
        self,
        analysis: AnalysisResult,
        evidence: list[EvidenceRecord],
    ) -> AnalysisResult:
        evidence_index = EvidenceIndex(evidence)
        known_evidence_ids = {
            record.evidence_id for record in evidence_index.records
        }
        for finding in analysis.findings:
            finding.evidence_ids = [
                evidence_id
                for evidence_id in dict.fromkeys(finding.evidence_ids)
                if evidence_id in known_evidence_ids
            ]
            if (
                finding.status is FindingStatus.CONFIRMED
                and not finding.evidence_ids
            ):
                finding.status = FindingStatus.SUSPECTED
                finding.confidence = min(finding.confidence, 0.79)
                note = (
                    f"结论“{finding.title}”未绑定可验证 Evidence，"
                    "已从 confirmed 确定性降级为 suspected。"
                )
                if note not in analysis.limitations:
                    analysis.limitations.append(note)
        cold = analysis.cold_start
        if cold is not None:
            candidate = evidence_index.cold_start_candidate(
                cold.resolved_process.ipid
            )
            timeline = evidence_index.cold_start_timeline(
                ipid=cold.resolved_process.ipid,
                start_ns=cold.start_boundary.timestamp_ns,
                end_ns=cold.end_boundary.timestamp_ns,
            )
            if candidate is not None:
                cold.resolved_process.evidence_ids = self._merge(
                    cold.resolved_process.evidence_ids,
                    candidate.evidence_id,
                )
                self._hydrate_process_identity(cold, candidate)
            if timeline is not None:
                cold.start_boundary.evidence_ids = self._merge(
                    cold.start_boundary.evidence_ids,
                    timeline.evidence_id,
                )
                cold.end_boundary.evidence_ids = self._merge(
                    cold.end_boundary.evidence_ids,
                    timeline.evidence_id,
                )
                if cold.presentation_boundary is not None:
                    cold.presentation_boundary.evidence_ids = self._merge(
                        cold.presentation_boundary.evidence_ids,
                        timeline.evidence_id,
                    )

            for stage in cold.stages:
                if timeline is not None:
                    stage.evidence_ids = self._merge(
                        stage.evidence_ids,
                        timeline.evidence_id,
                    )
                self._hydrate_stage_threads(stage, evidence_index)

            cold.evidence_ids = self._merge(
                cold.evidence_ids,
                *(
                    item.evidence_id
                    for item in (candidate, timeline)
                    if item is not None
                ),
            )

            interval = analysis.problem_interval
            if interval is not None:
                interval_record = evidence_index.cold_start_timeline(
                    ipid=cold.resolved_process.ipid,
                    start_ns=interval.start_boundary.timestamp_ns,
                    end_ns=interval.end_boundary.timestamp_ns,
                ) or timeline
                if interval_record is not None:
                    interval.start_boundary.evidence_ids = self._merge(
                        interval.start_boundary.evidence_ids,
                        interval_record.evidence_id,
                    )
                    interval.end_boundary.evidence_ids = self._merge(
                        interval.end_boundary.evidence_ids,
                        interval_record.evidence_id,
                    )
                    interval.evidence_ids = self._merge(
                        interval.evidence_ids,
                        interval_record.evidence_id,
                    )

        completion = analysis.completion_latency
        if completion is not None:
            if not completion.completion_proven:
                completion.completion_boundary = None
                completion.completion_latency_ms = None
                completion.post_response_duration_ms = None
                response_end_ns = (
                    completion.response_boundary.timestamp_ns
                    if completion.response_boundary is not None
                    else None
                )
                completion.phases = [
                    phase
                    for phase in completion.phases
                    if response_end_ns is not None
                    and phase.start_ns
                    >= completion.input_boundary.timestamp_ns
                    and phase.end_ns <= response_end_ns
                ]
            completion_record = evidence_index.completion_candidate(
                ipid=completion.resolved_process.ipid,
                start_ns=completion.input_boundary.timestamp_ns,
                end_ns=(
                    completion.completion_boundary.timestamp_ns
                    if completion.completion_boundary is not None
                    else (
                        completion.response_boundary.timestamp_ns
                        if completion.response_boundary is not None
                        else completion.input_boundary.timestamp_ns
                    )
                ),
            )
            phase_record = evidence_index.completion_phases(
                ipid=completion.resolved_process.ipid,
                input_ns=completion.input_boundary.timestamp_ns,
                response_ns=(
                    completion.response_boundary.timestamp_ns
                    if completion.response_boundary is not None
                    else None
                ),
                completion_ns=(
                    completion.completion_boundary.timestamp_ns
                    if completion.completion_boundary is not None
                    and completion.completion_proven
                    else None
                ),
            )
            if completion_record is not None:
                evidence_id = completion_record.evidence_id
                completion.resolved_process.evidence_ids = self._merge(
                    completion.resolved_process.evidence_ids,
                    evidence_id,
                )
                self._hydrate_process_identity(
                    completion,
                    completion_record,
                )
                completion.input_boundary.evidence_ids = self._merge(
                    completion.input_boundary.evidence_ids,
                    evidence_id,
                )
                if completion.response_boundary is not None:
                    completion.response_boundary.evidence_ids = self._merge(
                        completion.response_boundary.evidence_ids,
                        evidence_id,
                    )
                if completion.completion_boundary is not None:
                    completion.completion_boundary.evidence_ids = self._merge(
                        completion.completion_boundary.evidence_ids,
                        evidence_id,
                    )
                completion.evidence_ids = self._merge(
                    completion.evidence_ids,
                    evidence_id,
                )

            input_ns = completion.input_boundary.timestamp_ns
            response_ns = (
                completion.response_boundary.timestamp_ns
                if completion.response_boundary is not None
                else None
            )
            completion_ns = (
                completion.completion_boundary.timestamp_ns
                if completion.completion_boundary is not None
                and completion.completion_proven
                else None
            )
            completion.response_latency_ms = (
                (response_ns - input_ns) / 1_000_000.0
                if response_ns is not None
                else None
            )
            completion.completion_latency_ms = (
                (completion_ns - input_ns) / 1_000_000.0
                if completion_ns is not None
                else None
            )
            completion.post_response_duration_ms = (
                (completion_ns - response_ns) / 1_000_000.0
                if completion_ns is not None and response_ns is not None
                else None
            )
            for phase in completion.phases:
                if completion_record is not None:
                    phase.evidence_ids = self._merge(
                        phase.evidence_ids,
                        completion_record.evidence_id,
                    )
                if phase_record is not None:
                    phase.evidence_ids = self._merge(
                        phase.evidence_ids,
                        phase_record.evidence_id,
                    )
                self._hydrate_stage_threads(phase, evidence_index)
            if phase_record is not None:
                completion.evidence_ids = self._merge(
                    completion.evidence_ids,
                    phase_record.evidence_id,
                )

            interval = analysis.problem_interval
            if (
                interval is not None
                and completion_record is not None
                and interval.start_boundary.timestamp_ns == input_ns
                and completion_ns is not None
                and interval.end_boundary.timestamp_ns == completion_ns
            ):
                interval.start_boundary.evidence_ids = self._merge(
                    interval.start_boundary.evidence_ids,
                    completion_record.evidence_id,
                )
                interval.end_boundary.evidence_ids = self._merge(
                    interval.end_boundary.evidence_ids,
                    completion_record.evidence_id,
                )
                interval.evidence_ids = self._merge(
                    interval.evidence_ids,
                    completion_record.evidence_id,
                )

        profile = evidence_index.perf_profile()
        if profile is not None:
            analysis.perf = self._normalize_perf(analysis.perf, profile)
        return analysis

    @staticmethod
    def _merge(values: list[str], *required: str) -> list[str]:
        return list(dict.fromkeys([*values, *required]))

    @staticmethod
    def _hydrate_process_identity(cold: Any, record: EvidenceRecord) -> None:
        candidates = record.data.get("process_candidates")
        if not isinstance(candidates, list):
            candidates = record.data.get("target_process_candidates")
        if not isinstance(candidates, list):
            return
        candidate = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and item.get("ipid") == cold.resolved_process.ipid
            ),
            None,
        )
        if not isinstance(candidate, dict):
            return
        if cold.resolved_process.main_tid is None and isinstance(
            candidate.get("main_tid"), int
        ):
            cold.resolved_process.main_tid = candidate["main_tid"]
        if cold.resolved_process.main_itid is None and isinstance(
            candidate.get("main_itid"), int
        ):
            cold.resolved_process.main_itid = candidate["main_itid"]

    @classmethod
    def _hydrate_stage_threads(
        cls,
        stage: Any,
        evidence_index: EvidenceIndex,
    ) -> None:
        for profile, raw_thread in evidence_index.thread_profiles(
            start_ns=stage.start_ns,
            end_ns=stage.end_ns,
        ):
            if not isinstance(raw_thread, dict):
                continue
            payload = dict(raw_thread)
            payload["evidence_ids"] = cls._merge(
                list(payload.get("evidence_ids") or []),
                profile.evidence_id,
            )
            deterministic = ThreadExecutionAnalysis.model_validate(payload)
            existing_index = next(
                (
                    index
                    for index, item in enumerate(stage.critical_threads)
                    if item.itid == deterministic.itid
                ),
                None,
            )
            if existing_index is None:
                stage.critical_threads.append(deterministic)
            else:
                stage.critical_threads[existing_index] = deterministic
            stage.evidence_ids = cls._merge(
                stage.evidence_ids,
                profile.evidence_id,
            )

    @classmethod
    def _normalize_perf(
        cls,
        current: PerfAnalysis | None,
        record: EvidenceRecord,
    ) -> PerfAnalysis:
        data = record.data
        raw_collection = data.get("collection")
        if not isinstance(raw_collection, dict):
            if current is None:
                raise ValueError(
                    "inspect_perf_profile Evidence 缺少 collection"
                )
            return current
        collection = PerfCollectionMetadata(
            config_names=list(raw_collection.get("config_names") or []),
            command_line=raw_collection.get("command_line"),
            scope=str(raw_collection.get("scope") or "unknown"),
            sampling_frequency_hz=raw_collection.get(
                "sampling_frequency_hz"
            ),
            callstack_mode=raw_collection.get("callstack_mode"),
            clock_id=raw_collection.get("clock_id"),
            target_pids=list(raw_collection.get("target_pids") or []),
        )
        existing_hotspots = {
            (hotspot.symbol, hotspot.file_path): hotspot
            for event in (current.events if current is not None else [])
            for hotspot in event.hotspots
        }
        events: list[PerfEventProfile] = []
        for raw_event in data.get("event_profiles") or []:
            if not isinstance(raw_event, dict):
                continue
            hotspots: list[PerfHotspot] = []
            for raw_hotspot in raw_event.get("hotspots") or []:
                if not isinstance(raw_hotspot, dict):
                    continue
                key = (
                    str(raw_hotspot.get("symbol") or "unknown"),
                    raw_hotspot.get("file_path"),
                )
                old = existing_hotspots.get(key)
                hotspots.append(
                    PerfHotspot(
                        symbol=key[0],
                        file_path=key[1],
                        layer=str(
                            raw_hotspot.get("layer") or "application"
                        ),
                        self_samples=int(raw_hotspot.get("self_samples") or 0),
                        inclusive_samples=int(
                            raw_hotspot.get("inclusive_samples") or 0
                        ),
                        self_event_count=int(
                            raw_hotspot.get("self_event_count") or 0
                        ),
                        inclusive_event_count=int(
                            raw_hotspot.get("inclusive_event_count") or 0
                        ),
                        inclusive_share=float(
                            raw_hotspot.get("inclusive_share") or 0
                        ),
                        critical_path_relevance=(
                            old.critical_path_relevance
                            if old is not None
                            else (
                                "应用包或应用自带库中的确定性 Perf 热点；"
                                "需结合 Trace 阶段判断具体业务含义和因果"
                            )
                        ),
                        evidence_ids=[record.evidence_id],
                    )
                )
            events.append(
                PerfEventProfile(
                    event_type_id=int(raw_event.get("event_type_id") or 0),
                    event_name=str(raw_event.get("event_name") or "unknown"),
                    sample_count=int(raw_event.get("sample_count") or 0),
                    total_event_count=int(
                        raw_event.get("total_event_count") or 0
                    ),
                    hotspots=hotspots,
                    evidence_ids=[record.evidence_id],
                )
            )

        limitations = list(
            dict.fromkeys(
                [
                    *(current.limitations if current is not None else []),
                    *(data.get("limitations") or []),
                ]
            )
        )
        return PerfAnalysis(
            interval_start_ns=int(data["interval_start_ns"]),
            interval_end_ns=int(data["interval_end_ns"]),
            collection=collection,
            process_ids=list(data.get("observed_process_ids") or []),
            thread_ids=list(data.get("requested_thread_ids") or []),
            sample_count=int(data.get("sample_count") or 0),
            total_callchain_frames=int(
                data.get("total_callchain_frames") or 0
            ),
            symbolized_callchain_frames=int(
                data.get("symbolized_callchain_frames") or 0
            ),
            symbolization_rate=data.get("symbolization_rate"),
            events=events,
            assessment=(
                current.assessment
                if current is not None
                else "已按问题窗口和相关线程完成确定性 Perf 聚合"
            ),
            confidence=(current.confidence if current is not None else 0.9),
            evidence_ids=[record.evidence_id],
            limitations=limitations,
        )
