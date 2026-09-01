from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from trace_agent.evidence import EvidenceIndex
from trace_agent.models import EvidenceRecord
from trace_agent.report import ReportProjectionBuilder


def test_cold_timeline_keeps_app_completion_after_earlier_presentation() -> None:
    cold = SimpleNamespace(
        resolved_process=SimpleNamespace(
            ipid=10,
            name="com.example.app",
            main_itid=11,
        ),
        start_boundary=SimpleNamespace(
            timestamp_ns=100,
            source="marker",
            confidence=1.0,
        ),
        end_boundary=SimpleNamespace(
            timestamp_ns=500,
            name="Stable home",
            kind="stable-home-frame",
            source="application-definition",
            confidence=0.9,
        ),
        presentation_boundary=SimpleNamespace(
            timestamp_ns=300,
            source="frame_slice",
            confidence=0.9,
        ),
        stages=[],
    )
    analysis = SimpleNamespace(cold_start=cold)

    timeline = ReportProjectionBuilder()._build_cold_start_timeline(
        analysis=analysis,
        evidence_index=EvidenceIndex([]),
        database_path=None,
    )

    assert timeline["end_ns"] == 500
    assert timeline["boundaries"][1]["position"] == 100
    assert timeline["boundaries"][2]["timestamp_ns"] == 300
    assert timeline["boundaries"][2]["position"] == 50


def test_frame_jank_view_separates_active_and_presentation_fps() -> None:
    evidence = EvidenceRecord(
        evidence_id="ev-frame-rate",
        trace_id="trace",
        tool="inspect_frame_jank",
        summary="frame rate",
        data={
            "available": True,
            "target_process": {"ipid": 10},
            "interval": {
                "start_ns": 1_000_000_000,
                "end_ns": 6_000_000_000,
                "duration_ms": 5000.0,
            },
            "cadence": {
                "dominant_refresh_rate_hz": 60.0,
                "dominant_frame_budget_ms": 16.666667,
                "dynamic_refresh_detected": False,
                "segments": [
                    {
                        "start_ns": 1_000_000_000,
                        "end_ns": 1_100_000_000,
                        "refresh_rate_hz": 60.0,
                        "frame_budget_ms": 16.666667,
                        "sample_count": 6,
                        "source": "expected-duration",
                        "confidence": 0.95,
                    }
                ],
            },
            "metrics": {
                "application_actual_frames": 6,
                "mapped_presented_frames": 0,
                "expected_frame_slots": 6,
                "problem_interval_frame_count": 50,
                "problem_interval_fps": 10.0,
                "problem_interval_fps_source": (
                    "global-render-service-output"
                ),
                "problem_interval_target_attributed": False,
                "render_service_interval": {
                    "actual_frames": 50,
                    "expected_frames": 52,
                    "dropped_frame_candidates": 2,
                },
                "application_production_fps": 60.0,
                "effective_presentation_fps": None,
                "interval_average_fps_meaningful": False,
                "application_frame_duration_ms": {
                    "p50": 1.0,
                    "p95": 4.0,
                    "p99": 5.0,
                    "maximum": 6.0,
                },
            },
            "render_architecture": {
                "surface_identity_available": False,
                "frame_producer_ui_mismatch": {
                    "status": "strong-ui-candidate-differs-from-frame-owner"
                },
            },
        },
    )

    view = ReportProjectionBuilder._build_frame_jank_view(
        evidence_index=EvidenceIndex([evidence]),
        database_path=None,
    )

    assert view["application_production_fps"] == 60.0
    assert view["effective_presentation_fps"] is None
    assert view["presentation_fps_available"] is False
    assert view["whole_window_frame_rate"] == 10.0
    assert view["problem_interval_fps"] == 10.0
    assert view["problem_interval_frame_count"] == 50
    assert view["problem_interval_fps_source"] == (
        "global-render-service-output"
    )
    assert view["render_service_actual_frames"] == 50
    assert view["render_service_dropped_frame_candidates"] == 2
    assert view["problem_interval_duration_ms"] == 5000.0
    assert view["whole_window_rate_meaningful"] is False
    assert view["application_fps_label"] == "平台/包装层活动产帧 FPS"
    assert view["segments"][0]["frame_count"] == 6


