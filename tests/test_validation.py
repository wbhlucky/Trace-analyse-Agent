from __future__ import annotations

import sqlite3
from pathlib import Path

from trace_agent.models import (
    AgentKind,
    AnalysisResult,
    AnalyzeRequest,
    ColdStartAnalysis,
    ColdStartStage,
    CpuExecutionShare,
    EvidenceRecord,
    Finding,
    FindingSeverity,
    FindingStatus,
    PerfAnalysis,
    PerfCollectionMetadata,
    PerfEventProfile,
    ProblemInterval,
    ResolvedProcess,
    ScenarioType,
    SchedulingDiagnosis,
    ThreadExecutionAnalysis,
    ThreadPriorityProfile,
    ThreadStateBreakdown,
    TraceBoundary,
    TraceCapability,
    TraceHandle,
)
from trace_agent.validation import AnalysisResultValidator


def _valid_cold_start() -> ColdStartAnalysis:
    thread = ThreadExecutionAnalysis(
        process_name="com.example.app",
        thread_name="main",
        pid=100,
        ipid=10,
        tid=100,
        itid=11,
        state_breakdown=ThreadStateBreakdown(
            running_ms=120,
            runnable_ms=20,
            sleeping_ms=60,
            uninterruptible_io_ms=0,
            uninterruptible_other_ms=0,
            other_ms=0,
        ),
        cpu_distribution=[
            CpuExecutionShare(
                cpu=3,
                running_ms=80,
                share=2 / 3,
                schedule_slices=4,
            ),
            CpuExecutionShare(
                cpu=5,
                running_ms=40,
                share=1 / 3,
                schedule_slices=2,
            ),
        ],
        cpu_migrations=1,
        schedule_slices=6,
        longest_running_ms=45,
        longest_runnable_ms=12,
        longest_sleep_ms=40,
        priority=ThreadPriorityProfile(
            observed_values=[120],
            dominant_value=120,
            interpretation="原始值，排序语义未确认",
        ),
        diagnosis=SchedulingDiagnosis.MIXED,
        assessment="CPU 执行与等待共同贡献",
        confidence=0.8,
        evidence_ids=["ev-0004"],
    )
    return ColdStartAnalysis(
        resolved_process=ResolvedProcess(
            name="com.example.app",
            pid=100,
            ipid=10,
            main_tid=100,
            main_itid=11,
            selection_reason="Trace 内新建并产生首帧",
            confidence=0.95,
            evidence_ids=["ev-0001"],
        ),
        cold_start_proven=True,
        classification_reason="目标进程在 Trace 内首次创建",
        start_boundary=TraceBoundary(
            name="process-start",
            timestamp_ns=1_000_000_000,
            source="process.start_ts",
            confidence=0.9,
            evidence_ids=["ev-0001"],
        ),
        end_boundary=TraceBoundary(
            name="first-frame",
            timestamp_ns=1_200_000_000,
            source="frame_slice",
            source_id="frame_slice:42",
            confidence=0.9,
            evidence_ids=["ev-0002"],
        ),
        total_duration_ms=200,
        stages=[
            ColdStartStage(
                name="application-init",
                start_ns=1_000_000_000,
                end_ns=1_200_000_000,
                duration_ms=200,
                critical_threads=[thread],
                assessment="初始化阶段",
                evidence_ids=["ev-0003", "ev-0004"],
            )
        ],
        critical_path_summary="初始化阶段控制首帧完成",
        evidence_ids=[
            "ev-0001",
            "ev-0002",
            "ev-0003",
            "ev-0004",
        ],
    )


def _context(
    tmp_path: Path,
) -> tuple[AnalyzeRequest, TraceHandle, list[EvidenceRecord]]:
    trace_path = tmp_path / "cold-start.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "trace.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE trace_range(start_ts INT, end_ts INT)"
        )
        connection.execute(
            "INSERT INTO trace_range VALUES (?, ?)",
            (900_000_000, 1_300_000_000),
        )
    request = AnalyzeRequest(
        trace_id="cold-start",
        trace_path=trace_path,
        scenario_type=ScenarioType.COLD_START,
        scenario="应用冷启动",
        symptom="启动较慢",
        output_dir=tmp_path / "results",
    )
    trace = TraceHandle(
        trace_id="cold-start",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.TRACE_DATABASE,
            TraceCapability.CPU_SCHEDULING,
        ],
    )
    evidence = [
        EvidenceRecord(
            evidence_id=f"ev-{index:04d}",
            trace_id="cold-start",
            tool="query_trace_sql",
            summary="evidence",
            data={},
        )
        for index in range(1, 5)
    ]
    return request, trace, evidence


