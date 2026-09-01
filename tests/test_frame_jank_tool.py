from __future__ import annotations

import sqlite3
from pathlib import Path

from trace_agent.database import FrameJankRepository
from trace_agent.evidence import EvidenceStore
from trace_agent.models import TraceCapability, TraceHandle
from trace_agent.tools import TraceToolset


START_NS = 1_000_000_000
END_NS = 1_100_000_000
BUDGET_NS = 8_333_333


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "frame-jank.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, pid INT, name TEXT);
            CREATE TABLE thread(
                id INT, itid INT, tid INT, name TEXT, ipid INT
            );
            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT,
                name TEXT, depth INT
            );
            CREATE TABLE frame_slice(
                id INT, ts INT, vsync INT, ipid INT, itid INT,
                callstack_id INT, dur INT, src TEXT, dst INT,
                type INT, type_desc TEXT, flag INT, depth INT,
                frame_no INT
            );
            CREATE TABLE frame_maps(id INT, src_row INT, dst_row INT);

            INSERT INTO process VALUES
                (10, 100, 'com.example.flutter'),
                (30, 300, 'render_service');
            INSERT INTO thread VALUES
                (1, 11, 100, 'main', 10),
                (2, 12, 101, 'FlutterUI', 10),
                (3, 13, 102, 'FlutterRaster', 10),
                (4, 31, 301, 'RSUniRenderThread', 30),
                (5, 32, 302, 'UnrelatedRSThread', 30);
            INSERT INTO callstack VALUES
                (1, 1000000000, 100000000, 12,
                 'Dart UI Frame', 0),
                (2, 1000000000, 100000000, 31,
                 'RSUniRender::DrawFrame', 0),
                (3, 1000000000, 100000000, 32,
                 'UnrelatedSystemComposition', 0);
            """
        )
        for index in range(4):
            vsync = 1_000 + index
            expected_id = 100 + index
            actual_id = 200 + index
            render_expected_id = 300 + index
            render_actual_id = 400 + index
            expected_ts = START_NS + index * BUDGET_NS
            actual_ts = expected_ts + 500_000
            actual_duration = 20_000_000 if index == 1 else 3_000_000
            render_ts = actual_ts + actual_duration
            connection.execute(
                "INSERT INTO frame_slice VALUES "
                "(?, ?, ?, 10, 12, 0, ?, '', ?, 1, 'expect', 0, 0, 0)",
                (
                    expected_id,
                    expected_ts,
                    vsync,
                    BUDGET_NS,
                    render_expected_id,
                ),
            )
            connection.execute(
                "INSERT INTO frame_slice VALUES "
                "(?, ?, ?, 10, 12, 0, ?, '', ?, 0, 'actural', ?, 0, 0)",
                (
                    actual_id,
                    actual_ts,
                    vsync,
                    actual_duration,
                    render_actual_id,
                    1 if index == 1 else 0,
                ),
            )
            connection.execute(
                "INSERT INTO frame_slice VALUES "
                "(?, ?, ?, 30, 31, 0, ?, '', 0, 1, 'expect', 0, 0, 0)",
                (
                    render_expected_id,
                    expected_ts,
                    2_000 + index,
                    BUDGET_NS,
                ),
            )
            connection.execute(
                "INSERT INTO frame_slice VALUES "
                "(?, ?, ?, 30, 31, 0, 3000000, '', 0, 0, 'actural', 0, 0, 0)",
                (render_actual_id, render_ts, 2_000 + index),
            )
            connection.execute(
                "INSERT INTO frame_maps VALUES (?, ?, ?)",
                (index + 1, actual_id, render_actual_id),
            )

        # One expected slot without an application actual frame.
        connection.execute(
            "INSERT INTO frame_slice VALUES "
            "(150, ?, 1004, 10, 12, 0, ?, '', 350, "
            "1, 'expect', 0, 0, 0)",
            (START_NS + 4 * BUDGET_NS, BUDGET_NS),
        )
        connection.execute(
            "INSERT INTO frame_slice VALUES "
            "(350, ?, 2004, 30, 31, 0, ?, '', 0, "
            "1, 'expect', 0, 0, 0)",
            (START_NS + 4 * BUDGET_NS, BUDGET_NS),
        )
    return path


def _inspect(path: Path) -> dict:
    return FrameJankRepository(path).inspect(
        target_ipid=10,
        interval_start_ns=START_NS,
        interval_end_ns=END_NS,
        refresh_rate_hz=None,
        frame_producer_itid=None,
        max_bad_frames=10,
        max_clusters=5,
    )


def test_repository_uses_frame_owner_and_mapped_render_pipeline(
    tmp_path: Path,
) -> None:
    result = _inspect(_database(tmp_path))

    assert result["available"] is True
    assert result["selected_pipeline"]["itid"] == 12
    assert result["selected_pipeline"]["main_thread_assumed"] is False
    assert result["cadence"]["dominant_refresh_rate_hz"] == 120
    assert result["metrics"]["application_actual_frames"] == 4
    assert result["metrics"]["mapped_presented_frames"] == 4
    assert result["metrics"]["problem_interval_frame_count"] == 4
    assert result["metrics"]["problem_interval_fps"] == 40.0
    assert result["metrics"]["problem_interval_fps_source"] == (
        "target-mapped-render-service"
    )
    assert result["metrics"]["dropped_frame_candidates"] == 1
    assert result["metrics"]["jank_frames"] == 1
    assert result["bad_frames"][0]["observable_delay_stage"] == (
        "application-production"
    )

    architecture = result["render_architecture"]
    assert architecture["framework_family"] == "flutter"
    assert architecture["ui_thread_equals_main_thread_assumed"] is False
    assert architecture["unified_rendering"]["status"] == "proven"
    mapped_roles = [
        item
        for item in architecture["thread_roles"]
        if "target-frame-mapped-render-service" in item["roles"]
    ]
    assert [item["itid"] for item in mapped_roles] == [31]
    assert 32 not in architecture["recommended_perf_thread_ids"]


def test_repository_selects_busy_qt_useragent_for_perf(
    tmp_path: Path,
) -> None:
    database_path = _database(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            UPDATE process SET name='com.example.qt' WHERE ipid=10;
            UPDATE thread SET name='FrameOwner' WHERE itid=12;
            UPDATE thread SET name='Raster' WHERE itid=13;
            UPDATE callstack SET name='FrameOwnerLoop' WHERE id=1;
            INSERT INTO thread VALUES (6, 14, 103, 'UserAgent', 10);
            INSERT INTO callstack VALUES
                (10, 1000000000, 90000000, 14,
                 'QBackingStore::flush', 0),
                (11, 1000000000, 90000000, 14,
                 'NativeWindowFlushBuffer', 1);
            CREATE TABLE sched_slice(
                id INT, ts INT, dur INT, cpu INT, itid INT,
                ipid INT, priority INT, end_state TEXT
            );
            INSERT INTO sched_slice VALUES
                (1, 1000000000, 80000000, 7, 14, 10, 50, 'R'),
                (2, 1000000000, 5000000, 4, 12, 10, 50, 'R');
            CREATE TABLE perf_thread(
                thread_id INT, process_id INT, thread_name TEXT
            );
            CREATE TABLE perf_sample(
                thread_id INT, timestamp_trace INT
            );
            INSERT INTO perf_thread VALUES
                (101, 100, 'FrameOwner'),
                (103, 100, 'UserAgent');
            """
        )
        connection.executemany(
            "INSERT INTO perf_sample VALUES (103, ?)",
            [(START_NS + index * 1000,) for index in range(20)],
        )
        connection.execute(
            "INSERT INTO perf_sample VALUES (101, ?)", (START_NS,)
        )

    architecture = _inspect(database_path)["render_architecture"]

    assert architecture["framework_family"] == "qt"
    assert 103 in architecture["recommended_perf_thread_ids"]
    useragent = next(
        item
        for item in architecture["thread_roles"]
        if item["tid"] == 103
    )
    assert "framework-ui-event-loop-candidate" in useragent["roles"]
    assert "surface-submit-or-ui-paint-candidate" in useragent["roles"]
    assert useragent["running_share"] == 0.8
    assert useragent["perf_sample_share"] > 0.9
    mismatch = architecture["frame_producer_ui_mismatch"]
    assert mismatch["status"] == "strong-ui-candidate-differs-from-frame-owner"
    assert mismatch["ui_candidate_tid"] == 103


def test_tool_registers_bounded_frame_evidence(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    trace_path = tmp_path / "frames.htrace"
    trace_path.write_bytes(b"trace")
    trace = TraceHandle(
        trace_id="frame-jank",
        trace_path=trace_path,
        format="htrace",
        size_bytes=5,
        database_path=database_path,
        capabilities=[
            TraceCapability.TRACE_DATABASE,
            TraceCapability.FRAME_EVENTS,
        ],
    )
    registry = TraceToolset(
        trace,
        EvidenceStore("frame-jank"),
    ).build_registry()

    evidence = registry.invoke(
        "inspect_frame_jank",
        {
            "target_ipid": 10,
            "interval_start_ns": START_NS,
            "interval_end_ns": END_NS,
            "refresh_rate_hz": 0.0,
            "frame_producer_itid": 0,
            "max_bad_frames": 10,
            "max_clusters": 5,
        },
    )

    assert evidence["tool"] == "inspect_frame_jank"
    assert evidence["data"]["selected_pipeline"]["itid"] == 12
    assert evidence["data"]["metrics"]["jank_frames"] == 1