def test_perf_focus_excludes_zero_sample_threads_and_prefers_hot_worker(
    tmp_path,
) -> None:
    database_path = tmp_path / "perf-focus.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE thread(itid INT, tid INT, name TEXT);
            CREATE TABLE sched_slice(itid INT, ts INT, dur INT);
            INSERT INTO thread VALUES
                (1, 100, 'main'),
                (2, 200, 'tp_0'),
                (3, 300, 'render_service');
            INSERT INTO sched_slice VALUES
                (1, 0, 10),
                (2, 0, 95),
                (3, 0, 100);
            """
        )
    evidence = EvidenceRecord(
        evidence_id="ev-perf-focus",
        trace_id="trace",
        tool="inspect_perf_profile",
        summary="perf",
        data={
            "event_profiles": [
                {
                    "event_type_id": 0,
                    "threads": [
                        {
                            "thread_id": 200,
                            "thread_name": "tp_0",
                            "sample_count": 940,
                            "event_count": 9400,
                            "sample_share": 0.94,
                        },
                        {
                            "thread_id": 100,
                            "thread_name": "main",
                            "sample_count": 60,
                            "event_count": 600,
                            "sample_share": 0.06,
                        },
                    ],
                }
            ]
        },
    )
    analysis = SimpleNamespace(
        perf=SimpleNamespace(
            thread_ids=[100, 200, 300],
            interval_start_ns=0,
            interval_end_ns=100,
            events=[SimpleNamespace(event_type_id=0)],
        ),
        cold_start=None,
        completion_latency=SimpleNamespace(
            resolved_process=SimpleNamespace(main_tid=100),
            phases=[],
        ),
        problem_interval=None,
    )

    focus = ReportProjectionBuilder._build_perf_focus(
        analysis=analysis,
        evidence_index=EvidenceIndex([evidence]),
        database_path=database_path,
    )

    assert focus["primary_thread_id"] == 200
    assert set(focus["selected_thread_ids"]) == {100, 200}
    assert 300 not in focus["selected_thread_ids"]


def test_perf_thread_state_profiles_use_full_problem_interval(tmp_path) -> None:
    database_path = tmp_path / "thread-state-profiles.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, pid INT, name TEXT);
            CREATE TABLE thread(itid INT, tid INT, ipid INT, name TEXT);
            CREATE TABLE thread_state(
                id INT, itid INT, ts INT, dur INT, state TEXT
            );
            CREATE TABLE sched_slice(
                id INT, itid INT, ts INT, dur INT, cpu INT, priority INT
            );
            INSERT INTO process VALUES (10, 1000, 'com.example.app');
            INSERT INTO thread VALUES
                (1, 100, 10, 'main'),
                (2, 200, 10, 'UserAgent');
            INSERT INTO thread_state VALUES
                (1, 1, 0, 10000000, 'Running'),
                (2, 1, 10000000, 90000000, 'S'),
                (3, 2, 0, 80000000, 'Running'),
                (4, 2, 80000000, 20000000, 'R');
            INSERT INTO sched_slice VALUES
                (1, 1, 0, 10000000, 2, 120),
                (2, 2, 0, 50000000, 4, 110),
                (3, 2, 50000000, 30000000, 6, 110);
            """
        )

    profiles = ReportProjectionBuilder._build_perf_thread_state_profiles(
        database_path=database_path,
        interval_start_ns=0,
        interval_end_ns=100_000_000,
        perf_focus={
            "threads": [
                {
                    "thread_id": 100,
                    "thread_name": "main",
                    "roles": ["进程主线程"],
                    "reasons": ["required"],
                    "sample_count": 0,
                },
                {
                    "thread_id": 200,
                    "thread_name": "UserAgent",
                    "roles": ["UI / 事件循环线程"],
                    "reasons": ["framework"],
                    "sample_count": 80,
                },
            ]
        },
    )

    assert profiles[0]["profile_key"] == "combined"
    assert profiles[0]["mode"] == "comparison"
    by_key = {item["profile_key"]: item for item in profiles[1:]}
    main_states = {
        item["key"]: item for item in by_key["100"]["states"]
    }
    ui_states = {
        item["key"]: item for item in by_key["200"]["states"]
    }
    assert main_states["running_ms"]["share"] == 0.1
    assert main_states["sleeping_ms"]["share"] == 0.9
    assert ui_states["running_ms"]["share"] == 0.8
    assert ui_states["runnable_ms"]["share"] == 0.2
    assert by_key["200"]["cpu_migrations"] == 1
    assert by_key["200"]["top_cpu"]["cpu"] == 4
    assert by_key["100"]["sample_count"] == 0


