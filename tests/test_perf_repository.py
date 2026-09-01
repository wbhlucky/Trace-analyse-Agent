from __future__ import annotations

import sqlite3

from trace_agent.database import PerfAnalysisRepository, PerfTraceRepository


def test_bottom_up_descends_past_plt_to_specific_application_function() -> None:
    frames = [
        {
            "symbol": "BusinessController::refreshConversation()",
            "file_path": "/app/dingtalk.hap",
        },
        {
            "symbol": ".plt",
            "file_path": "/app/dingtalk.hap",
        },
    ]

    result = PerfAnalysisRepository._classify_bottom_up_operation(
        frames,
        application_owner=frames[-1],
    )

    assert result is not None
    assert result["application_function"] == (
        "BusinessController::refreshConversation()"
    )


def test_bottom_up_aggregates_font_matching_and_text_shaping() -> None:
    frames = [
        {
            "symbol": "QTextEngine::shapeTextWithHarfbuzzNG(...) const",
            "file_path": "/system/lib64/libQt5Gui.so",
        },
        {
            "symbol": "FcFontMatch",
            "file_path": "/system/lib64/libfontconfig.so",
        },
    ]

    result = PerfAnalysisRepository._classify_bottom_up_operation(
        frames,
        application_owner=frames[0],
    )

    assert result is not None
    assert result["category"] == "font-matching-text-shaping"
    assert result["matched_symbol"] == "FcFontMatch"


def test_perf_projection_builds_thread_distribution_and_call_tree(
    tmp_path,
) -> None:
    database_path = tmp_path / "perf.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE perf_sample(
                id INT, callchain_id INT, event_count INT,
                thread_id INT, event_type_id INT,
                timestamp_trace INT, cpu_id INT
            );
            CREATE TABLE perf_thread(
                thread_id INT, process_id INT, thread_name TEXT
            );
            CREATE TABLE perf_callchain(
                id INT, callchain_id INT, depth INT,
                file_id INT, name INT
            );
            CREATE TABLE perf_files(
                id INT, file_id INT, serial_id INT,
                symbol TEXT, path TEXT
            );
            CREATE TABLE data_dict(id INT, data TEXT);

            INSERT INTO perf_thread VALUES
                (100, 42, 'main'),
                (101, 42, 'worker');
            INSERT INTO perf_sample VALUES
                (1, 10, 100, 100, 0, 1100, 6),
                (2, 11, 50, 101, 0, 1200, 4);
            INSERT INTO data_dict VALUES
                (1, 'ProcessRoot'),
                (2, 'LoadModule'),
                (3, 'WorkerTask');
            INSERT INTO perf_files VALUES
                (1, 1, 0, 'ProcessRoot', '/system/lib/libexample.so');
            INSERT INTO perf_callchain VALUES
                (1, 10, 0, 1, 1),
                (2, 10, 1, 1, 2),
                (3, 11, 0, 1, 1),
                (4, 11, 1, 1, 3);
            """
        )

    projection = PerfTraceRepository(database_path).project(
        interval_start_ns=1000,
        interval_end_ns=1300,
        event_type_id=0,
        process_ids=[42],
    )

    assert projection.sample_count == 2
    assert projection.total_event_count == 150
    assert [item["thread_name"] for item in projection.threads] == [
        "main",
        "worker",
    ]
    assert projection.threads[0]["sample_share"] == 0.5
    assert projection.modules[0]["name"] == "libexample.so"
    assert projection.modules[0]["sample_count"] == 2
    assert projection.modules[0]["event_share"] == 1
    assert projection.modules[0]["representative_symbols"][0][
        "symbol"
    ] == "ProcessRoot"
    assert projection.flame_root is not None
    thread_roots = projection.flame_root["children"]
    assert [item["thread_id"] for item in thread_roots] == [100, 101]
    assert [item["name"] for item in thread_roots] == [
        "TID 100 · main",
        "TID 101 · worker",
    ]
    assert thread_roots[0]["event_count"] == 100
    assert thread_roots[0]["children"][0]["name"] == "ProcessRoot"
    assert thread_roots[0]["children"][0]["children"][0]["name"] == (
        "LoadModule"
    )
    assert thread_roots[1]["event_count"] == 50
    assert thread_roots[1]["children"][0]["children"][0]["name"] == (
        "WorkerTask"
    )

    relevant_projection = PerfTraceRepository(database_path).project(
        interval_start_ns=1000,
        interval_end_ns=1300,
        event_type_id=0,
        process_ids=[42],
        thread_ids=[100],
    )

    assert relevant_projection.sample_count == 1
    assert [item["thread_id"] for item in relevant_projection.threads] == [
        100
    ]
    assert relevant_projection.flame_root is not None
    assert relevant_projection.flame_root["name"] == "TID 100 · main"
    assert {
        item["name"]
        for item in relevant_projection.flame_root["children"][0][
            "children"
        ]
    } == {"LoadModule"}


def test_perf_projection_preserves_deep_stack_height_by_default(
    tmp_path,
) -> None:
    database_path = tmp_path / "deep-perf.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE perf_sample(
                id INT, callchain_id INT, event_count INT,
                thread_id INT, event_type_id INT,
                timestamp_trace INT, cpu_id INT
            );
            CREATE TABLE perf_thread(
                thread_id INT, process_id INT, thread_name TEXT
            );
            CREATE TABLE perf_callchain(
                id INT, callchain_id INT, depth INT,
                file_id INT, name INT
            );
            CREATE TABLE perf_files(
                id INT, file_id INT, serial_id INT,
                symbol TEXT, path TEXT
            );
            CREATE TABLE data_dict(id INT, data TEXT);
            INSERT INTO perf_thread VALUES (100, 42, 'main');
            INSERT INTO perf_sample VALUES
                (1, 10, 100, 100, 0, 1100, 6);
            """
        )
        connection.executemany(
            "INSERT INTO data_dict VALUES (?, ?)",
            [(index + 1, f"Frame{index}") for index in range(35)],
        )
        connection.executemany(
            "INSERT INTO perf_callchain VALUES (?, 10, ?, 1, ?)",
            [
                (index + 1, index, index + 1)
                for index in range(35)
            ],
        )

    projection = PerfTraceRepository(database_path).project(
        interval_start_ns=1000,
        interval_end_ns=1300,
        event_type_id=0,
        process_ids=[42],
        thread_ids=[100],
    )

    assert projection.flame_max_depth == 35
    assert projection.min_node_share == 0.001
    assert projection.flame_root is not None
    node = projection.flame_root
    for index in range(35):
        assert len(node["children"]) == 1
        node = node["children"][0]
        assert node["name"] == f"Frame{index}"


