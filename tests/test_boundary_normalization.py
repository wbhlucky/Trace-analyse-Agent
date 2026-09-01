from __future__ import annotations

from pathlib import Path

from trace_agent.application.boundary_normalization import (
    ExplicitProblemIntervalNormalizer,
)
from trace_agent.models import AnalysisResult, AnalyzeRequest, ScenarioType


def test_explicit_time_range_replaces_agent_problem_window() -> None:
    request = AnalyzeRequest(
        trace_id="trace",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="cold start",
        symptom="slow",
        output_dir=Path("results"),
        time_range="1000ns-5000001000ns",
    )
    analysis = AnalysisResult.model_validate(
        {
            "summary": "agent chose a narrower interval",
            "problem_interval": {
                "metric_definition": "agent candidate",
                "start_boundary": {
                    "name": "candidate start",
                    "timestamp_ns": 2000,
                    "source": "callstack",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "candidate end",
                    "timestamp_ns": 4000,
                    "source": "frame_slice",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0002"],
                },
                "duration_ms": 0.002,
                "selection_rule": "agent inferred",
                "single_operation_assumption": True,
                "evidence_ids": ["ev-0001", "ev-0002"],
            },
        }
    )

    result = ExplicitProblemIntervalNormalizer().normalize(analysis, request)
    interval = result.problem_interval

    assert interval is not None
    assert interval.start_boundary.timestamp_ns == 1000
    assert interval.end_boundary.timestamp_ns == 5_000_001_000
    assert interval.duration_ms == 5000
    assert interval.start_boundary.source == "explicit_time_range"
    assert interval.end_boundary.source_id == "time_range"
    assert interval.single_operation_assumption is False
    assert result.limitations


def test_requested_duration_replaces_inconsistent_agent_window_end() -> None:
    request = AnalyzeRequest(
        trace_id="trace",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="cold start",
        symptom="about 5.6 seconds",
        output_dir=Path("results"),
        problem_duration_ms=5600,
    )
    analysis = AnalysisResult.model_validate(
        {
            "summary": "technical completion is earlier",
            "problem_interval": {
                "metric_definition": "user observation window",
                "start_boundary": {
                    "name": "AppSpawn",
                    "timestamp_ns": 1_000,
                    "source": "callstack",
                    "confidence": 0.95,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "APP_COMPONENT_LOAD completion",
                    "timestamp_ns": 3_159_587_000,
                    "source": "callstack",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0002"],
                },
                "duration_ms": 5600,
                "selection_rule": "mixed user and technical metrics",
                "evidence_ids": ["ev-0001", "ev-0002"],
            },
        }
    )

    result = ExplicitProblemIntervalNormalizer().normalize(analysis, request)
    interval = result.problem_interval

    assert interval is not None
    assert interval.start_boundary.timestamp_ns == 1_000
    assert interval.end_boundary.timestamp_ns == 5_600_001_000
    assert interval.end_boundary.source == "user_problem_duration"
    assert interval.end_boundary.source_id == "problem_duration_ms"
    assert interval.duration_ms == 5600
    assert result.limitations


def test_completion_duration_normalizes_boundary_metrics_and_phases() -> None:
    request = AnalyzeRequest(
        trace_id="trace",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="open page",
        symptom="about 5.6 seconds",
        output_dir=Path("results"),
        problem_duration_ms=5600,
    )
    analysis = AnalysisResult.model_validate(
        {
            "summary": "agent selected an earlier animation",
            "problem_interval": {
                "metric_definition": "user duration",
                "start_boundary": {
                    "name": "input",
                    "timestamp_ns": 1_000,
                    "source": "callstack",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "animation",
                    "timestamp_ns": 4_477_001_000,
                    "source": "callstack",
                    "confidence": 0.6,
                    "evidence_ids": ["ev-0002"],
                },
                "duration_ms": 4477,
                "selection_rule": "agent animation candidate",
                "evidence_ids": ["ev-0001", "ev-0002"],
            },
            "completion_latency": {
                "resolved_process": {
                    "name": "app",
                    "pid": 100,
                    "ipid": 10,
                    "selection_reason": "input owner",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "input_boundary": {
                    "name": "input",
                    "timestamp_ns": 1_000,
                    "source": "callstack",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "response_boundary": {
                    "name": "response",
                    "timestamp_ns": 72_501_000,
                    "source": "frame_maps",
                    "confidence": 0.7,
                    "evidence_ids": ["ev-0002"],
                },
                "completion_boundary": {
                    "name": "animation",
                    "timestamp_ns": 4_477_001_000,
                    "source": "callstack",
                    "confidence": 0.6,
                    "evidence_ids": ["ev-0002"],
                },
                "completion_proven": True,
                "completion_semantics": "animation end",
                "response_latency_ms": 72.5,
                "post_response_duration_ms": 4404.5,
                "completion_latency_ms": 4477,
                "phases": [
                    {
                        "name": "response",
                        "start_ns": 1_000,
                        "end_ns": 72_501_000,
                        "duration_ms": 72.5,
                        "assessment": "first response",
                        "evidence_ids": ["ev-0002"],
                    },
                    {
                        "name": "post-response",
                        "start_ns": 72_501_000,
                        "end_ns": 4_477_001_000,
                        "duration_ms": 4404.5,
                        "assessment": "animation",
                        "evidence_ids": ["ev-0002"],
                    },
                ],
                "critical_path_summary": "browser work",
                "evidence_ids": ["ev-0001", "ev-0002"],
            },
        }
    )

    result = ExplicitProblemIntervalNormalizer().normalize(analysis, request)
    interval = result.problem_interval
    completion = result.completion_latency

    assert interval is not None
    assert completion is not None
    assert interval.end_boundary.timestamp_ns == 5_600_001_000
    assert completion.completion_boundary is not None
    assert completion.completion_boundary.timestamp_ns == 5_600_001_000
    assert completion.completion_boundary.source == "user_problem_duration"
    assert completion.completion_latency_ms == 5600
    assert completion.post_response_duration_ms == 5527.5
    assert completion.phases[1].start_ns == 72_501_000
    assert completion.phases[1].end_ns == 5_600_001_000
    assert completion.phases[1].duration_ms == 5527.5
    assert completion.phases[1].critical_threads == []
    assert "5600ms" in result.summary


def test_explicit_time_range_has_priority_over_requested_duration() -> None:
    request = AnalyzeRequest(
        trace_id="trace",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="cold start",
        symptom="slow",
        output_dir=Path("results"),
        time_range="1000ns-5000001000ns",
        problem_duration_ms=5600,
    )
    analysis = AnalysisResult.model_validate(
        {
            "summary": "agent interval",
            "problem_interval": {
                "metric_definition": "candidate",
                "start_boundary": {
                    "name": "start",
                    "timestamp_ns": 2_000,
                    "source": "callstack",
                    "confidence": 0.9,
                },
                "end_boundary": {
                    "name": "end",
                    "timestamp_ns": 4_000,
                    "source": "callstack",
                    "confidence": 0.9,
                },
                "duration_ms": 0.002,
                "selection_rule": "agent",
            },
        }
    )

    interval = ExplicitProblemIntervalNormalizer().normalize(
        analysis,
        request,
    ).problem_interval

    assert interval is not None
    assert interval.start_boundary.timestamp_ns == 1_000
    assert interval.end_boundary.timestamp_ns == 5_000_001_000
    assert interval.duration_ms == 5000
    assert interval.end_boundary.source == "explicit_time_range"