def test_completion_perf_scope_uses_main_and_phase_threads() -> None:
    analysis = SimpleNamespace(
        perf=SimpleNamespace(thread_ids=[100, 200, 999]),
        cold_start=None,
        completion_latency=SimpleNamespace(
            resolved_process=SimpleNamespace(main_tid=100),
            phases=[
                SimpleNamespace(
                    critical_threads=[
                        SimpleNamespace(tid=200),
                        SimpleNamespace(tid=300),
                    ]
                )
            ],
        ),
    )

    selected, reason = (
        ReportProjectionBuilder._relevant_perf_thread_scope(analysis)
    )

    assert selected == [100, 200]
    assert "关键阶段" in reason


def test_completion_timeline_keeps_main_ui_causal_and_render_threads() -> None:
    def thread(itid, name, running_ms, ipid=10):
        return SimpleNamespace(
            itid=itid,
            tid=1000 + itid,
            ipid=ipid,
            thread_name=name,
            process_name=(
                "com.example.app" if ipid == 10 else "render_service"
            ),
            state_breakdown=SimpleNamespace(running_ms=running_ms),
            wakeup_chain=[],
        )

    completion = SimpleNamespace(
        resolved_process=SimpleNamespace(
            ipid=10,
            name="com.example.app",
            main_itid=11,
        ),
        phases=[
            SimpleNamespace(
                name="response",
                critical_threads=[
                    thread(11, "main", 8),
                    thread(103, "UserAgent", 88),
                    thread(4, "render_service", 20, ipid=30),
                ]
            ),
            SimpleNamespace(
                name="post-response",
                critical_threads=[
                    thread(11, "main", 7),
                    thread(103, "UserAgent", 670),
                ]
            ),
        ],
    )
    completion.phases[0].critical_threads[0].wakeup_chain = [
        SimpleNamespace(
            depth=1,
            waker_itid=200,
            waker_thread="blocking-worker",
            waker_process="com.example.app",
        )
    ]
    completion.phases[1].critical_threads[1].wakeup_chain = [
        SimpleNamespace(
            depth=1,
            waker_itid=201,
            waker_thread="pooled-worker",
            waker_process="com.example.app",
        ),
        SimpleNamespace(
            depth=2,
            waker_itid=202,
            waker_thread="app-upstream-worker",
            waker_process="com.example.app",
        ),
        SimpleNamespace(
            depth=2,
            waker_itid=203,
            waker_thread="remote-system-ipc",
            waker_process="system_service",
        ),
    ]

    specs = ReportProjectionBuilder._completion_timeline_thread_specs(
        completion
    )

    by_itid = {item["itid"]: item for item in specs}
    assert "进程主线程" in by_itid[11]["roles"]
    assert "UI / 事件循环候选" in by_itid[103]["roles"]
    assert "直接唤醒 / 依赖线程" in by_itid[200]["roles"]
    assert "直接唤醒 / 依赖线程" in by_itid[201]["roles"]
    assert "应用内上游依赖线程" in by_itid[202]["roles"]
    assert 203 not in by_itid
    assert "目标帧映射 RenderService" in by_itid[4]["roles"]


