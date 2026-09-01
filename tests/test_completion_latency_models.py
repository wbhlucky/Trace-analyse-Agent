from __future__ import annotations

import sqlite3

from trace_agent.application.evidence_normalization import (
    DeterministicEvidenceNormalizer,
)
from trace_agent.models import (
    AgentAnalysisDraft,
    AgentKind,
    AnalysisResult,
    AnalyzeRequest,
    CompletionLatencyAnalysis,
    EvidenceRecord,
    LatencyPhaseAnalysis,
    ResolvedProcess,
    ScenarioType,
    TraceBoundary,
    TraceCapability,
    TraceHandle,
)
from trace_agent.validation import AnalysisResultValidator


def _boundary(name: str, timestamp_ns: int, kind: str) -> TraceBoundary:
    return TraceBoundary(
        name=name,
        timestamp_ns=timestamp_ns,
        source="callstack",
        source_id=f"callstack:{name}",
        kind=kind,
        confidence=0.95,
        evidence_ids=["ev-0001"],
    )


def _completion() -> CompletionLatencyAnalysis:
    return CompletionLatencyAnalysis(
        resolved_process=ResolvedProcess(
            name="com.example.app",
            pid=100,
            ipid=10,
            selection_reason="业务 Marker 所在线程属于目标应用",
            confidence=0.95,
            evidence_ids=["ev-0001"],
        ),
        input_boundary=_boundary(
            "operation-start", 2_000_000_000, "application-defined-start"
        ),
        response_boundary=_boundary(
            "first-feedback", 2_100_000_000, "application-response"
        ),
        completion_boundary=_boundary(
            "operation-end", 3_000_000_000, "application-defined-completion"
        ),
        completion_proven=True,
        completion_semantics="应用唯一 duration Slice 定义详情页操作完成",
        response_latency_ms=100,
        post_response_duration_ms=900,
        completion_latency_ms=1000,
        phases=[
            LatencyPhaseAnalysis(
                name="response",
                start_ns=2_000_000_000,
                end_ns=2_100_000_000,
                duration_ms=100,
                assessment="首个有效反馈",
                evidence_ids=["ev-0001"],
            ),
            LatencyPhaseAnalysis(
                name="post-response",
                start_ns=2_100_000_000,
                end_ns=3_000_000_000,
                duration_ms=900,
                assessment="反馈后完成工作",
                evidence_ids=["ev-0001"],
            ),
        ],
        critical_path_summary="响应后阶段主导总耗时",
        evidence_ids=["ev-0001"],
    )


def _context(tmp_path, *, operation_marker: str | None = "OPEN_DETAIL"):
    trace_path = tmp_path / "completion.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "completion.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE trace_range(start_ts INT, end_ts INT)"
        )
        connection.execute(
            "INSERT INTO trace_range VALUES (?, ?)",
            (1_000_000_000, 4_000_000_000),
        )
    request = AnalyzeRequest(
        trace_id="completion",
        trace_path=trace_path,
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="打开详情页",
        symptom="完成较慢",
        output_dir=tmp_path / "result",
        operation_marker=operation_marker,
        agent=AgentKind.QODER,
    )
    trace = TraceHandle(
        trace_id="completion",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[TraceCapability.TRACE_DATABASE],
    )
    evidence = EvidenceRecord(
        evidence_id="ev-0001",
        trace_id="completion",
        tool="inspect_completion_latency_candidates",
        summary="completion candidates",
        data={
            "target_ipid": 10,
            "target_process_candidates": [
                {
                    "ipid": 10,
                    "pid": 100,
                    "main_itid": 11,
                    "main_tid": 100,
                }
            ],
            "discovery_window": {
                "start_ns": 2_000_000_000,
                "end_ns": 3_000_000_000,
            },
            "operation_span": {
                "candidate": {
                    "start_ns": 2_000_000_000,
                    "end_ns": 3_000_000_000,
                }
            },
            "response_candidates": [
                {
                    "candidate_kind": "application-response-marker",
                    "timestamp_ns": 2_100_000_000,
                }
            ],
            "completion_candidates": [
                {
                    "candidate_kind": "application-duration-slice-end",
                    "timestamp_ns": 3_000_000_000,
                }
            ],
        },
    )
    return request, trace, evidence


def test_valid_completion_latency_passes_semantic_validation(tmp_path) -> None:
    request, trace, evidence = _context(tmp_path)
    report = AnalysisResultValidator().validate(
        request=request,
        analysis=AnalysisResult(
            summary="completion analyzed",
            completion_latency=_completion(),
        ),
        evidence=[evidence],
        trace=trace,
    )

    assert report.valid is True
    assert report.errors == []


def test_explicit_time_range_rejects_different_completion_boundaries(
    tmp_path,
) -> None:
    request, trace, evidence = _context(tmp_path, operation_marker=None)
    request.time_range = "2100000000ns-2900000000ns"
    evidence.data["operation_span"] = {"candidate": None}
    evidence.data["explicit_time_range"] = {
        "start_ns": 2_100_000_000,
        "end_ns": 2_900_000_000,
    }

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=AnalysisResult(
            summary="wrong explicit interval",
            completion_latency=_completion(),
        ),
        evidence=[evidence],
        trace=trace,
    )

    codes = {item.code for item in report.errors}
    assert "explicit_time_range_start_mismatch" in codes
    assert "explicit_time_range_end_mismatch" in codes


