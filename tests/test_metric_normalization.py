from __future__ import annotations

from trace_agent.application.normalization import ColdStartMetricNormalizer
from trace_agent.models import AnalysisResult, EvidenceRecord


def test_normalizer_applies_proven_platform_metric_and_clips_stages():
    analysis = AnalysisResult.model_validate(
        {
            "summary": "agent selected click metric",
            "cold_start": {
                "resolved_process": {
                    "name": "com.example.app",
                    "pid": 10,
                    "ipid": 1,
                    "selection_reason": "new process",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "cold_start_proven": True,
                "classification_reason": "spawn chain",
                "start_boundary": {
                    "name": "click",
                    "timestamp_ns": 900,
                    "source": "callstack",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "frame",
                    "timestamp_ns": 2100,
                    "source": "frame_slice",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-0001"],
                },
                "total_duration_ms": 0.0012,
                "presentation_boundary": {
                    "name": "rs",
                    "timestamp_ns": 2200,
                    "source": "frame_slice",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-0001"],
                },
                "presentation_duration_ms": 0.0001,
                    "stages": [
                        {
                            "name": "spawn",
                        "start_ns": 900,
                        "end_ns": 1200,
                        "duration_ms": 0.0003,
                            "assessment": "includes click lead-in",
                            "evidence_ids": ["ev-0001"],
                        },
                        {
                            "name": "runtime",
                            "start_ns": 1200,
                            "end_ns": 1800,
                            "duration_ms": 999,
                            "assessment": "agent supplied wrong duration",
                            "evidence_ids": ["ev-0001"],
                        }
                ],
                "critical_path_summary": "spawn to frame",
                "evidence_ids": ["ev-0001"],
            },
        }
    )
    evidence = [
        EvidenceRecord(
            evidence_id="ev-0002",
            trace_id="trace",
            tool="inspect_cold_start_timeline",
            summary="metric",
            data={
                "boundary_evidence": {
                    "metric_candidate": {
                        "status": "proven_platform_boundary_pair",
                        "start": {
                            "name": "AppSpawn",
                            "ts": 1000,
                            "source": "callstack",
                            "source_id": "callstack:10",
                        },
                        "application_complete": {
                            "name": "ReceiveVsync",
                            "timestamp_ns": 2000,
                            "source": "callstack",
                            "source_id": "callstack:20",
                        },
                        "presentation_complete": {
                            "name": "RS frame end",
                            "timestamp_ns": 2150,
                            "source": "frame_slice",
                            "source_id": "frame_slice:30",
                        },
                    }
                }
            },
        )
    ]

    result = ColdStartMetricNormalizer().normalize(analysis, evidence)
    cold = result.cold_start

    assert cold is not None
    assert cold.start_boundary.timestamp_ns == 1000
    assert cold.end_boundary.timestamp_ns == 2000
    assert cold.total_duration_ms == 0.001
    assert cold.presentation_boundary is not None
    assert cold.presentation_boundary.timestamp_ns == 2150
    assert cold.presentation_duration_ms == 0.00115
    assert cold.stages[0].start_ns == 1000
    assert cold.stages[0].duration_ms == 0.0002
    assert cold.stages[1].duration_ms == 0.0006
    assert "ev-0002" in cold.start_boundary.evidence_ids
    assert result.limitations


def test_normalizer_preserves_explicit_application_defined_cold_start_interval():
    analysis = AnalysisResult.model_validate(
        {
            "summary": "home page stable metric",
            "problem_interval": {
                "metric_definition": "explicit startup observation window",
                "start_boundary": {
                    "name": "window start",
                    "timestamp_ns": 800,
                    "source": "explicit_time_range",
                    "source_id": "time_range",
                    "kind": "explicit-time-range-start",
                    "confidence": 1.0,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "home stable",
                    "timestamp_ns": 4_500_000_900,
                    "source": "explicit_time_range",
                    "source_id": "time_range",
                    "kind": "explicit-time-range-end",
                    "confidence": 1.0,
                    "evidence_ids": ["ev-0001"],
                },
                "duration_ms": 4500.0001,
                "selection_rule": "explicit range",
                "evidence_ids": ["ev-0001"],
            },
            "cold_start": {
                "resolved_process": {
                    "name": "com.example.app",
                    "pid": 10,
                    "ipid": 1,
                    "selection_reason": "new process",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "cold_start_proven": True,
                "classification_reason": "spawn chain",
                "start_boundary": {
                    "name": "APP_START_BEGIN",
                    "timestamp_ns": 900,
                    "source": "callstack",
                    "kind": "application-marker-start",
                    "confidence": 0.95,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "HOME_PAGE_STABLE",
                    "timestamp_ns": 4_000_000_900,
                    "source": "callstack",
                    "kind": "stable-home-frame",
                    "confidence": 0.95,
                    "evidence_ids": ["ev-0001"],
                },
                "total_duration_ms": 4000,
                "presentation_boundary": {
                    "name": "technical first frame presented",
                    "timestamp_ns": 2250,
                    "source": "frame_slice",
                    "kind": "presentation",
                    "confidence": 0.95,
                    "evidence_ids": ["ev-0001"],
                },
                "presentation_duration_ms": 999,
                "stages": [
                    {
                        "name": "launch dispatch",
                        "start_ns": 800,
                        "end_ns": 1200,
                        "duration_ms": 0.0004,
                        "assessment": "starts before the selected boundary",
                        "evidence_ids": ["ev-0001"],
                    }
                ],
                "critical_path_summary": "launch to stable home page",
                "evidence_ids": ["ev-0001"],
            },
        }
    )
    evidence = [
        EvidenceRecord(
            evidence_id="ev-0002",
            trace_id="trace",
            tool="inspect_cold_start_timeline",
            summary="technical first frame",
            data={
                "boundary_evidence": {
                    "metric_candidate": {
                        "status": "proven_platform_boundary_pair",
                        "start": {"ts": 1000},
                        "application_complete": {
                            "timestamp_ns": 2000
                        },
                        "presentation_complete": {
                            "name": "mapped render-service frame end",
                            "timestamp_ns": 2150,
                            "source": "frame_slice",
                            "source_id": "frame_slice:30",
                        },
                    }
                }
            },
        )
    ]

    result = ColdStartMetricNormalizer().normalize(analysis, evidence)
    cold = result.cold_start

    assert cold is not None
    assert cold.start_boundary.timestamp_ns == 900
    assert cold.end_boundary.timestamp_ns == 4_500_000_900
    assert cold.total_duration_ms == 4500
    assert cold.presentation_boundary is not None
    assert cold.presentation_boundary.timestamp_ns == 2150
    assert cold.presentation_duration_ms == 0.00125
    assert cold.stages[0].start_ns == 900
    assert cold.stages[0].duration_ms == 0.0003
    assert result.limitations