def test_valid_cold_start_passes_semantic_validation(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    analysis = AnalysisResult(
        summary="cold start analyzed",
        cold_start=_valid_cold_start(),
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    assert report.valid is True
    assert report.errors == []


def test_small_sched_and_thread_state_running_mismatch_is_a_warning(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    cold_start = _valid_cold_start()
    cold_start.stages[0].critical_threads[0].cpu_distribution[
        0
    ].running_ms = 81.2

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=AnalysisResult(
            summary="minor source mismatch",
            cold_start=cold_start,
        ),
        evidence=evidence,
        trace=trace,
    )

    assert report.valid is True
    assert "cpu_running_mismatch" in {
        issue.code for issue in report.warnings
    }


def test_cpu_running_longer_than_analysis_interval_is_rejected(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    cold_start = _valid_cold_start()
    cold_start.stages[0].critical_threads[0].cpu_distribution[
        0
    ].running_ms = 161.2

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=AnalysisResult(
            summary="impossible cpu duration",
            cold_start=cold_start,
        ),
        evidence=evidence,
        trace=trace,
    )

    assert "cpu_running_exceeds_interval" in {
        issue.code for issue in report.errors
    }


def test_invalid_cold_start_reports_deterministic_errors(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    cold_start = _valid_cold_start()
    cold_start.cold_start_proven = False
    cold_start.total_duration_ms = 10
    cold_start.end_boundary.confidence = 0.5
    thread = cold_start.stages[0].critical_threads[0]
    thread.state_breakdown.running_ms = 250
    thread.longest_running_ms = 260
    analysis = AnalysisResult(
        summary="invalid",
        cold_start=cold_start,
        findings=[
            Finding(
                title="confirmed cause",
                severity=FindingSeverity.HIGH,
                status=FindingStatus.CONFIRMED,
                confidence=0.9,
                evidence_ids=["ev-missing"],
                analysis="cause",
                recommendation="fix",
                verification="rerun",
            )
        ],
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    assert report.valid is False
    codes = {issue.code for issue in report.errors}
    assert "unknown_evidence_id" in codes
    assert "cold_start_duration_mismatch" in codes
    assert "confirmed_with_unproven_cold_start" in codes
    assert "state_duration_exceeds_interval" in codes
    assert "longest_running_exceeds_total" in codes


def test_proven_metric_candidate_rejects_different_boundaries(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    evidence.append(
        EvidenceRecord(
            evidence_id="ev-0005",
            trace_id="cold-start",
            tool="inspect_cold_start_timeline",
            summary="proven boundary chain",
            data={
                "boundary_evidence": {
                    "metric_candidate": {
                        "status": "proven_platform_boundary_pair",
                        "start": {"ts": 1_010_000_000},
                        "application_complete": {
                            "timestamp_ns": 1_190_000_000
                        },
                        "presentation_complete": {
                            "timestamp_ns": 1_210_000_000
                        },
                    }
                }
            },
        )
    )
    analysis = AnalysisResult(
        summary="uses different boundaries",
        cold_start=_valid_cold_start(),
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    codes = {issue.code for issue in report.errors}
    assert "unproven_launch_boundary" in codes
    assert "unproven_first_frame_boundary" in codes


def test_application_defined_interval_does_not_get_overridden_by_platform_frame(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    evidence.append(
        EvidenceRecord(
            evidence_id="ev-0005",
            trace_id="cold-start",
            tool="inspect_cold_start_timeline",
            summary="platform comparison boundary",
            data={
                "boundary_evidence": {
                    "metric_candidate": {
                        "status": "proven_platform_boundary_pair",
                        "start": {"ts": 1_010_000_000},
                        "application_complete": {
                            "timestamp_ns": 1_190_000_000
                        },
                    }
                }
            },
        )
    )
    cold_start = _valid_cold_start()
    cold_start.start_boundary.kind = "application-marker-start"
    cold_start.end_boundary.kind = "stable-home-frame"
    cold_start.end_boundary.timestamp_ns = 1_300_000_000
    cold_start.total_duration_ms = 300
    cold_start.presentation_boundary = TraceBoundary(
        name="technical first frame presented",
        timestamp_ns=1_210_000_000,
        source="frame_slice",
        source_id="frame_slice:presented",
        kind="presentation",
        confidence=0.95,
        evidence_ids=["ev-0002"],
    )
    cold_start.presentation_duration_ms = 210
    analysis = AnalysisResult(
        summary="application-defined cold start",
        problem_interval=ProblemInterval(
            metric_definition="应用进入首页并达到稳定帧",
            start_boundary=cold_start.start_boundary,
            end_boundary=cold_start.end_boundary,
            duration_ms=300,
            selection_rule="unique application Slice pair",
            evidence_ids=["ev-0001", "ev-0002"],
        ),
        cold_start=cold_start,
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    codes = {issue.code for issue in report.errors}
    assert "unproven_launch_boundary" not in codes
    assert "unproven_first_frame_boundary" not in codes
    assert report.valid is True


def test_qoder_result_requires_perf_when_samples_are_available(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    request.agent = AgentKind.QODER
    trace.capabilities.append(TraceCapability.PERF_SAMPLES)
    analysis = AnalysisResult(
        summary="cold start analyzed without perf",
        cold_start=_valid_cold_start(),
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    assert report.valid is False
    assert "perf_analysis_required" in {
        issue.code for issue in report.errors
    }


def test_qoder_result_rejects_capability_only_empty_perf(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    request.agent = AgentKind.QODER
    trace.capabilities.append(TraceCapability.PERF_SAMPLES)
    analysis = AnalysisResult(
        summary="empty perf placeholder",
        cold_start=_valid_cold_start(),
        perf=PerfAnalysis(
            interval_start_ns=1_000_000_000,
            interval_end_ns=1_200_000_000,
            collection=PerfCollectionMetadata(scope="unknown"),
            sample_count=0,
            total_callchain_frames=0,
            symbolized_callchain_frames=0,
            assessment="not queried",
            confidence=0.1,
            evidence_ids=["ev-0001"],
        ),
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    assert "perf_profile_evidence_required" in {
        issue.code for issue in report.errors
    }


def test_qoder_result_accepts_matching_perf_profile_evidence(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    request.agent = AgentKind.QODER
    trace.capabilities.append(TraceCapability.PERF_SAMPLES)
    evidence.append(
        EvidenceRecord(
            evidence_id="ev-0005",
            trace_id="cold-start",
            tool="inspect_perf_profile",
            summary="profile",
            data={
                "sample_count": 3,
                "requested_thread_ids": [100],
                "event_profiles": [
                    {
                        "event_type_id": 0,
                        "sample_count": 3,
                        "total_event_count": 30,
                    }
                ],
            },
        )
    )
    cold_start = _valid_cold_start()
    thread = cold_start.stages[0].critical_threads[0]
    thread.evidence_ids.append("ev-0006")
    evidence.append(
        EvidenceRecord(
            evidence_id="ev-0006",
            trace_id="cold-start",
            tool="inspect_thread_execution",
            summary="exact stage thread profile",
            data={
                "interval_start_ns": cold_start.stages[0].start_ns,
                "interval_end_ns": cold_start.stages[0].end_ns,
                "itid": thread.itid,
                "thread_execution": thread.model_dump(mode="json"),
            },
        )
    )
    analysis = AnalysisResult(
        summary="perf analyzed",
        cold_start=cold_start,
        perf=PerfAnalysis(
            interval_start_ns=1_000_000_000,
            interval_end_ns=1_200_000_000,
            collection=PerfCollectionMetadata(scope="system-wide"),
            process_ids=[100],
            thread_ids=[100],
            sample_count=3,
            total_callchain_frames=9,
            symbolized_callchain_frames=9,
            symbolization_rate=1.0,
            events=[
                PerfEventProfile(
                    event_type_id=0,
                    event_name="hw-cpu-cycles",
                    sample_count=3,
                    total_event_count=30,
                    evidence_ids=["ev-0005"],
                )
            ],
            assessment="profiled",
            confidence=0.8,
            evidence_ids=["ev-0005"],
        ),
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    assert report.valid is True


def test_qoder_stage_thread_requires_exact_profile_evidence(
    tmp_path: Path,
) -> None:
    request, trace, evidence = _context(tmp_path)
    request.agent = AgentKind.QODER
    analysis = AnalysisResult(
        summary="thread stats reused from another interval",
        cold_start=_valid_cold_start(),
    )

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=analysis,
        evidence=evidence,
        trace=trace,
    )

    assert "thread_execution_profile_required" in {
        issue.code for issue in report.errors
    }