def test_discovery_only_frame_cannot_prove_completion(tmp_path) -> None:
    request, trace, evidence = _context(tmp_path, operation_marker=None)
    evidence.data["operation_span"] = {"candidate": None}
    evidence.data["completion_candidates"] = [
        {
            "candidate_kind": "frame-quiescence-heuristic",
            "timestamp_ns": 3_000_000_000,
            "discovery_only": True,
            "does_not_prove_business_completion": True,
        }
    ]

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=AnalysisResult(
            summary="invalid completion",
            completion_latency=_completion(),
        ),
        evidence=[evidence],
        trace=trace,
    )

    assert "heuristic_completion_not_proven" in {
        item.code for item in report.errors
    }


def test_unproven_completion_must_leave_completion_values_empty(tmp_path) -> None:
    request, trace, evidence = _context(tmp_path, operation_marker=None)
    completion = _completion()
    completion.completion_proven = False

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=AnalysisResult(
            summary="unproven completion",
            completion_latency=completion,
        ),
        evidence=[evidence],
        trace=trace,
    )

    assert "unproven_completion_must_be_unavailable" in {
        item.code for item in report.errors
    }


def test_explicit_range_end_is_observation_end_when_completion_unproven(
    tmp_path,
) -> None:
    request, trace, evidence = _context(tmp_path, operation_marker=None)
    request.time_range = "2000000000ns-3000000000ns"
    evidence.data["operation_span"] = {"candidate": None}
    evidence.data["explicit_time_range"] = {
        "start_ns": 2_000_000_000,
        "end_ns": 3_000_000_000,
    }
    completion = _completion()
    completion.completion_proven = False

    normalized = DeterministicEvidenceNormalizer().normalize(
        AnalysisResult(
            summary="completion absent before observation end",
            completion_latency=completion,
        ),
        [evidence],
    )
    report = AnalysisResultValidator().validate(
        request=request,
        analysis=normalized,
        evidence=[evidence],
        trace=trace,
    )

    assert normalized.completion_latency is not None
    assert normalized.completion_latency.completion_boundary is None
    assert report.valid is True


def test_normalizer_binds_completion_evidence_and_recomputes_metrics(
    tmp_path,
) -> None:
    _, _, evidence = _context(tmp_path)
    completion = _completion()
    completion.resolved_process.main_itid = None
    completion.resolved_process.main_tid = None
    completion.response_latency_ms = 1
    completion.post_response_duration_ms = 1
    completion.completion_latency_ms = 1
    completion.evidence_ids = []
    completion.input_boundary.evidence_ids = []
    completion.response_boundary.evidence_ids = []
    completion.completion_boundary.evidence_ids = []

    result = DeterministicEvidenceNormalizer().normalize(
        AnalysisResult(
            summary="normalize completion",
            completion_latency=completion,
        ),
        [evidence],
    )
    normalized = result.completion_latency
    assert normalized is not None
    assert normalized.resolved_process.main_itid == 11
    assert normalized.resolved_process.main_tid == 100
    assert normalized.response_latency_ms == 100
    assert normalized.post_response_duration_ms == 900
    assert normalized.completion_latency_ms == 1000
    assert normalized.input_boundary.evidence_ids == ["ev-0001"]
    assert normalized.evidence_ids == ["ev-0001"]


def test_normalizer_removes_unproven_completion_candidate_fields(
    tmp_path,
) -> None:
    request, trace, evidence = _context(
        tmp_path, operation_marker=None
    )
    completion = _completion()
    completion.completion_proven = False

    result = DeterministicEvidenceNormalizer().normalize(
        AnalysisResult(
            summary="unproven candidate",
            completion_latency=completion,
        ),
        [evidence],
    )
    normalized = result.completion_latency
    assert normalized is not None
    assert normalized.completion_boundary is None
    assert normalized.completion_latency_ms is None
    assert normalized.post_response_duration_ms is None
    assert [phase.name for phase in normalized.phases] == ["response"]

    report = AnalysisResultValidator().validate(
        request=request,
        analysis=result,
        evidence=[evidence],
        trace=trace,
    )
    assert report.valid is True


def test_agent_draft_excludes_deterministic_thread_and_perf_payloads() -> None:
    completion_payload = _completion().model_dump()
    for phase in completion_payload["phases"]:
        phase.pop("critical_threads")

    draft = AgentAnalysisDraft.model_validate(
        {
            "summary": "small semantic result",
            "completion_latency": completion_payload,
            "findings": [],
            "limitations": [],
        }
    )
    result = draft.to_analysis_result()

    assert result.perf is None
    assert result.completion_latency is not None
    assert all(
        phase.critical_threads == []
        for phase in result.completion_latency.phases
    )
    schema = AgentAnalysisDraft.model_json_schema()
    assert "ThreadExecutionAnalysis" not in str(schema)
    assert "PerfAnalysis" not in str(schema)