def test_completion_timeline_adds_cpu_running_supplements(tmp_path) -> None:
    database_path = tmp_path / "timeline.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, name TEXT);
            CREATE TABLE thread(itid INT, tid INT, ipid INT, name TEXT);
            CREATE TABLE sched_slice(itid INT, ts INT, dur INT);
            INSERT INTO process VALUES (10, 'com.example.app');
            INSERT INTO thread VALUES
                (11, 1011, 10, 'main'),
                (103, 1103, 10, 'UserAgent'),
                (301, 1301, 10, 'QThread-heavy'),
                (302, 1302, 10, 'worker-heavy');
            INSERT INTO sched_slice VALUES
                (11, 0, 10),
                (103, 0, 80),
                (301, 0, 70),
                (302, 0, 60);
            """
        )
    completion = SimpleNamespace(
        resolved_process=SimpleNamespace(ipid=10),
        input_boundary=SimpleNamespace(timestamp_ns=0),
        response_boundary=SimpleNamespace(timestamp_ns=50),
        completion_boundary=SimpleNamespace(timestamp_ns=100),
    )
    required = [
        {
            "itid": 11,
            "thread_name": "main",
            "process_name": "com.example.app",
            "roles": ["进程主线程"],
            "reasons": ["required"],
            "running_ms": 0,
            "order": 0,
        },
        {
            "itid": 103,
            "thread_name": "UserAgent",
            "process_name": "com.example.app",
            "roles": ["UI / 事件循环线程"],
            "reasons": ["required"],
            "running_ms": 0,
            "order": 1,
        },
    ]

    specs = ReportProjectionBuilder._completion_cpu_supplement_specs(
        completion=completion,
        database_path=database_path,
        required_specs=required,
    )

    by_itid = {item["itid"]: item for item in specs}
    assert 11 in by_itid
    assert 103 in by_itid
    assert by_itid[301]["roles"] == ["CPU 高贡献补选"]
    assert by_itid[302]["roles"] == ["CPU 高贡献补选"]


def test_callstack_lane_downsamples_across_the_full_window(tmp_path) -> None:
    database_path = tmp_path / "dense-callstack.db"
    event_count = 10_050
    interval_end_ns = event_count * 100 + 100
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE callstack("
            "id INT, ts INT, dur INT, name TEXT, cat TEXT, depth INT, "
            "parent_id INT, child_callid INT, callid INT)"
        )
        connection.executemany(
            "INSERT INTO callstack VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    index,
                    index * 100,
                    50,
                    f"slice-{index}",
                    "app",
                    index % 4,
                    0,
                    0,
                    11,
                )
                for index in range(event_count)
            ],
        )

    lane = ReportProjectionBuilder()._read_main_thread_slice_lane(
        database_path=database_path,
        target_itid=11,
        process_name="com.example.app",
        start_ns=0,
        end_ns=interval_end_ns,
    )

    assert lane is not None
    assert lane["downsampled"] is True
    assert lane["source_event_count"] == event_count
    assert len(lane["items"]) < 10_000
    assert min(item["start_ms"] for item in lane["items"]) < 10
    assert max(
        item["start_ms"] + item["duration_ms"]
        for item in lane["items"]
    ) > (
        interval_end_ns / 1_000_000 * 0.95
    )
    assert "full-window balanced" in lane["sublabel"]


def test_unproven_completion_uses_problem_interval_as_observation_window(
    tmp_path,
) -> None:
    database_path = tmp_path / "observation.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, name TEXT);
            CREATE TABLE thread(itid INT, tid INT, ipid INT, name TEXT);
            CREATE TABLE sched_slice(
                id INT, ts INT, dur INT, cpu INT, itid INT,
                ipid INT, priority INT, end_state TEXT
            );
            INSERT INTO process VALUES (10, 'com.example.app');
            INSERT INTO thread VALUES (11, 1011, 10, 'main');
            INSERT INTO sched_slice VALUES
                (1, 10, 20, 0, 11, 10, 100, 'R');
            """
        )
    completion = SimpleNamespace(
        resolved_process=SimpleNamespace(
            ipid=10,
            name="com.example.app",
            main_itid=11,
        ),
        input_boundary=SimpleNamespace(
            timestamp_ns=10,
            source="explicit-time-range",
            confidence=1.0,
        ),
        response_boundary=None,
        completion_boundary=None,
        phases=[],
    )
    analysis = SimpleNamespace(
        completion_latency=completion,
        problem_interval=SimpleNamespace(
            start_boundary=SimpleNamespace(timestamp_ns=10),
            end_boundary=SimpleNamespace(
                timestamp_ns=100,
                source="explicit-time-range",
            ),
        ),
    )

    timeline = ReportProjectionBuilder()._build_completion_timeline(
        analysis=analysis,
        evidence_index=EvidenceIndex([]),
        database_path=database_path,
    )

    assert timeline["available"] is True
    assert timeline["end_ns"] == 100
    assert timeline["title"] == "未完成操作观察窗口泳道"
    assert timeline["boundaries"][-1]["kind"] == "observation-end"


