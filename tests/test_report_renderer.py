from __future__ import annotations

import sqlite3
from pathlib import Path

from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
    EvidenceRecord,
    ScenarioType,
)
from trace_agent.report import ReportRenderer


def test_timeline_panel_is_available_to_frame_jank_reports() -> None:
    template_path = (
        Path(__file__).parents[1]
        / "src"
        / "trace_agent"
        / "report"
        / "templates"
        / "report.html.j2"
    )
    template = template_path.read_text(encoding="utf-8")

    assert "{% if view.timeline.available %}" in template
    assert (
        "{% if analysis.cold_start or analysis.completion_latency %}"
        not in template
    )
    assert 'role="tablist" aria-label="相关线程 Perf 视图"' in template
    assert 'data-perf-state-profile="' in template
    assert "问题区间线程调度状态" in template
    assert "profile.dataset.perfStateProfile !== key" in template


def test_html_report_renders_cold_start_timeline_and_evidence(
    tmp_path,
) -> None:
    trace_path = tmp_path / "startup.htrace"
    trace_path.write_bytes(b"trace")
    database_path = tmp_path / "startup.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE process(ipid INT, pid INT, name TEXT);
            CREATE TABLE thread(itid INT, tid INT, name TEXT, ipid INT);
            CREATE TABLE instant(
                id INT, ts INT, name TEXT, ref INT, wakeup_from INT
            );
            CREATE TABLE callstack(
                id INT, ts INT, dur INT, callid INT, cat TEXT,
                name TEXT, depth INT, parent_id INT, child_callid INT
            );
            CREATE TABLE thread_state(
                id INT, ts INT, dur INT, cpu INT, itid INT,
                tid INT, pid INT, state TEXT, arg_setid INT
            );
            CREATE TABLE sched_slice(
                id INT, ts INT, dur INT, ts_end INT, cpu INT,
                itid INT, ipid INT, end_state TEXT,
                priority INT, arg_setid INT
            );
            INSERT INTO process VALUES (10, 100, 'com.example.app');
            INSERT INTO thread VALUES
                (11, 100, 'main', 10),
                (12, 101, 'OS_TaskWorker', 10);
            INSERT INTO instant VALUES
                (400, 1130000000, 'sched_wakeup', 11, 12);
            INSERT INTO callstack VALUES
                (100, 1000000000, 250000000, 11, 'app',
                 'MainThreadRoot', 0, 4294967295, 11),
                (101, 1050000000, 150000000, 11, 'app',
                 'MainThreadChild', 1, 100, 11),
                (102, 1100000000, 10000000, 11, 'js',
                 'DeepMainThreadSlice', 7, 101, 11),
                (103, 1080000000, 50000000, 12, 'app',
                 'H:libbusiness_kmp.so|I39', 2, 4294967295, 12);
            INSERT INTO thread_state VALUES
                (200, 1000000000, 80000000, 6, 11, 100, 100,
                 'Running', 0),
                (201, 1080000000, 20000000, 6, 11, 100, 100,
                 'R', 0),
                (202, 1100000000, 30000000, 6, 11, 100, 100,
                 'S', 0),
                (203, 1080000000, 50000000, 4, 12, 101, 100,
                 'Running', 0);
            INSERT INTO sched_slice VALUES
                (300, 1000000000, 80000000, 1080000000, 6,
                 11, 10, 'R', 53, 0),
                (301, 1140000000, 40000000, 1180000000, 4,
                 11, 10, 'S', 53, 0),
                (302, 1080000000, 50000000, 1130000000, 4,
                 12, 10, 'S', 51, 0);
            """
        )
    request = AnalyzeRequest(
        trace_id="html-report",
        trace_path=trace_path,
        scenario_type=ScenarioType.COLD_START,
        scenario="应用冷启动",
        symptom="首帧较慢",
        output_dir=tmp_path,
    )
    analysis = AnalysisResult.model_validate(
        {
            "summary": "首帧链路主要耗时集中在 UI Ability 启动。",
            "cold_start": {
                "resolved_process": {
                    "name": "com.example.app",
                    "pid": 100,
                    "ipid": 10,
                    "main_tid": 100,
                    "main_itid": 11,
                    "selection_reason": "Trace 内发现新进程启动链",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "cold_start_proven": True,
                "classification_reason": "进程创建与首帧链连续",
                "start_boundary": {
                    "name": "启动请求",
                    "timestamp_ns": 1_000_000_000,
                    "source": "callstack",
                    "source_id": "callstack:1",
                    "confidence": 0.88,
                    "evidence_ids": ["ev-0001"],
                },
                "end_boundary": {
                    "name": "应用首帧",
                    "timestamp_ns": 1_250_000_000,
                    "source": "callstack",
                    "source_id": "callstack:2",
                    "confidence": 0.84,
                    "evidence_ids": ["ev-0001"],
                },
                "total_duration_ms": 250,
                "presentation_boundary": {
                    "name": "RS 首帧呈现完成",
                    "timestamp_ns": 1_280_000_000,
                    "source": "frame_slice",
                    "source_id": "frame_slice:3",
                    "kind": "presentation",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-0001"],
                },
                "presentation_duration_ms": 280,
                "stages": [
                    {
                        "name": "UI Ability Launching",
                        "start_ns": 1_020_000_000,
                        "end_ns": 1_240_000_000,
                        "duration_ms": 220,
                        "critical_threads": [
                            {
                                "process_name": "com.example.app",
                                "thread_name": "main",
                                "pid": 100,
                                "ipid": 10,
                                "tid": 100,
                                "itid": 11,
                                "state_breakdown": {
                                    "running_ms": 180,
                                    "runnable_ms": 10,
                                    "sleeping_ms": 30,
                                    "uninterruptible_io_ms": 0,
                                    "uninterruptible_other_ms": 0,
                                    "other_ms": 0,
                                },
                                "cpu_distribution": [
                                    {
                                        "cpu": 6,
                                        "running_ms": 180,
                                        "share": 1,
                                        "schedule_slices": 5,
                                    }
                                ],
                                "cpu_migrations": 0,
                                "schedule_slices": 5,
                                "longest_running_ms": 80,
                                "longest_runnable_ms": 5,
                                "longest_sleep_ms": 20,
                                "priority": {
                                    "observed_values": [53],
                                    "dominant_value": 53,
                                    "interpretation": "保留原始优先级值",
                                },
                                "contention_intervals": [],
                                "wakeup_chain": [],
                                "diagnosis": "cpu-bound",
                                "assessment": "主线程以 Running 为主",
                                "confidence": 0.8,
                                "evidence_ids": ["ev-0001"],
                            }
                        ],
                        "assessment": "关键阶段",
                        "evidence_ids": ["ev-0001"],
                    }
                ],
                "critical_path_summary": "UI Ability 阶段控制首帧完成。",
                "evidence_ids": ["ev-0001"],
            },
            "findings": [],
            "limitations": [],
        }
    )
    evidence = [
        EvidenceRecord(
            evidence_id="ev-0001",
            trace_id="html-report",
            tool="inspect_cold_start_timeline",
            summary="冷启动候选时间线",
            data={
                "target_process": {
                    "ipid": 10,
                    "pid": 100,
                    "process_name": "com.example.app",
                },
                "timeline_events": [
                    {
                        "source": "callstack",
                        "source_id": 1,
                        "ts": 1_000_000_000,
                        "end_ns": 1_040_000_000,
                        "dur_ns": 40_000_000,
                        "name": "Launch </script>",
                        "depth": 0,
                        "ipid": 10,
                        "pid": 100,
                        "process_name": "com.example.app",
                        "itid": 11,
                        "tid": 100,
                        "thread_name": "main",
                        "candidate_reasons": [
                            "target_process_shallow_slice"
                        ],
                    }
                ],
                "frame_candidates": [
                    {
                        "id": 2,
                        "ts": 1_240_000_000,
                        "end_ns": 1_250_000_000,
                        "dur": 10_000_000,
                        "ipid": 10,
                        "type_desc": "actural",
                    }
                ],
                "frame_links": [
                    {
                        "map_id": 1,
                        "src_row": 2,
                        "dst_row": 3,
                        "src_ipid": 10,
                        "src_ts": 1_240_000_000,
                        "src_dur_ns": 10_000_000,
                        "src_process_name": "com.example.app",
                        "dst_ipid": 20,
                        "dst_ts": 1_260_000_000,
                        "dst_dur_ns": 20_000_000,
                        "dst_process_name": "render_service",
                        "dst_type_desc": "actural",
                    }
                ],
            },
        )
    ]
    output_path = tmp_path / "report.html"

    ReportRenderer().render(
        request=request,
        analysis=analysis,
        evidence=evidence,
        output_path=output_path,
        database_path=database_path,
    )

    html = output_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "冷启动性能分析" in html
    assert "跨进程冷启动泳道" in html
    assert "应用首帧完成" in html
    assert "屏幕呈现完成" in html
    assert "trace-canvas" in html
    assert "createCanvasTimeline" in html
    assert "DeepMainThreadSlice" in html
    assert "OS_TaskWorker" in html
    assert "H:libbusiness_kmp.so|I39" in html
    assert '"key":"cold-causal-12-scheduling"' in html
    assert '"key":"cold-causal-12-callstack"' in html
    assert '"source":"causal-wakeup-source-0-0"' in html
    assert '"wakeup-source": ["#ffe1ad"' in html
    assert "depth 0–7" in html
    assert 'id="timeline-depth"' in html
    assert "同名 Slice 同色" in html
    assert "function callstackColors" in html
    assert "Thread Scheduling" in html
    assert 'aria-label="CPU 分布"' in html
    assert "66.7%" in html
    assert "CPU 6" in html
    assert "function cpuColors" in html
    assert "function createPerfFlame" in html
    assert "function renderPerfVisuals" in html
    assert "height: clamp(360px, 68vh, 680px);" in html
    assert "overflow-y: auto;" in html
    assert "shell.scrollTop = shell.scrollHeight;" in html
    assert (
        "[data-startup-module-row][hidden] {\n"
        "      display: none !important;"
    ) in html
    assert "const maxVisibleDepth" not in html
    assert "const visibleDepth = treeDepth(state.current)" in html
    assert "thread-state-200" not in html
    assert "thread-state-201" in html
    assert "thread-state-202" not in html
    assert html.index('"key":"thread-scheduling"') < html.index(
        '"key":"main-thread-callstack"'
    )
    assert "render_service" in html
    assert "ev-0001" in html
    assert "\\u003c/script\\u003e" in html


def test_html_report_renders_completion_latency_swimlane(tmp_path) -> None:
    trace_path = tmp_path / "completion.htrace"
    trace_path.write_bytes(b"trace")
    request = AnalyzeRequest(
        trace_id="completion-report",
        trace_path=trace_path,
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="点击进入详情并完成渲染",
        symptom="页面稳定较慢",
        problem_duration_ms=3500,
        output_dir=tmp_path,
    )
    analysis = AnalysisResult.model_validate(
        {
            "summary": "响应后仍有较长渲染阶段。",
            "completion_latency": {
                "resolved_process": {
                    "name": "com.example.app",
                    "pid": 100,
                    "ipid": 10,
                    "main_tid": 100,
                    "main_itid": 11,
                    "selection_reason": "应用帧归属目标进程",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-candidates"],
                },
                "input_boundary": {
                    "name": "最后点击点",
                    "timestamp_ns": 1_000_000_000,
                    "source": "callstack",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-candidates"],
                },
                "response_boundary": {
                    "name": "首个响应帧",
                    "timestamp_ns": 2_000_000_000,
                    "source": "frame_slice",
                    "confidence": 0.7,
                    "evidence_ids": ["ev-candidates"],
                },
                "completion_boundary": {
                    "name": "业务完成",
                    "timestamp_ns": 4_500_000_000,
                    "source": "application-marker",
                    "confidence": 0.9,
                    "evidence_ids": ["ev-candidates"],
                },
                "completion_proven": True,
                "completion_semantics": "应用唯一标记定义完成。",
                "response_latency_ms": 1000,
                "post_response_duration_ms": 2500,
                "completion_latency_ms": 3500,
                "phases": [
                    {
                        "name": "response",
                        "start_ns": 1_000_000_000,
                        "end_ns": 2_000_000_000,
                        "duration_ms": 1000,
                        "assessment": "首个响应阶段",
                        "evidence_ids": ["ev-phases"],
                    },
                    {
                        "name": "post-response",
                        "start_ns": 2_000_000_000,
                        "end_ns": 4_500_000_000,
                        "duration_ms": 2500,
                        "assessment": "业务完成阶段",
                        "evidence_ids": ["ev-phases"],
                    },
                ],
                "critical_path_summary": "输入、响应帧、业务完成。",
                "evidence_ids": ["ev-candidates", "ev-phases"],
            },
            "findings": [],
            "limitations": [],
        }
    )
    evidence = [
        EvidenceRecord(
            evidence_id="ev-candidates",
            trace_id="completion-report",
            tool="inspect_completion_latency_candidates",
            summary="完成时延候选",
            data={
                "target_ipid": 10,
                "app_frames": [
                    {
                        "id": 20,
                        "ts": 1_900_000_000,
                        "end_ns": 2_000_000_000,
                        "type_desc": "actural",
                    }
                ],
                "frame_links": [
                    {
                        "src_row": 20,
                        "dst_row": 21,
                        "dst_ts": 2_010_000_000,
                        "dst_end_ns": 2_030_000_000,
                        "dst_process_name": "render_service",
                    }
                ],
            },
        ),
        EvidenceRecord(
            evidence_id="ev-phases",
            trace_id="completion-report",
            tool="inspect_completion_latency_phases",
            summary="完成时延阶段",
            data={"target_process": {"ipid": 10}},
        ),
    ]
    output_path = tmp_path / "completion-report.html"

    ReportRenderer().render(
        request=request,
        analysis=analysis,
        evidence=evidence,
        output_path=output_path,
    )

    html = output_path.read_text(encoding="utf-8")
    assert "完成时延关键路径泳道" in html
    assert "输入、首个响应、业务完成" in html
    assert '"key":"latency-phases"' in html
    assert '"key":"app-frames"' in html
    assert '"key":"render-frames"' in html
    assert html.index('"label":"输入"') < html.index('"label":"首个响应"')
    assert html.index('"label":"首个响应"') < html.index('"label":"完成"')
