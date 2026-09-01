from __future__ import annotations

import sqlite3

from trace_agent.evidence import EvidenceStore
from trace_agent.models import TraceCapability, TraceHandle
from trace_agent.tools import TraceToolset


def _build_trace(tmp_path) -> TraceHandle:
    trace_path = tmp_path / "interaction.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "interaction.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE trace_range(start_ts INT, end_ts INT);
            INSERT INTO trace_range
            VALUES (1000000000, 10000000000);

            CREATE TABLE process(
                id INT, ipid INT, pid INT, name TEXT
            );
            INSERT INTO process
            VALUES (1, 10, 100, 'com.example.app');
            INSERT INTO process
            VALUES (2, 20, 200, 'system.input');
            INSERT INTO process
            VALUES (3, 30, 300, 'com.other.app');

            CREATE TABLE thread(
                id INT, itid INT, tid INT, name TEXT, ipid INT
            );
            INSERT INTO thread VALUES (1, 11, 100, 'main', 10);
            INSERT INTO thread VALUES (2, 21, 200, 'input', 20);
            INSERT INTO thread VALUES (3, 31, 300, 'main', 30);

            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT, cat TEXT,
                name TEXT, depth INT, parent_id INT
            );
            INSERT INTO callstack VALUES (
                1, 2000000000, 1000, 21, 'input',
                'H:IEM:PointerEvent id:1 action:3', 0, 0
            );
            INSERT INTO callstack VALUES (
                2, 3000000000, 1000, 21, 'input',
                'H:DispatchTouchEvent type=1', 0, 0
            );
            INSERT INTO callstack VALUES (
                3, 8000000000, 1000, 11, 'config',
                'ArkUIWebUpdateTouchEventFeatureDetectionEnabled', 0, 0
            );
            INSERT INTO callstack VALUES (
                4, 4000000000, 1000, 11, 'app',
                'APP_OPERATION_BEGIN', 0, 0
            );
            INSERT INTO callstack VALUES (
                5, 6500000000, 1000, 11, 'app',
                'HOME_PAGE_STABLE', 0, 0
            );
            INSERT INTO callstack VALUES (
                6, 4500000000, 1000, 31, 'app',
                'APP_OPERATION_BEGIN', 0, 0
            );
            """
        )

    return TraceHandle(
        trace_id="interaction",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.FILE_METADATA,
            TraceCapability.TRACE_DATABASE,
            TraceCapability.MARKERS,
        ],
    )


def test_unique_application_slice_pair_has_priority(tmp_path) -> None:
    trace = _build_trace(tmp_path)
    tools = TraceToolset(
        trace,
        EvidenceStore("interaction"),
    ).build_registry()

    result = tools.invoke(
        "inspect_problem_window_candidates",
        {
            "target_process": "example",
            "start_marker": "APP_OPERATION_BEGIN",
            "end_marker": "HOME_PAGE_STABLE",
            "problem_duration_ms": 1500.0,
            "max_candidates": 20,
        },
    )

    data = result["data"]
    pair = data["application_marker_pair"]
    assert pair["status"] == "resolved_unique_pair"
    assert pair["candidate"]["start_ns"] == 4_000_000_000
    assert pair["candidate"]["end_ns"] == 6_500_000_000
    assert data["default_candidate"]["candidate_kind"] == (
        "application-slice-pair"
    )
    assert result["evidence_id"] == "ev-0001"


def test_last_valid_input_point_plus_duration_is_generic_fallback(
    tmp_path,
) -> None:
    trace = _build_trace(tmp_path)
    tools = TraceToolset(
        trace,
        EvidenceStore("interaction"),
    ).build_registry()

    data = tools.invoke(
        "inspect_problem_window_candidates",
        {
            "target_process": "",
            "start_marker": "",
            "end_marker": "",
            "problem_duration_ms": 1500.0,
            "max_candidates": 20,
        },
    )["data"]

    selected = data["last_input_point"]["candidate"]
    assert selected["source_id"] == "callstack:2"
    assert selected["point_timestamp_ns"] == 3_000_000_000
    assert data["last_input_point"]["single_operation_assumption"] is True
    assert data["duration_window"]["start_ns"] == 3_000_000_000
    assert data["duration_window"]["end_ns"] == 4_500_000_000
    assert data["default_candidate"]["candidate_kind"] == (
        "last-input-plus-duration"
    )