def test_actionable_hotspots_prefer_bottom_up_and_skip_generic_symbols() -> None:
    record = EvidenceRecord(
        evidence_id="ev-perf",
        trace_id="trace",
        tool="inspect_perf_profile",
        summary="perf",
        data={
            "event_profiles": [
                {
                    "event_type_id": 0,
                    "bottom_up_diagnostics": [
                        {
                            "application_function": ".plt",
                            "self_samples": 20,
                            "self_event_share": 0.20,
                        },
                        {
                            "application_function": "BusinessLayout::update()",
                            "self_samples": 12,
                            "self_event_share": 0.12,
                            "representative_reverse_path": [
                                "BusinessLayout::update()",
                                "PostedEventCallback",
                            ],
                            "optimization_direction": "batch updates",
                        },
                    ],
                }
            ]
        },
    )
    event = SimpleNamespace(
        event_type_id=0,
        sample_count=100,
        hotspots=[
            SimpleNamespace(
                symbol="dingtalk.hap+offsets",
                file_path="/app/dingtalk.hap",
                layer="application",
                self_samples=0,
                inclusive_samples=100,
                inclusive_share=1.0,
                critical_path_relevance="generic",
            ),
            SimpleNamespace(
                symbol="SpecificTopDownCall",
                file_path="/app/dingtalk.hap",
                layer="application",
                self_samples=3,
                inclusive_samples=10,
                inclusive_share=0.1,
                critical_path_relevance="specific",
            ),
        ],
    )

    hotspots = ReportProjectionBuilder._actionable_perf_hotspots(
        event=event,
        evidence_index=EvidenceIndex([record]),
    )

    assert hotspots[0]["symbol"] == "BusinessLayout::update()"
    assert hotspots[0]["aggregation_source"] == "bottom-up"
    assert ".plt" not in {item["symbol"] for item in hotspots}
    assert "dingtalk.hap+offsets" not in {
        item["symbol"] for item in hotspots
    }


def test_actionable_hotspots_show_representative_symbol_for_aggregation() -> None:
    record = EvidenceRecord(
        evidence_id="ev-font",
        trace_id="trace",
        tool="inspect_perf_profile",
        summary="perf",
        data={
            "event_profiles": [
                {
                    "event_type_id": 0,
                    "bottom_up_diagnostics": [
                        {
                            "operation": (
                                "Font matching, fallback and text shaping"
                            ),
                            "category": "font-matching-text-shaping",
                            "self_samples": 95,
                            "self_event_share": 0.117,
                            "supporting_symbols": [
                                {
                                    "symbol": "FcFontMatch",
                                    "sample_count": 95,
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    event = SimpleNamespace(
        event_type_id=0,
        sample_count=100,
        hotspots=[],
    )

    hotspots = ReportProjectionBuilder._actionable_perf_hotspots(
        event=event,
        evidence_index=EvidenceIndex([record]),
    )

    assert hotspots[0]["symbol"] == "FcFontMatch"
    assert hotspots[0]["aggregation_group"] == (
        "Font matching, fallback and text shaping"
    )
