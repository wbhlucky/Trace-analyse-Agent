from __future__ import annotations

import sqlite3

from trace_agent.evidence import EvidenceStore
from trace_agent.models import TraceCapability, TraceHandle
from trace_agent.tools import TraceToolset


def test_cold_start_tool_returns_stage_and_boundary_candidates(
    tmp_path,
) -> None:
    trace_path = tmp_path / "cold-start.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "cold-start.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE trace_range(start_ts INT, end_ts INT);
            INSERT INTO trace_range VALUES (1000, 5000);

            CREATE TABLE process(
                id INT, ipid INT, pid INT, name TEXT, start_ts INT,
                switch_count INT, thread_count INT, slice_count INT,
                mem_count INT
            );
            INSERT INTO process
            VALUES (1, 10, 100, 'com.example.app', 1100, 5, 2, 8, 0);
            INSERT INTO process
            VALUES (2, 20, 200, 'system.launcher', 1000, 5, 1, 3, 0);
            INSERT INTO process
            VALUES (3, 30, 300, 'render_service', 1000, 5, 1, 3, 0);

            CREATE TABLE thread(
                id INT, itid INT, tid INT, name TEXT, start_ts INT,
                end_ts INT, ipid INT, is_main_thread INT,
                switch_count INT
            );
            INSERT INTO thread
            VALUES (1, 11, 100, 'main', 1100, NULL, 10, 1, 5);
            INSERT INTO thread
            VALUES (2, 21, 200, 'launcher_main', 1000, NULL, 20, 1, 5);
            INSERT INTO thread
            VALUES (3, 31, 300, 'rs_main', 1000, NULL, 30, 1, 5);

            CREATE TABLE data_dict(id INT, data TEXT);
            INSERT INTO data_dict VALUES (7, 'ApplicationLaunch');
            INSERT INTO data_dict VALUES (8, 'com.example.app');

            CREATE TABLE app_startup(
                id INT, call_id INT, ipid INT, tid INT,
                start_time INT, end_time INT, start_name INT,
                packed_name TEXT
            );
            INSERT INTO app_startup
            VALUES (1, 20, 20, 200, 1200, 1800, 7, 8);

            CREATE TABLE frame_slice(
                id INT, ts INT, vsync INT, ipid INT, itid INT,
                callstack_id INT, dur INT, src TEXT, dst INT,
                type INT, type_desc TEXT, flag INT, depth INT,
                frame_no INT
            );
            INSERT INTO frame_slice
            VALUES (1, 1850, 1, 10, 11, 20, 10, '', 0, 0,
                    'expect', 0, 0, 1);
            INSERT INTO frame_slice
            VALUES (2, 1900, 1, 10, 11, 20, 20, '', 0, 0,
                    'actural', 0, 0, 1);
            INSERT INTO frame_slice
            VALUES (3, 1950, 1, 30, 31, NULL, 30, '', 0, 0,
                    'actural', 0, 0, 1);

            CREATE TABLE frame_maps(id INT, src_row INT, dst_row INT);
            INSERT INTO frame_maps VALUES (1, 2, 3);

            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT, cat TEXT,
                name TEXT, depth INT, parent_id INT
            );
            INSERT INTO callstack
            VALUES (
                20, 1150, 700, 11, 'startup', 'Launch', 0, 4294967295
            );
            INSERT INTO callstack
            VALUES (
                21, 1050, 80, 21, 'startup',
                'request com.example.app', 0, 4294967295
            );
            INSERT INTO callstack
            VALUES (
                22, 1160, 20, 11, 'startup',
                'AppSpawnExecuteClearEnvHook', 1, 20
            );
            INSERT INTO callstack
            VALUES (
                23, 1900, 20, 11, 'frame',
                'ReceiveVsync', 0, 4294967295
            );
            UPDATE frame_slice SET callstack_id = 23 WHERE id = 2;
            """
        )

    trace = TraceHandle(
        trace_id="cold-start",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.FILE_METADATA,
            TraceCapability.TRACE_DATABASE,
            TraceCapability.APP_STARTUP_STAGES,
        ],
    )
    evidence = EvidenceStore("cold-start")
    tools = TraceToolset(trace, evidence).build_registry()

    result = tools.invoke(
        "inspect_cold_start_candidates",
        {
            "target_process": "example",
            "max_candidates": 10,
        },
    )

    data = result["data"]
    assert data["trace_range"] == {
        "start_ns": 1000,
        "end_ns": 5000,
    }
    assert data["process_candidates"][0]["started_inside_trace"] is True
    assert data["app_startup"]["row_count"] == 1
    assert data["app_startup"]["matched_row_count"] == 1
    assert data["app_startup"]["stages"][0]["stage_name"] == (
        "ApplicationLaunch"
    )
    assert data["first_frame_candidates"][0][
        "first_actual_frame_ns"
    ] == 1900
    assert data["earliest_slice_candidates"][0]["slice_name"] == (
        "Launch"
    )
    assert data["app_startup"]["stages"][0]["package_name"] == (
        "com.example.app"
    )
    assert result["evidence_id"] == "ev-0001"

    timeline = tools.invoke(
        "inspect_cold_start_timeline",
        {
            "target_ipid": 10,
            "lookback_ms": 1,
            "lookahead_ms": 1,
            "max_events": 50,
        },
    )["data"]

    assert timeline["window"]["anchor_source"] == "process.start_ts"
    assert timeline["app_startup"]["window_matched_row_count"] == 1
    assert timeline["app_startup"]["stages"][0]["ipid"] == 20
    assert any(
        "target_package_name_match" in event["candidate_reasons"]
        for event in timeline["timeline_events"]
        if event["source"] == "callstack"
    )
    assert timeline["frame_links"][0]["src_row"] == 2
    assert timeline["frame_links"][0]["dst_row"] == 3
    boundary = timeline["boundary_evidence"]
    assert boundary["launch_candidates"][0]["source_id"] == (
        "callstack:22"
    )
    assert boundary["mapped_frame_candidates"][0][
        "main_thread_receive_proven"
    ] is True
    assert boundary["metric_candidate"]["application_complete"][
        "timestamp_ns"
    ] == 1920
    assert boundary["metric_candidate"]["presentation_complete"][
        "timestamp_ns"
    ] == 1980
    assert timeline["decision_policy"].startswith(
        "这些是结构化候选事实"
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE process SET start_ts = NULL WHERE ipid = 10"
        )
        connection.execute(
            "UPDATE thread SET start_ts = NULL WHERE ipid = 10"
        )

    fallback_timeline = tools.invoke(
        "inspect_cold_start_timeline",
        {
            "target_ipid": 10,
            "lookback_ms": 1,
            "lookahead_ms": 1,
            "max_events": 50,
        },
    )["data"]
    assert fallback_timeline["window"]["anchor_source"] == (
        "target_process.first_slice"
    )
    assert (
        "process.start_ts 缺失，时间线使用后备锚点"
        in fallback_timeline["limitations"]
    )