def test_perf_analysis_builds_validated_window_profile(tmp_path) -> None:
    database_path = tmp_path / "perf-analysis.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE perf_report(
                id INT, report_type TEXT, report_value TEXT
            );
            CREATE TABLE perf_sample(
                id INT, callchain_id INT, event_count INT,
                thread_id INT, event_type_id INT,
                timestamp_trace INT, cpu_id INT
            );
            CREATE TABLE perf_thread(
                thread_id INT, process_id INT, thread_name TEXT
            );
            CREATE TABLE perf_callchain(
                id INT, callchain_id INT, depth INT,
                file_id INT, symbol_id INT, name INT
            );
            CREATE TABLE perf_files(
                id INT, file_id INT, serial_id INT,
                symbol TEXT, path TEXT
            );
            CREATE TABLE data_dict(id INT, data TEXT);

                INSERT INTO perf_report VALUES
                    (1, 'config_name', 'hw-cpu-cycles'),
                    (2, 'cmdline',
                     'hiperf record -f 1000 -a --call-stack dwarf --clockid monotonic');
            INSERT INTO perf_thread VALUES
                (100, 42, 'main'),
                (101, 42, 'worker'),
                (200, 99, 'other');
            INSERT INTO perf_sample VALUES
                (1, 10, 100, 100, 0, 1100, 6),
                (2, 11, 50, 101, 0, 1200, 4),
                (3, 12, 70, 200, 0, 1150, 3),
                (4, 13, 80, 100, 0, 1500, 6);
            INSERT INTO data_dict VALUES
                (1, 'RunScript'),
                (2, 'LoadModule'),
                (3, 'WorkerTask');
            INSERT INTO perf_files VALUES
                (1, 1, 0, 'RunScript', '/app/libmodule.so'),
                (2, 1, 1, 'LoadModule', '/app/libmodule.so'),
                (3, 1, 2, 'WorkerTask', '/app/libmodule.so');
            INSERT INTO perf_callchain VALUES
                (1, 10, 0, 1, 0, 1),
                (2, 10, 1, 1, 1, 2),
                (3, 11, 0, 1, 2, 3),
                (4, 11, 1, 1, 1, 2);
            """
        )

    result = PerfAnalysisRepository(database_path).inspect(
        interval_start_ns=1000,
        interval_end_ns=1300,
        process_ids=[42],
        thread_ids=[100, 101],
        max_hotspots=10,
        include_thread_profiles=True,
    )

    assert result["sample_count"] == 2
    assert result["total_callchain_frames"] == 4
    assert result["symbolized_callchain_frames"] == 4
    assert result["collection"]["scope"] == "system-wide"
    assert result["collection"]["sampling_frequency_hz"] == 1000
    event = result["event_profiles"][0]
    assert event["event_name"] == "hw-cpu-cycles"
    assert event["sample_count"] == 2
    assert event["total_event_count"] == 150
    load_module = next(
        item for item in event["hotspots"]
        if item["symbol"] == "LoadModule"
    )
    assert load_module["inclusive_samples"] == 2
    assert load_module["inclusive_event_count"] == 150
    assert [
        (item["thread_id"], item["sample_count"])
        for item in result["thread_profiles"]
    ] == [(100, 1), (101, 1)]


def test_perf_analysis_separates_process_roots_from_application_hotspots(
    tmp_path,
) -> None:
    database_path = tmp_path / "application-perf.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE perf_report(
                id INT, report_type TEXT, report_value TEXT
            );
            CREATE TABLE perf_sample(
                id INT, callchain_id INT, event_count INT,
                thread_id INT, event_type_id INT,
                timestamp_trace INT, cpu_id INT
            );
            CREATE TABLE perf_thread(
                thread_id INT, process_id INT, thread_name TEXT
            );
            CREATE TABLE perf_callchain(
                id INT, callchain_id INT, depth INT,
                file_id INT, symbol_id INT, name INT
            );
            CREATE TABLE perf_files(
                id INT, file_id INT, serial_id INT,
                symbol TEXT, path TEXT
            );
            CREATE TABLE data_dict(id INT, data TEXT);

            INSERT INTO perf_report VALUES
                (1, 'config_name', 'hw-cpu-cycles');
            INSERT INTO perf_thread VALUES (100, 42, 'main');
            INSERT INTO perf_sample VALUES
                (1, 10, 100, 100, 0, 1100, 6),
                (2, 11, 50, 100, 0, 1150, 6),
                (3, 10, 100, 100, 0, 1200, 6),
                (4, 10, 100, 100, 0, 1250, 6),
                (5, 11, 50, 100, 0, 1270, 6),
                (6, 11, 50, 100, 0, 1290, 6);
            INSERT INTO perf_files VALUES
                (1, 1, 0, 'appspawn+0xd24c', '/system/bin/appspawn'),
                (2, 2, 0, 'MainThread::Start',
                 '/system/lib64/libappkit.so'),
                (3, 3, 0, 'dingtalk.hap+0x100',
                 '/proc/42/root/data/storage/el1/bundle/dingtalk.hap'),
                (4, 3, 1, 'BusinessInit',
                 '/proc/42/root/data/storage/el1/bundle/dingtalk.hap'),
                (5, 4, 0, 'ArkRuntimeLeaf',
                 '/system/lib64/libark_jsruntime.so');
            INSERT INTO perf_callchain VALUES
                (1, 10, 0, 1, 0, 0),
                (2, 10, 1, 2, 0, 0),
                (3, 10, 2, 3, 0, 0),
                (4, 10, 3, 3, 1, 0),
                (5, 11, 0, 1, 0, 0),
                (6, 11, 1, 3, 0, 0),
                (7, 11, 2, 4, 0, 0);
            """
        )

    result = PerfAnalysisRepository(database_path).inspect(
        interval_start_ns=1000,
        interval_end_ns=1300,
        process_ids=[42],
        thread_ids=[100],
        max_hotspots=10,
    )

    event = result["event_profiles"][0]
    assert event["application_hotspot_status"] == "available"
    assert all(item["layer"] == "application" for item in event["hotspots"])
    assert "appspawn+0xd24c" not in {
        item["symbol"] for item in event["hotspots"]
    }
    business = next(
        item for item in event["hotspots"]
        if item["symbol"] == "BusinessInit"
    )
    assert business["self_samples"] == 3
    appspawn = next(
        item for item in event["context_hotspots"]
        if item["symbol"] == "appspawn+0xd24c"
    )
    assert appspawn["self_samples"] == 0
    assert appspawn["inclusive_samples"] == 6
    assert event["application_modules"][0]["sample_count"] == 6
    assert event["application_modules"][0]["sample_share"] == 1
    diagnostics = event["bottom_up_diagnostics"]
    assert diagnostics[0]["operation"] == (
        "Application function: BusinessInit"
    )
    assert diagnostics[0]["category"] == "application-function"
    assert diagnostics[0]["self_samples"] == 3
    assert diagnostics[0]["root_cause_candidate"] is True
    assert all(
        "dingtalk.hap+offsets" not in str(item)
        for item in diagnostics
    )
    assert all(
        "ArkRuntimeLeaf" not in str(item)
        for item in diagnostics
    )
