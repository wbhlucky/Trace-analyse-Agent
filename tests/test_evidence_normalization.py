from __future__ import annotations

from trace_agent.application.evidence_normalization import (
    DeterministicEvidenceNormalizer,
)
from trace_agent.models import (
    AnalysisResult,
    EvidenceRecord,
    Finding,
    FindingSeverity,
    FindingStatus,
)


def test_normalizer_binds_tool_evidence_and_projects_deterministic_data():
    analysis = AnalysisResult.model_validate(
        {
            "summary": "compact model result",
            "cold_start": {
                "resolved_process": {
                    "name": "com.example.app",
                    "pid": 100,
                    "ipid": 10,
                    "selection_reason": "candidate",
                    "confidence": 0.9,
                },
                "cold_start_proven": True,
                "classification_reason": "spawn marker",
                "start_boundary": {
                    "name": "start",
                    "timestamp_ns": 1_000_000_000,
                    "source": "callstack",
                    "confidence": 0.9,
                },
                "end_boundary": {
                    "name": "frame",
                    "timestamp_ns": 1_200_000_000,
                    "source": "frame_slice",
                    "confidence": 0.9,
                },
                "total_duration_ms": 200,
                "stages": [
                    {
                        "name": "module loading",
                        "start_ns": 1_000_000_000,
                        "end_ns": 1_200_000_000,
                        "duration_ms": 200,
                        "assessment": "CPU work",
                    }
                ],
                "critical_path_summary": "main thread",
            },
            "perf": {
                "interval_start_ns": 1_000_000_000,
                "interval_end_ns": 1_200_000_000,
                "collection": {"scope": "unknown"},
                "process_ids": [100],
                "thread_ids": [100],
                "sample_count": 2,
                "total_callchain_frames": 2,
                "symbolized_callchain_frames": 1,
                "symbolization_rate": 0.5,
                "events": [],
                "assessment": "CPU profile",
                "confidence": 0.7,
            },
        }
    )
    evidence = [
        EvidenceRecord(
            evidence_id="ev-0001",
            trace_id="trace",
            tool="inspect_cold_start_candidates",
            summary="candidate",
            data={
                "process_candidates": [
                    {
                        "ipid": 10,
                        "pid": 100,
                        "main_tid": 100,
                        "main_itid": 11,
                    }
                ]
            },
        ),
        EvidenceRecord(
            evidence_id="ev-0002",
            trace_id="trace",
            tool="inspect_cold_start_timeline",
            summary="timeline",
            data={
                "target_process": {"ipid": 10},
                "window": {
                    "start_ns": 900_000_000,
                    "end_ns": 1_300_000_000,
                },
            },
        ),
        EvidenceRecord(
            evidence_id="ev-0003",
            trace_id="trace",
            tool="inspect_thread_execution",
            summary="thread",
            data={
                "interval_start_ns": 1_000_000_000,
                "interval_end_ns": 1_200_000_000,
                "itid": 11,
                "thread_execution": {
                    "process_name": "com.example.app",
                    "thread_name": "main",
                    "pid": 100,
                    "ipid": 10,
                    "tid": 100,
                    "itid": 11,
                    "state_breakdown": {
                        "running_ms": 150,
                        "runnable_ms": 10,
                        "sleeping_ms": 40,
                        "uninterruptible_io_ms": 0,
                        "uninterruptible_other_ms": 0,
                        "other_ms": 0,
                    },
                    "cpu_distribution": [
                        {
                            "cpu": 3,
                            "running_ms": 150,
                            "share": 1,
                            "schedule_slices": 4,
                        }
                    ],
                    "cpu_migrations": 0,
                    "schedule_slices": 4,
                    "longest_running_ms": 50,
                    "longest_runnable_ms": 5,
                    "longest_sleep_ms": 20,
                    "priority": {
                        "observed_values": [53],
                        "dominant_value": 53,
                        "interpretation": "raw",
                    },
                    "diagnosis": "cpu-bound",
                    "assessment": "CPU-bound",
                    "confidence": 0.9,
                },
            },
        ),
        EvidenceRecord(
            evidence_id="ev-0004",
            trace_id="trace",
            tool="inspect_perf_profile",
            summary="perf",
            data={
                "interval_start_ns": 1_000_000_000,
                "interval_end_ns": 1_200_000_000,
                "collection": {
                    "config_names": ["hw-cpu-cycles"],
                    "scope": "system-wide",
                    "sampling_frequency_hz": 1000,
                },
                "requested_thread_ids": [100, 101],
                "observed_process_ids": [100],
                "sample_count": 3,
                "total_callchain_frames": 9,
                "symbolized_callchain_frames": 6,
                "symbolization_rate": 2 / 3,
                "event_profiles": [
                    {
                        "event_type_id": 0,
                        "event_name": "hw-cpu-cycles",
                        "sample_count": 3,
                        "total_event_count": 30,
                        "hotspots": [
                            {
                                "symbol": "foo",
                                "file_path": "/system/libfoo.so",
                                "self_samples": 2,
                                "inclusive_samples": 3,
                                "self_event_count": 20,
                                "inclusive_event_count": 30,
                                "inclusive_share": 1,
                            }
                        ],
                    }
                ],
                "limitations": [],
            },
        ),
    ]

    without_perf = analysis.model_copy(deep=True)
    without_perf.perf = None
    result = DeterministicEvidenceNormalizer().normalize(
        analysis,
        evidence,
    )

    cold = result.cold_start
    assert cold is not None
    assert cold.resolved_process.main_itid == 11
    assert cold.resolved_process.evidence_ids == ["ev-0001"]
    assert cold.start_boundary.evidence_ids == ["ev-0002"]
    assert cold.end_boundary.evidence_ids == ["ev-0002"]
    assert cold.stages[0].evidence_ids == ["ev-0002", "ev-0003"]
    assert cold.stages[0].critical_threads[0].evidence_ids == ["ev-0003"]

    perf = result.perf
    assert perf is not None
    assert perf.thread_ids == [100, 101]
    assert perf.sample_count == 3
    assert perf.evidence_ids == ["ev-0004"]
    assert perf.events[0].evidence_ids == ["ev-0004"]
    assert perf.events[0].hotspots[0].evidence_ids == ["ev-0004"]
    projected = DeterministicEvidenceNormalizer().normalize(
        without_perf,
        evidence,
    ).perf
    assert projected is not None
    assert projected.sample_count == 3
    assert projected.evidence_ids == ["ev-0004"]


def test_normalizer_downgrades_confirmed_finding_without_evidence() -> None:
    analysis = AnalysisResult(
        summary="unsupported confirmed conclusion",
        findings=[
            Finding(
                title="Unbound root cause",
                severity=FindingSeverity.HIGH,
                status=FindingStatus.CONFIRMED,
                confidence=0.95,
                evidence_ids=[],
                analysis="No evidence foreign key was supplied.",
                recommendation="Collect evidence.",
                verification="Re-run the analysis.",
            )
        ],
    )

    normalized = DeterministicEvidenceNormalizer().normalize(
        analysis,
        [],
    )

    assert normalized.findings[0].status is FindingStatus.SUSPECTED
    assert normalized.findings[0].confidence == 0.79
    assert "确定性降级" in normalized.limitations[0]
