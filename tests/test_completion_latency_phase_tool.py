from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from trace_agent.application.evidence_normalization import (
    DeterministicEvidenceNormalizer,
)
from trace_agent.application.completion_phase_evidence import (
    CompletionPhaseEvidenceEnsurer,
)
from trace_agent.database import CompletionLatencyPhaseRepository
from trace_agent.evidence import EvidenceIndex, EvidenceStore
from trace_agent.models import (
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
from trace_agent.tools import TraceToolset
from trace_agent.validation import AnalysisResultValidator


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "completion-phases.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, pid INT, name TEXT);
            CREATE TABLE trace_range(start_ts INT, end_ts INT);
            CREATE TABLE thread(
                id INT, itid INT, tid INT, name TEXT, ipid INT
            );
            CREATE TABLE thread_state(
                id INT, ts INT, dur INT, itid INT, state TEXT
            );
            CREATE TABLE sched_slice(
                id INT, ts INT, dur INT, cpu INT,
                itid INT, priority INT
            );
            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT,
                name TEXT, depth INT
            );
            CREATE TABLE frame_slice(
                id INT, ts INT, vsync INT, ipid INT, itid INT,
                dur INT, type_desc TEXT
            );
            CREATE TABLE frame_maps(id INT, src_row INT, dst_row INT);

            INSERT INTO process VALUES
                (10, 100, 'com.example.app'),
                (30, 300, 'render_service');
            INSERT INTO trace_range VALUES (900000000, 2100000000);
            INSERT INTO thread VALUES
                (1, 11, 100, 'main', 10),
                (2, 12, 101, 'worker', 10),
                (3, 31, 300, 'RSUniRenderThre', 30);

            INSERT INTO thread_state VALUES
                (1, 1000000000, 150000000, 11, 'Running'),
                (2, 1150000000,  50000000, 11, 'S'),
                (3, 1200000000, 800000000, 11, 'S'),
                (4, 1000000000,  50000000, 12, 'Running'),
                (5, 1050000000, 150000000, 12, 'S'),
                (6, 1200000000, 500000000, 12, 'Running'),
                (7, 1700000000, 100000000, 12, 'R'),
                (8, 1800000000, 200000000, 12, 'S'),
                (9, 1000000000,  30000000, 31, 'Running'),
                (10, 1030000000, 170000000, 31, 'S'),
                (11, 1200000000, 200000000, 31, 'Running'),
                (12, 1400000000, 600000000, 31, 'S');

            INSERT INTO sched_slice VALUES
                (1, 1000000000, 150000000, 1, 11, 50),
                (2, 1000000000,  50000000, 2, 12, 55),
                (3, 1200000000, 500000000, 3, 12, 55),
                (4, 1000000000,  30000000, 4, 31, 48),
                (5, 1200000000, 200000000, 4, 31, 48);

            INSERT INTO callstack VALUES
                (1, 1000000000, 1000000000, 11,
                 'OPEN_DETAIL_OPERATION', 0),
                (2, 1000000000,  150000000, 11,
                 'BuildFirstFeedback', 1),
                (3, 1200000000,  500000000, 12,
                 'LoadDetailBusinessData', 1),
                (4, 1200000000,  200000000, 31,
                 'CompositeDetailFrame', 1);

            INSERT INTO frame_slice VALUES
                (1, 1050000000, 1, 10, 11, 10000000, 'actural'),
                (2, 1400000000, 2, 10, 11, 50000000, 'actural'),
                (101, 1060000000, 1, 30, 31, 12000000, 'actural'),
                (102, 1450000000, 2, 30, 31, 20000000, 'actural');
            INSERT INTO frame_maps VALUES
                (1, 1, 101),
                (2, 2, 102);
            """
        )
    return path


def _inspect(path: Path) -> dict:
    return CompletionLatencyPhaseRepository(path).inspect(
        target_ipid=10,
        input_ns=1_000_000_000,
        response_ns=1_200_000_000,
        completion_ns=2_000_000_000,
        max_threads_per_phase=4,
        max_slices_per_phase=10,
        max_frames_per_phase=5,
    )


def test_phase_repository_builds_exact_non_overlapping_evidence(
    tmp_path: Path,
) -> None:
    result = _inspect(_database(tmp_path))

    assert [item["name"] for item in result["phases"]] == [
        "response",
        "post-response",
    ]
    response, post_response = result["phases"]
    assert response["end_ns"] == post_response["start_ns"]
    assert response["duration_ms"] == 200
    assert post_response["duration_ms"] == 800

    response_rank = response["thread_rankings"]
    assert response_rank[0]["thread_name"] == "main"
    assert response_rank[0]["state_breakdown"]["running_ms"] == 150

    post_rank = post_response["thread_rankings"]
    assert post_rank[0]["thread_name"] == "worker"
    assert post_rank[0]["state_breakdown"]["running_ms"] == 500
    selected_names = {
        item["thread_execution"]["thread_name"]
        for item in post_response["thread_profiles"]
    }
    assert {"main", "worker", "RSUniRenderThre"} <= selected_names

    wrapper = next(
        item
        for item in post_response["slice_hotspots"]
        if item["name"] == "OPEN_DETAIL_OPERATION"
    )
    assert wrapper["interval_wrapper_candidate"] is True
    assert post_response["frames"]["long_frame_count"] == 1
    assert post_response["frames"]["long_frames"][0]["duration_ms"] == 50
    assert post_response["frames"]["mapped_presentations"]
    assert result["recommended_perf_scope"]["thread_ids"] == [
        100,
        101,
        300,
    ]


def test_phase_repository_refreshes_deadline_after_thread_profiles(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from trace_agent.database import completion_phases as phase_module

    clock = [0.0]
    monkeypatch.setattr(phase_module, "monotonic", lambda: clock[0])

    class ClockAdvancingRepository(CompletionLatencyPhaseRepository):
        def _thread_profiles(
            self,
            selected: list[dict[str, Any]],
            *,
            start_ns: int,
            end_ns: int,
        ) -> list[dict[str, Any]]:
            clock[0] += 21.0
            return []

        @staticmethod
        def _slice_hotspots(
            connection: sqlite3.Connection,
            **_: Any,
        ) -> list[dict[str, Any]]:
            connection.execute(
                "WITH RECURSIVE counter(value) AS ("
                "SELECT 1 UNION ALL SELECT value + 1 FROM counter "
                "WHERE value < 20000) SELECT SUM(value) FROM counter"
            ).fetchone()
            return []

    result = ClockAdvancingRepository(
        _database(tmp_path),
        timeout_seconds=20,
    ).inspect(
        target_ipid=10,
        input_ns=1_000_000_000,
        response_ns=1_200_000_000,
        completion_ns=2_000_000_000,
    )

    assert [phase["name"] for phase in result["phases"]] == [
        "response",
        "post-response",
    ]


def test_phase_tool_registers_one_evidence_bundle(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    trace_path = tmp_path / "completion.htrace"
    trace_path.write_bytes(b"trace")
    trace = TraceHandle(
        trace_id="completion-phases",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.TRACE_DATABASE,
            TraceCapability.CPU_SCHEDULING,
            TraceCapability.FRAME_EVENTS,
        ],
    )
    registry = TraceToolset(
        trace,
        EvidenceStore("completion-phases"),
    ).build_registry()

    result = registry.invoke(
        "inspect_completion_latency_phases",
        {
            "target_ipid": 10,
            "input_ns": 1_000_000_000,
            "response_ns": 1_200_000_000,
            "completion_ns": 2_000_000_000,
            "max_threads_per_phase": 4,
            "max_slices_per_phase": 10,
            "max_frames_per_phase": 5,
        },
    )

    assert result["evidence_id"] == "ev-0001"
    assert result["data"]["target_process"]["ipid"] == 10
    assert len(result["data"]["phases"]) == 2
    assert registry.budget_snapshot()["reserved_invocation_count"] == 1


def test_phase_ensurer_repairs_response_only_evidence_for_final_boundaries(
    tmp_path: Path,
) -> None:
    database_path = _database(tmp_path)
    trace_path = tmp_path / "completion.htrace"
    trace_path.write_bytes(b"trace")
    trace = TraceHandle(
        trace_id="completion-phases",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.TRACE_DATABASE,
            TraceCapability.CPU_SCHEDULING,
        ],
    )
    evidence = EvidenceStore("completion-phases")
    response_only = CompletionLatencyPhaseRepository(database_path).inspect(
        target_ipid=10,
        input_ns=1_000_000_000,
        response_ns=1_200_000_000,
        completion_ns=None,
    )
    evidence.add_evidence(
        tool="inspect_completion_latency_phases",
        summary="response only",
        data=response_only,
    )
    boundary = lambda name, ts, kind: TraceBoundary(
        name=name,
        timestamp_ns=ts,
        source="callstack",
        source_id=f"callstack:{name}",
        kind=kind,
        confidence=0.9,
    )
    analysis = AnalysisResult(
        summary="completion",
        completion_latency=CompletionLatencyAnalysis(
            resolved_process=ResolvedProcess(
                name="com.example.app",
                pid=100,
                ipid=10,
                selection_reason="application marker owner",
                confidence=0.95,
            ),
            input_boundary=boundary("input", 1_000_000_000, "input"),
            response_boundary=boundary(
                "response", 1_200_000_000, "response"
            ),
            completion_boundary=boundary(
                "completion", 2_000_000_000, "completion"
            ),
            completion_proven=True,
            completion_semantics="user supplied duration",
            response_latency_ms=200,
            post_response_duration_ms=800,
            completion_latency_ms=1000,
            phases=[
                LatencyPhaseAnalysis(
                    name="response",
                    start_ns=1_000_000_000,
                    end_ns=1_200_000_000,
                    duration_ms=200,
                    assessment="first feedback",
                ),
                LatencyPhaseAnalysis(
                    name="post-response",
                    start_ns=1_200_000_000,
                    end_ns=2_000_000_000,
                    duration_ms=800,
                    assessment="business completion",
                ),
            ],
            critical_path_summary="worker dominates post response",
        ),
    )

    repaired = CompletionPhaseEvidenceEnsurer().ensure(
        analysis,
        trace=trace,
        evidence=evidence,
    )

    assert repaired is not None
    assert repaired.data["completion_ns"] == 2_000_000_000
    assert len(repaired.data["phases"]) == 2
    exact = EvidenceIndex(evidence.evidence).completion_phases(
        ipid=10,
        input_ns=1_000_000_000,
        response_ns=1_200_000_000,
        completion_ns=2_000_000_000,
    )
    assert exact is not None
    assert exact.evidence_id == repaired.evidence_id
    assert CompletionPhaseEvidenceEnsurer().ensure(
        analysis,
        trace=trace,
        evidence=evidence,
    ) is None
    assert len(evidence.evidence) == 2


def test_normalizer_hydrates_threads_from_phase_bundle(
    tmp_path: Path,
) -> None:
    database_path = _database(tmp_path)
    data = _inspect(database_path)
    phase_evidence = EvidenceRecord(
        evidence_id="ev-0002",
        trace_id="completion-phases",
        tool="inspect_completion_latency_phases",
        summary="phase evidence",
        data=data,
    )
    candidate_evidence = EvidenceRecord(
        evidence_id="ev-0001",
        trace_id="completion-phases",
        tool="inspect_completion_latency_candidates",
        summary="candidate evidence",
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
                "start_ns": 1_000_000_000,
                "end_ns": 2_000_000_000,
            },
        },
    )
    boundary = lambda name, ts, kind: TraceBoundary(
        name=name,
        timestamp_ns=ts,
        source="callstack",
        source_id=f"callstack:{name}",
        kind=kind,
        confidence=0.9,
        evidence_ids=["ev-0001"],
    )
    analysis = AnalysisResult(
        summary="completion",
        completion_latency=CompletionLatencyAnalysis(
            resolved_process=ResolvedProcess(
                name="com.example.app",
                pid=100,
                ipid=10,
                selection_reason="application marker owner",
                confidence=0.95,
                evidence_ids=["ev-0001"],
            ),
            input_boundary=boundary("input", 1_000_000_000, "input"),
            response_boundary=boundary(
                "response", 1_200_000_000, "response"
            ),
            completion_boundary=boundary(
                "completion", 2_000_000_000, "completion"
            ),
            completion_proven=True,
            completion_semantics="application duration marker",
            response_latency_ms=200,
            post_response_duration_ms=800,
            completion_latency_ms=1000,
            phases=[
                LatencyPhaseAnalysis(
                    name="response",
                    start_ns=1_000_000_000,
                    end_ns=1_200_000_000,
                    duration_ms=200,
                    assessment="first feedback",
                    evidence_ids=["ev-0001"],
                ),
                LatencyPhaseAnalysis(
                    name="post-response",
                    start_ns=1_200_000_000,
                    end_ns=2_000_000_000,
                    duration_ms=800,
                    assessment="business completion",
                    evidence_ids=["ev-0001"],
                ),
            ],
            critical_path_summary="worker dominates post response",
            evidence_ids=["ev-0001"],
        ),
    )

    normalized = DeterministicEvidenceNormalizer().normalize(
        analysis,
        [candidate_evidence, phase_evidence],
    )
    completion = normalized.completion_latency
    assert completion is not None
    assert completion.phases[0].critical_threads
    assert completion.phases[1].critical_threads
    assert all(
        "ev-0002" in thread.evidence_ids
        for phase in completion.phases
        for thread in phase.critical_threads
    )
    assert all(
        "ev-0002" in phase.evidence_ids
        for phase in completion.phases
    )

    trace_path = tmp_path / "validated.htrace"
    trace_path.write_bytes(b"trace")
    request = AnalyzeRequest(
        trace_id="completion-phases",
        trace_path=trace_path,
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="open detail",
        symptom="completion is slow",
        output_dir=tmp_path / "result",
        agent=AgentKind.QODER,
    )
    trace = TraceHandle(
        trace_id="completion-phases",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.TRACE_DATABASE,
            TraceCapability.CPU_SCHEDULING,
        ],
    )
    report = AnalysisResultValidator().validate(
        request=request,
        analysis=normalized,
        evidence=[candidate_evidence, phase_evidence],
        trace=trace,
    )
    assert report.valid is True, [
        (item.path, item.code, item.message) for item in report.errors
    ]
