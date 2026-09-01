from __future__ import annotations

import sqlite3

from trace_agent.evidence import EvidenceStore
from trace_agent.models import TraceCapability, TraceHandle
from trace_agent.tools import TraceToolset


def _build_trace(tmp_path) -> TraceHandle:
    trace_path = tmp_path / "completion.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "completion.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE trace_range(start_ts INT, end_ts INT);
            INSERT INTO trace_range VALUES (0, 5000000000);

            CREATE TABLE process(id INT, ipid INT, pid INT, name TEXT);
            INSERT INTO process VALUES (1, 10, 100, 'com.example.app');
            INSERT INTO process VALUES (2, 20, 200, 'system.input');
            INSERT INTO process VALUES (3, 30, 300, 'render_service');

            CREATE TABLE thread(
                id INT, itid INT, tid INT, name TEXT, ipid INT
            );
            INSERT INTO thread VALUES (1, 11, 100, 'main', 10);
            INSERT INTO thread VALUES (2, 21, 200, 'input', 20);
            INSERT INTO thread VALUES (3, 31, 300, 'rs_main', 30);

            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT, cat TEXT,
                name TEXT, depth INT, parent_id INT
            );
            INSERT INTO callstack VALUES (
                1, 1000000000, 10000000, 21, 'input',
                'H:IEM:PointerEvent action:3', 0, 0
            );
            INSERT INTO callstack VALUES (
                2, 2000000000, 1000000000, 11, 'app',
                'OPEN_DETAIL_OPERATION', 0, 0
            );
            INSERT INTO callstack VALUES (
                3, 2100000000, 1000, 11, 'app',
                'DETAIL_FIRST_FEEDBACK', 1, 2
            );
            INSERT INTO callstack VALUES (
                4, 3000000000, 1000, 11, 'app',
                'DETAIL_PAGE_STABLE', 1, 2
            );
            INSERT INTO callstack VALUES (
                5, 2200000000, 500000000, 31, 'animation',
                'H:ABILITY_OR_PAGE_SWITCH,detail', 0, 0
            );

            CREATE TABLE frame_slice(
                id INT, ts INT, vsync INT, ipid INT, itid INT,
                callstack_id INT, dur INT, src TEXT, dst INT,
                type INT, type_desc TEXT, flag INT, depth INT,
                frame_no INT
            );
            INSERT INTO frame_slice VALUES
                (1, 2050000000, 1, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 1),
                (2, 2100000000, 2, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 2),
                (3, 2150000000, 3, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 3),
                (4, 2200000000, 4, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 4),
                (5, 2250000000, 5, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 5),
                (6, 2300000000, 6, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 6),
                (7, 2600000000, 7, 10, 11, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 7),
                (101, 2060000000, 1, 30, 31, 0, 10000000, '', 0, 0,
                 'actural', 0, 0, 1);

            CREATE TABLE frame_maps(id INT, src_row INT, dst_row INT);
            INSERT INTO frame_maps VALUES (1, 1, 101);
            """
        )
    return TraceHandle(
        trace_id="completion",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.TRACE_DATABASE,
            TraceCapability.MARKERS,
            TraceCapability.FRAME_EVENTS,
        ],
    )


def _arguments(**overrides):
    values = {
        "target_ipid": 10,
        "target_process": "com.example.app",
        "operation_marker": "OPEN_DETAIL_OPERATION",
        "start_marker": "",
        "end_marker": "",
        "response_marker": "DETAIL_FIRST_FEEDBACK",
        "completion_marker": "DETAIL_PAGE_STABLE",
        "problem_duration_ms": 0.0,
        "interval_start_ns": 0,
        "interval_end_ns": 0,
        "lookahead_ms": 5000,
        "max_candidates": 80,
    }
    values.update(overrides)
    return values


def test_completion_tool_returns_business_and_discovery_candidates(
    tmp_path,
) -> None:
    tools = TraceToolset(
        _build_trace(tmp_path), EvidenceStore("completion")
    ).build_registry()

    result = tools.invoke(
        "inspect_completion_latency_candidates", _arguments()
    )
    data = result["data"]

    assert data["target_ipid"] == 10
    assert data["operation_span"]["status"] == (
        "resolved_unique_duration_slice"
    )
    assert data["operation_span"]["candidate"]["start_ns"] == 2_000_000_000
    assert data["operation_span"]["candidate"]["end_ns"] == 3_000_000_000
    assert data["input_boundary"]["timestamp_ns"] == 1_010_000_000
    assert data["input_boundary"]["timestamp_rule"] == "input Slice end"
    assert any(
        item["candidate_kind"] == "application-response-marker"
        for item in data["response_candidates"]
    )
    assert any(
        item["candidate_kind"] == "first-mapped-presentation-completion"
        for item in data["response_candidates"]
    )
    assert any(
        item["candidate_kind"] == "application-duration-slice-end"
        and item["timestamp_ns"] == 3_000_000_000
        for item in data["completion_candidates"]
    )
    quiet = next(
        item
        for item in data["completion_candidates"]
        if item["candidate_kind"] == "frame-quiescence-heuristic"
    )
    assert quiet["discovery_only"] is True
    assert quiet["does_not_prove_business_completion"] is True
    assert result["evidence_id"] == "ev-0001"


def test_completion_tool_supports_process_discovery_then_exact_scope(
    tmp_path,
) -> None:
    tools = TraceToolset(
        _build_trace(tmp_path), EvidenceStore("completion")
    ).build_registry()

    discovery = tools.invoke(
        "inspect_completion_latency_candidates",
        _arguments(target_ipid=0, target_process=""),
    )["data"]
    assert discovery["target_ipid"] is None
    assert any(
        item["ipid"] == 10
        for item in discovery["target_process_candidates"]
    )
    assert discovery["app_frames"] == []

    scoped = tools.invoke(
        "inspect_completion_latency_candidates", _arguments()
    )["data"]
    assert scoped["target_ipid"] == 10
    assert len(scoped["app_frames"]) == 7


def test_single_start_marker_can_define_positive_duration_operation(
    tmp_path,
) -> None:
    tools = TraceToolset(
        _build_trace(tmp_path), EvidenceStore("completion")
    ).build_registry()

    data = tools.invoke(
        "inspect_completion_latency_candidates",
        _arguments(
            operation_marker="",
            start_marker="OPEN_DETAIL_OPERATION",
            completion_marker="",
        ),
    )["data"]

    assert data["operation_span"]["candidate"]["requested_from"] == (
        "single_start_marker"
    )
    assert data["discovery_window"]["source"] == (
        "single-start-duration-slice"
    )


def test_last_input_plus_user_duration_is_a_metric_candidate(tmp_path) -> None:
    tools = TraceToolset(
        _build_trace(tmp_path), EvidenceStore("completion")
    ).build_registry()

    data = tools.invoke(
        "inspect_completion_latency_candidates",
        _arguments(
            operation_marker="",
            response_marker="",
            completion_marker="",
            problem_duration_ms=3500.0,
        ),
    )["data"]

    assert data["duration_window"] == {
        "start_ns": 1_010_000_000,
        "end_ns": 4_510_000_000,
        "duration_ms": 3500.0,
        "candidate_kind": "last-input-plus-user-duration",
        "single_operation_assumption": True,
        "metric_definition_from_user": True,
    }
    assert data["discovery_window"]["source"] == (
        "input-plus-known-duration"
    )
    assert any(
        item["candidate_kind"] == "input-plus-user-duration"
        and item["timestamp_ns"] == 4_510_000_000
        for item in data["completion_candidates"]
    )


def test_explicit_time_range_overrides_last_input_and_duration(tmp_path) -> None:
    tools = TraceToolset(
        _build_trace(tmp_path), EvidenceStore("completion")
    ).build_registry()

    data = tools.invoke(
        "inspect_completion_latency_candidates",
        _arguments(
            operation_marker="",
            response_marker="",
            completion_marker="",
            problem_duration_ms=3500.0,
            interval_start_ns=2_000_000_000,
            interval_end_ns=3_000_000_000,
        ),
    )["data"]

    assert data["input_boundary"]["timestamp_ns"] == 2_000_000_000
    assert data["explicit_time_range"]["end_ns"] == 3_000_000_000
    assert data["discovery_window"] == {
        "start_ns": 2_000_000_000,
        "end_ns": 3_000_000_000,
        "source": "explicit-time-range",
        "is_metric_candidate": True,
    }
    completion = data["completion_candidates"][0]
    assert completion["candidate_kind"] == "explicit-time-range"
    assert completion["timestamp_ns"] == 3_000_000_000
    assert completion["single_operation_assumption"] is False
