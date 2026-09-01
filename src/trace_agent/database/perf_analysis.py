from __future__ import annotations

import re
import shlex
import sqlite3
from collections import defaultdict
from pathlib import Path
from time import monotonic
from typing import Any, Iterable


class PerfAnalysisRepository:
    """Build a bounded, deterministic Perf profile for one problem window."""

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 30,
        max_samples: int = 200_000,
    ) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(f"Trace 数据库不存在：{database_path}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_samples <= 0:
            raise ValueError("max_samples 必须大于 0")
        self._database_path = database_path.resolve()
        self._timeout_seconds = timeout_seconds
        self._max_samples = max_samples

    def inspect(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        process_ids: Iterable[int],
        thread_ids: Iterable[int],
        max_hotspots: int = 20,
        include_thread_profiles: bool = False,
    ) -> dict[str, Any]:
        if interval_end_ns <= interval_start_ns:
            raise ValueError("Perf 分析结束时间必须晚于开始时间")
        if not 1 <= max_hotspots <= 50:
            raise ValueError("max_hotspots 必须在 1 到 50 之间")
        process_scope = sorted(set(int(value) for value in process_ids))
        thread_scope = sorted(set(int(value) for value in thread_ids))
        if not process_scope and not thread_scope:
            raise ValueError(
                "Perf 分析必须限定 process_ids 或 thread_ids，"
                "禁止把 system-wide 全量样本直接作为问题根因"
            )

        deadline = monotonic() + self._timeout_seconds
        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    10_000,
                )
                reports = self._read_reports(connection)
                samples = self._read_samples(
                    connection,
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                    process_ids=process_scope,
                    thread_ids=thread_scope,
                )
                if len(samples) > self._max_samples:
                    raise ValueError(
                        "Perf 窗口样本过多："
                        f"{len(samples)} > {self._max_samples}；"
                        "请进一步限定关键线程"
                    )
                frames = self._read_frames(connection, samples)
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise RuntimeError("Perf 确定性分析超时") from exc
            raise RuntimeError(f"Perf 确定性分析失败：{exc}") from exc

        collection, collection_limitations = self._collection_metadata(
            reports
        )
        return self._aggregate(
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
            process_scope=process_scope,
            thread_scope=thread_scope,
            samples=samples,
            frames=frames,
            collection=collection,
            limitations=collection_limitations,
            max_hotspots=max_hotspots,
            include_thread_profiles=include_thread_profiles,
        )

    @staticmethod
    def _read_reports(
        connection: sqlite3.Connection,
    ) -> list[tuple[str, str]]:
        if not PerfAnalysisRepository._has_table(
            connection, "perf_report"
        ):
            return []
        return [
            (str(report_type), str(report_value))
            for report_type, report_value in connection.execute(
                "SELECT report_type, report_value "
                "FROM perf_report ORDER BY id"
            )
            if report_type is not None and report_value is not None
        ]

    @staticmethod
    def _read_samples(
        connection: sqlite3.Connection,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        process_ids: list[int],
        thread_ids: list[int],
    ) -> list[dict[str, Any]]:
        has_threads = PerfAnalysisRepository._has_table(
            connection, "perf_thread"
        )
        if process_ids and not has_threads:
            raise ValueError(
                "Trace 缺少 perf_thread，无法按 OS PID 限定 Perf 样本"
            )
        filters = [
            "s.timestamp_trace >= ?",
            "s.timestamp_trace < ?",
        ]
        parameters: list[int] = [interval_start_ns, interval_end_ns]
        if process_ids:
            placeholders = ",".join("?" for _ in process_ids)
            filters.append(f"t.process_id IN ({placeholders})")
            parameters.extend(process_ids)
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            filters.append(f"s.thread_id IN ({placeholders})")
            parameters.extend(thread_ids)

        thread_cte = (
            "WITH threads AS ("
            "SELECT thread_id, MIN(process_id) AS process_id, "
            "MIN(thread_name) AS thread_name FROM perf_thread "
            "GROUP BY thread_id) "
            if has_threads
            else ""
        )
        process_column = "t.process_id" if has_threads else "NULL"
        thread_name_column = "t.thread_name" if has_threads else "NULL"
        thread_join = (
            "LEFT JOIN threads AS t ON t.thread_id = s.thread_id "
            if has_threads
            else ""
        )
        sql = (
            thread_cte
            + "SELECT s.id, s.callchain_id, s.event_count, "
            + "s.event_type_id, s.thread_id, s.cpu_id, "
            + f"{process_column}, {thread_name_column} "
            + "FROM perf_sample AS s "
            + thread_join
            + "WHERE "
            + " AND ".join(filters)
            + " ORDER BY s.id"
        )
        return [
            {
                "sample_id": int(row[0]),
                "callchain_id": int(row[1]),
                "event_count": int(row[2] or 0),
                "event_type_id": int(row[3]),
                "thread_id": int(row[4]),
                "cpu_id": int(row[5]) if row[5] is not None else None,
                "process_id": (
                    int(row[6]) if row[6] is not None else None
                ),
                "thread_name": str(row[7]) if row[7] else None,
            }
            for row in connection.execute(sql, parameters)
        ]

    @staticmethod
    def _read_frames(
        connection: sqlite3.Connection,
        samples: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not samples:
            return []
        if not PerfAnalysisRepository._has_table(
            connection, "perf_callchain"
        ):
            return []
        connection.execute(
            "CREATE TEMP TABLE scoped_perf_analysis_samples("
            "sample_id INTEGER, callchain_id INTEGER, "
            "event_type_id INTEGER, event_count INTEGER)"
        )
        connection.executemany(
            "INSERT INTO scoped_perf_analysis_samples VALUES (?, ?, ?, ?)",
            [
                (
                    row["sample_id"],
                    row["callchain_id"],
                    row["event_type_id"],
                    row["event_count"],
                )
                for row in samples
            ],
        )
        connection.execute(
            "CREATE INDEX scoped_perf_analysis_callchain_idx "
            "ON scoped_perf_analysis_samples(callchain_id)"
        )
        has_data_dict = PerfAnalysisRepository._has_table(
            connection, "data_dict"
        )
        has_files = PerfAnalysisRepository._has_table(
            connection, "perf_files"
        )
        callchain_columns = PerfAnalysisRepository._columns(
            connection, "perf_callchain"
        )
        file_columns = (
            PerfAnalysisRepository._columns(connection, "perf_files")
            if has_files
            else set()
        )
        has_exact_symbols = (
            has_files
            and "symbol_id" in callchain_columns
            and {"serial_id", "symbol"} <= file_columns
        )
        file_cte = (
            "WITH file_paths AS ("
            "SELECT file_id, MIN(path) AS path FROM perf_files "
            "WHERE path IS NOT NULL AND TRIM(path) != '' "
            "GROUP BY file_id) "
            if has_files
            else ""
        )
        exact_symbol_join = (
            "LEFT JOIN perf_files AS pf "
            "ON pf.file_id = c.file_id AND pf.serial_id = c.symbol_id "
            if has_exact_symbols
            else ""
        )
        if has_exact_symbols and has_data_dict:
            symbol_column = "COALESCE(NULLIF(pf.symbol, ''), d.data)"
        elif has_exact_symbols:
            symbol_column = "pf.symbol"
        else:
            symbol_column = "d.data" if has_data_dict else "NULL"
        if has_exact_symbols and has_files:
            path_column = "COALESCE(pf.path, f.path)"
        else:
            path_column = "f.path" if has_files else "NULL"
        resolved_column = (
            "CASE WHEN pf.symbol IS NOT NULL "
            "AND TRIM(pf.symbol) != '' THEN 1 ELSE 0 END"
            if has_exact_symbols
            else "0"
        )
        data_join = (
            "LEFT JOIN data_dict AS d ON d.id = c.name "
            if has_data_dict
            else ""
        )
        file_join = (
            "LEFT JOIN file_paths AS f ON f.file_id = c.file_id "
            if has_files
            else ""
        )
        sql = (
            file_cte
            + "SELECT s.sample_id, s.event_type_id, s.event_count, "
            + f"c.depth, {symbol_column}, {path_column}, "
            + f"{resolved_column} "
            + "FROM perf_callchain AS c "
            + "JOIN scoped_perf_analysis_samples AS s "
            + "ON s.callchain_id = c.callchain_id "
            + data_join
            + file_join
            + exact_symbol_join
            + "ORDER BY s.sample_id, c.depth, c.id"
        )
        return [
            {
                "sample_id": int(row[0]),
                "event_type_id": int(row[1]),
                "event_count": int(row[2] or 0),
                "depth": int(row[3]),
                "symbol": str(row[4]).strip() if row[4] else "[unknown]",
                "file_path": str(row[5]) if row[5] else None,
                "symbolized": bool(row[6]),
            }
            for row in connection.execute(sql)
        ]

    @staticmethod
    def _has_table(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone() is not None

    @staticmethod
    def _columns(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> set[str]:
        return {
            str(row[1])
            for row in connection.execute(
                f'SELECT * FROM pragma_table_info("{table_name}")'
            )
        }

    @staticmethod
    def _collection_metadata(
        reports: list[tuple[str, str]],
    ) -> tuple[dict[str, Any], list[str]]:
        config_names = [
            value for report_type, value in reports
            if report_type == "config_name"
        ]
        command_line = next(
            (
                value for report_type, value in reports
                if report_type == "cmdline"
            ),
            None,
        )
        limitations: list[str] = []
        tokens: list[str] = []
        if command_line:
            try:
                tokens = shlex.split(command_line)
            except ValueError:
                limitations.append("perf_report.cmdline 无法完整解析")

        scope = "unknown"
        if "-a" in tokens or "--system-wide" in tokens:
            scope = "system-wide"
        elif "-p" in tokens or "--pid" in tokens:
            scope = "process"
        elif "-t" in tokens or "--tid" in tokens:
            scope = "thread"

        def option_value(*names: str) -> str | None:
            for name in names:
                if name in tokens:
                    index = tokens.index(name)
                    if index + 1 < len(tokens):
                        return tokens[index + 1]
            return None

        frequency_raw = option_value("-f", "--freq")
        frequency = None
        if frequency_raw:
            try:
                frequency = float(frequency_raw)
            except ValueError:
                limitations.append("Perf 采样频率无法解析")

        target_pids: list[int] = []
        target_raw = option_value("-p", "--pid")
        if target_raw:
            try:
                target_pids = [
                    int(value)
                    for value in re.split(r"[,\s]+", target_raw)
                    if value
                ]
            except ValueError:
                limitations.append("Perf 目标 PID 无法解析")

        return (
            {
                "config_names": config_names,
                "command_line": command_line,
                "scope": scope,
                "sampling_frequency_hz": frequency,
                "callstack_mode": option_value("--call-stack"),
                "clock_id": option_value("--clockid"),
                "target_pids": target_pids,
                "raw_reports": [
                    {"report_type": kind, "report_value": value}
                    for kind, value in reports
                ],
            },
            limitations,
        )

    @staticmethod
    def _aggregate(
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        process_scope: list[int],
        thread_scope: list[int],
        samples: list[dict[str, Any]],
        frames: list[dict[str, Any]],
        collection: dict[str, Any],
        limitations: list[str],
        max_hotspots: int,
        include_thread_profiles: bool = False,
    ) -> dict[str, Any]:
        event_totals: dict[int, dict[str, int]] = defaultdict(
            lambda: {"sample_count": 0, "total_event_count": 0}
        )
        thread_totals: dict[tuple[int, int, str | None], dict[str, int]] = (
            defaultdict(lambda: {"sample_count": 0, "event_count": 0})
        )
        cpu_totals: dict[tuple[int, int | None], dict[str, int]] = (
            defaultdict(lambda: {"sample_count": 0, "event_count": 0})
        )
        observed_process_ids: set[int] = set()
        observed_thread_ids: set[int] = set()
        for sample in samples:
            event_id = sample["event_type_id"]
            weight = sample["event_count"]
            event_totals[event_id]["sample_count"] += 1
            event_totals[event_id]["total_event_count"] += weight
            thread_key = (
                event_id,
                sample["thread_id"],
                sample["thread_name"],
            )
            thread_totals[thread_key]["sample_count"] += 1
            thread_totals[thread_key]["event_count"] += weight
            cpu_key = (event_id, sample["cpu_id"])
            cpu_totals[cpu_key]["sample_count"] += 1
            cpu_totals[cpu_key]["event_count"] += weight
            observed_thread_ids.add(sample["thread_id"])
            if sample["process_id"] is not None:
                observed_process_ids.add(sample["process_id"])

        hotspot_totals: dict[
            tuple[int, str, str | None], dict[str, int]
        ] = defaultdict(
            lambda: {
                "self_samples": 0,
                "inclusive_samples": 0,
                "self_event_count": 0,
                "inclusive_event_count": 0,
            }
        )
        application_module_totals: dict[
            tuple[int, str], dict[str, Any]
        ] = defaultdict(
            lambda: {
                "sample_count": 0,
                "event_count": 0,
                "self_samples": 0,
                "self_event_count": 0,
                "symbols": defaultdict(
                    lambda: {"sample_count": 0, "event_count": 0}
                ),
            }
        )
        bottom_up_totals: dict[tuple[int, str], dict[str, Any]] = {}
        total_callchain_frames = len(frames)
        symbolized_callchain_frames = sum(
            1 for frame in frames if frame["symbolized"]
        )
        current_sample_id: int | None = None
        current_frames: list[dict[str, Any]] = []

        def flush_frames() -> None:
            if current_sample_id is None or not current_frames:
                return
            first = current_frames[0]
            event_id = first["event_type_id"]
            weight = first["event_count"]
            inclusive_seen: set[tuple[str, str | None]] = set()
            self_seen: set[tuple[str, str | None]] = set()
            terminal_depth = max(frame["depth"] for frame in current_frames)
            ordered_frames = sorted(
                current_frames,
                key=lambda frame: frame["depth"],
            )
            terminal = next(
                frame
                for frame in reversed(ordered_frames)
                if frame["depth"] == terminal_depth
            )
            application_ancestors = [
                frame
                for frame in ordered_frames
                if frame["depth"] <= terminal_depth
                and PerfAnalysisRepository._is_application_frame(
                    frame["symbol"], frame["file_path"]
                )
            ]
            application_owner = (
                application_ancestors[-1]
                if application_ancestors
                else None
            )
            if application_owner is not None:
                diagnosis = (
                    PerfAnalysisRepository._classify_bottom_up_operation(
                        ordered_frames,
                        application_owner=application_owner,
                    )
                )
                if diagnosis is not None:
                    bottom_key = (event_id, diagnosis["key"])
                    bottom = bottom_up_totals.setdefault(
                        bottom_key,
                        {
                            **diagnosis,
                            "self_samples": 0,
                            "self_event_count": 0,
                            "supporting_symbols": defaultdict(
                                lambda: {
                                    "sample_count": 0,
                                    "event_count": 0,
                                }
                            ),
                            "caller_paths": defaultdict(
                                lambda: {
                                    "sample_count": 0,
                                    "event_count": 0,
                                }
                            ),
                        },
                    )
                    bottom["self_samples"] += 1
                    bottom["self_event_count"] += weight
                    matched_symbol = diagnosis["matched_symbol"]
                    symbol_row = bottom["supporting_symbols"][
                        matched_symbol
                    ]
                    symbol_row["sample_count"] += 1
                    symbol_row["event_count"] += weight
                    terminal_index = ordered_frames.index(terminal)
                    owner_index = ordered_frames.index(application_owner)
                    reverse_path = list(
                        reversed(
                            ordered_frames[
                                owner_index:terminal_index + 1
                            ]
                        )
                    )
                    if len(reverse_path) > 16:
                        reverse_path = [
                            *reverse_path[:15],
                            application_owner,
                        ]
                    caller_path = tuple(
                        frame["symbol"] for frame in reverse_path
                    )
                    path_row = bottom["caller_paths"][caller_path]
                    path_row["sample_count"] += 1
                    path_row["event_count"] += weight
            application_modules_seen: set[str] = set()
            application_symbols_seen: set[tuple[str, str]] = set()
            for frame in current_frames:
                frame_key = (frame["symbol"], frame["file_path"])
                aggregate_key = (event_id, *frame_key)
                if frame_key not in inclusive_seen:
                    inclusive_seen.add(frame_key)
                    hotspot_totals[aggregate_key][
                        "inclusive_samples"
                    ] += 1
                    hotspot_totals[aggregate_key][
                        "inclusive_event_count"
                    ] += weight
                if (
                    frame["depth"] == terminal_depth
                    and frame_key not in self_seen
                ):
                    self_seen.add(frame_key)
                    hotspot_totals[aggregate_key]["self_samples"] += 1
                    hotspot_totals[aggregate_key][
                        "self_event_count"
                    ] += weight
                if not PerfAnalysisRepository._is_application_frame(
                    frame["symbol"], frame["file_path"]
                ):
                    continue
                module_path = str(frame["file_path"] or frame["symbol"])
                module_key = (event_id, module_path)
                module = application_module_totals[module_key]
                if module_path not in application_modules_seen:
                    application_modules_seen.add(module_path)
                    module["sample_count"] += 1
                    module["event_count"] += weight
                symbol_key = (module_path, frame["symbol"])
                if symbol_key not in application_symbols_seen:
                    application_symbols_seen.add(symbol_key)
                    symbol_row = module["symbols"][frame["symbol"]]
                    symbol_row["sample_count"] += 1
                    symbol_row["event_count"] += weight
                if frame["depth"] == terminal_depth:
                    module["self_samples"] += 1
                    module["self_event_count"] += weight

        for frame in frames:
            if frame["sample_id"] != current_sample_id:
                flush_frames()
                current_sample_id = frame["sample_id"]
                current_frames = []
            current_frames.append(frame)
        flush_frames()

        config_names = collection["config_names"]
        event_profiles: list[dict[str, Any]] = []
        for event_id in sorted(event_totals):
            totals = event_totals[event_id]
            if len(config_names) == 1:
                event_name = config_names[0]
            else:
                event_name = f"event_type_{event_id}"
                if config_names:
                    note = (
                        "多个 config_name 无法与 event_type_id 确定映射；"
                        "保留数值事件 ID"
                    )
                    if note not in limitations:
                        limitations.append(note)

            event_hotspots = [
                (key, values)
                for key, values in hotspot_totals.items()
                if key[0] == event_id
            ]
            application_hotspots = [
                item
                for item in event_hotspots
                if PerfAnalysisRepository._is_application_frame(
                    item[0][1], item[0][2]
                )
            ]
            context_hotspots = [
                item
                for item in event_hotspots
                if PerfAnalysisRepository._frame_layer(
                    item[0][1], item[0][2]
                )
                == "process-runtime-context"
            ]
            runtime_hotspots = [
                item
                for item in event_hotspots
                if item not in application_hotspots
                and item not in context_hotspots
            ]

            inclusive_hotspots = sorted(
                (
                    (key, values)
                    for key, values in application_hotspots
                ),
                key=lambda item: (
                    -item[1]["inclusive_event_count"],
                    -item[1]["inclusive_samples"],
                    item[0][1],
                ),
            )[:max_hotspots]
            ranked_hotspots = inclusive_hotspots[:max_hotspots]
            event_weight = totals["total_event_count"]
            event_samples = totals["sample_count"]
            hotspots = []
            for (_, symbol, file_path), values in ranked_hotspots:
                denominator = event_weight or event_samples
                numerator = (
                    values["inclusive_event_count"]
                    if event_weight
                    else values["inclusive_samples"]
                )
                hotspots.append(
                    {
                        "symbol": symbol,
                        "file_path": file_path,
                        "layer": "application",
                        **values,
                        "inclusive_share": (
                            numerator / denominator if denominator else 0
                        ),
                    }
                )

            def serialize_secondary(
                candidates: list[
                    tuple[tuple[int, str, str | None], dict[str, int]]
                ],
                *,
                limit: int,
            ) -> list[dict[str, Any]]:
                ranked = sorted(
                    candidates,
                    key=lambda item: (
                        -item[1]["inclusive_event_count"],
                        -item[1]["inclusive_samples"],
                        item[0][1],
                    ),
                )[:limit]
                rows: list[dict[str, Any]] = []
                for (_, symbol, file_path), values in ranked:
                    denominator = event_weight or event_samples
                    numerator = (
                        values["inclusive_event_count"]
                        if event_weight
                        else values["inclusive_samples"]
                    )
                    rows.append(
                        {
                            "symbol": symbol,
                            "file_path": file_path,
                            "layer": PerfAnalysisRepository._frame_layer(
                                symbol, file_path
                            ),
                            **values,
                            "inclusive_share": (
                                numerator / denominator
                                if denominator
                                else 0
                            ),
                        }
                    )
                return rows

            application_modules = []
            for (_, module_path), values in sorted(
                (
                    (key, values)
                    for key, values in application_module_totals.items()
                    if key[0] == event_id
                ),
                key=lambda item: (
                    -item[1]["event_count"],
                    -item[1]["sample_count"],
                    item[0][1],
                ),
            )[:max_hotspots]:
                symbols = sorted(
                    values["symbols"].items(),
                    key=lambda item: (
                        -item[1]["event_count"],
                        -item[1]["sample_count"],
                        item[0],
                    ),
                )
                application_modules.append(
                    {
                        "path": module_path,
                        "sample_count": values["sample_count"],
                        "event_count": values["event_count"],
                        "sample_share": (
                            values["sample_count"] / event_samples
                            if event_samples
                            else 0
                        ),
                        "event_share": (
                            values["event_count"] / event_weight
                            if event_weight
                            else 0
                        ),
                        "self_samples": values["self_samples"],
                        "self_event_count": values["self_event_count"],
                        "representative_symbols": [
                            {
                                "symbol": symbol,
                                **symbol_values,
                            }
                            for symbol, symbol_values in symbols[:5]
                        ],
                    }
                )

            bottom_up_diagnostics = []
            for (_, _), values in sorted(
                (
                    (key, values)
                    for key, values in bottom_up_totals.items()
                    if key[0] == event_id
                ),
                key=lambda item: (
                    -item[1]["self_event_count"],
                    -item[1]["self_samples"],
                    item[0][1],
                ),
            )[:max_hotspots]:
                self_sample_share = (
                    values["self_samples"] / event_samples
                    if event_samples
                    else 0
                )
                self_event_share = (
                    values["self_event_count"] / event_weight
                    if event_weight
                    else 0
                )
                ranking_share = (
                    self_event_share if event_weight else self_sample_share
                )
                if values["self_samples"] < 3 or ranking_share < 0.005:
                    continue
                representative_path, path_values = max(
                    values["caller_paths"].items(),
                    key=lambda item: (
                        item[1]["event_count"],
                        item[1]["sample_count"],
                    ),
                )
                supporting_symbols = sorted(
                    values["supporting_symbols"].items(),
                    key=lambda item: (
                        -item[1]["event_count"],
                        -item[1]["sample_count"],
                        item[0],
                    ),
                )
                bottom_up_diagnostics.append(
                    {
                        "operation": values["operation"],
                        "category": values["category"],
                        "self_samples": values["self_samples"],
                        "self_event_count": values["self_event_count"],
                        "self_sample_share": self_sample_share,
                        "self_event_share": self_event_share,
                        "application_function": values.get(
                            "application_function"
                        ),
                        "supporting_symbols": [
                            {
                                "symbol": symbol,
                                **symbol_values,
                            }
                            for symbol, symbol_values in supporting_symbols[:5]
                        ],
                        "representative_reverse_path": list(
                            representative_path
                        ),
                        "representative_path_samples": path_values[
                            "sample_count"
                        ],
                        "representative_path_event_count": path_values[
                            "event_count"
                        ],
                        "optimization_direction": values[
                            "optimization_direction"
                        ],
                        "confidence": (
                            0.9
                            if values["category"] == "application-function"
                            else 0.8 if ranking_share >= 0.01 else 0.65
                        ),
                        "requires_top_down_correlation": True,
                        "root_cause_candidate": ranking_share >= 0.01,
                    }
                )

            threads = [
                {
                    "thread_id": thread_id,
                    "thread_name": thread_name,
                    **values,
                    "sample_share": (
                        values["sample_count"] / event_samples
                        if event_samples
                        else 0
                    ),
                }
                for (
                    (candidate_event, thread_id, thread_name),
                    values,
                ) in sorted(
                    thread_totals.items(),
                    key=lambda item: -item[1]["sample_count"],
                )
                if candidate_event == event_id
            ][:20]
            cpus = [
                {
                    "cpu_id": cpu_id,
                    **values,
                    "sample_share": (
                        values["sample_count"] / event_samples
                        if event_samples
                        else 0
                    ),
                }
                for (candidate_event, cpu_id), values in sorted(
                    cpu_totals.items(),
                    key=lambda item: -item[1]["sample_count"],
                )
                if candidate_event == event_id
            ]
            event_profiles.append(
                {
                    "event_type_id": event_id,
                    "event_name": event_name,
                    **totals,
                    "hotspots": hotspots,
                    "application_modules": application_modules,
                    "bottom_up_diagnostics": bottom_up_diagnostics,
                    "bottom_up_semantics": (
                        "internal diagnostic only: promote a Bottom-up result "
                        "only when it identifies a specific application "
                        "function or concrete runtime operation and Top-down "
                        "Trace evidence places it on the critical path"
                    ),
                    "context_hotspots": serialize_secondary(
                        context_hotspots,
                        limit=10,
                    ),
                    "runtime_hotspots": serialize_secondary(
                        runtime_hotspots,
                        limit=10,
                    ),
                    "application_hotspot_status": (
                        "available" if hotspots else "unavailable"
                    ),
                    "hotspot_ranking": (
                        "application-owned inclusive-weight leaders; self "
                        "weight identifies terminal work but does not make a "
                        "tiny leaf outrank a dominant application path; "
                        "process roots and runtime/framework frames are "
                        "context only"
                    ),
                    "threads": threads,
                    "cpus": cpus,
                }
            )
            if event_samples and not hotspots:
                note = (
                    f"event_type_id={event_id} 的 Perf 调用栈未解析出应用包、"
                    "HAP/HSP 或应用自带库帧；公共启动根和系统运行时不能作为"
                    "应用根因"
                )
                if note not in limitations:
                    limitations.append(note)

        if samples and not frames:
            limitations.append("所选 Perf 样本没有可用调用栈帧")
        if not samples:
            limitations.append("所选问题窗口和进程/线程范围内没有 Perf 样本")
        if total_callchain_frames and (
            symbolized_callchain_frames / total_callchain_frames < 0.8
        ):
            limitations.append(
                "Perf 调用栈包含较多 module+offset 地址标签；"
                "这些标签不按完整函数符号计入 symbolization_rate"
            )

        thread_profiles: list[dict[str, Any]] = []
        if include_thread_profiles and observed_thread_ids:
            sample_thread_ids = {
                sample["sample_id"]: sample["thread_id"]
                for sample in samples
            }
            samples_by_thread: dict[int, list[dict[str, Any]]] = defaultdict(
                list
            )
            frames_by_thread: dict[int, list[dict[str, Any]]] = defaultdict(
                list
            )
            thread_names: dict[int, str | None] = {}
            for sample in samples:
                thread_id = sample["thread_id"]
                samples_by_thread[thread_id].append(sample)
                thread_names[thread_id] = sample.get("thread_name")
            for frame in frames:
                thread_id = sample_thread_ids.get(frame["sample_id"])
                if thread_id is not None:
                    frames_by_thread[thread_id].append(frame)
            for thread_id in sorted(
                observed_thread_ids,
                key=lambda value: (-len(samples_by_thread[value]), value),
            ):
                thread_result = PerfAnalysisRepository._aggregate(
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                    process_scope=process_scope,
                    thread_scope=[thread_id],
                    samples=samples_by_thread[thread_id],
                    frames=frames_by_thread[thread_id],
                    collection=collection,
                    limitations=[],
                    max_hotspots=max_hotspots,
                    include_thread_profiles=False,
                )
                for thread_event in thread_result["event_profiles"]:
                    thread_profiles.append(
                        {
                            "thread_id": thread_id,
                            "thread_name": (
                                thread_names.get(thread_id)
                                or f"TID {thread_id}"
                            ),
                            **thread_event,
                        }
                    )

        return {
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "collection": collection,
            "requested_process_ids": process_scope,
            "requested_thread_ids": thread_scope,
            "observed_process_ids": sorted(observed_process_ids),
            "observed_thread_ids": sorted(observed_thread_ids),
            "sample_count": len(samples),
            "total_callchain_frames": total_callchain_frames,
            "symbolized_callchain_frames": symbolized_callchain_frames,
            "symbolization_rate": (
                symbolized_callchain_frames / total_callchain_frames
                if total_callchain_frames
                else None
            ),
            "event_profiles": event_profiles,
            "thread_profiles": thread_profiles,
            "limitations": limitations,
        }

    @staticmethod
    def _is_application_frame(
        symbol: str,
        file_path: str | None,
    ) -> bool:
        return PerfAnalysisRepository._frame_layer(
            symbol, file_path
        ) == "application"

    @staticmethod
    def _frame_layer(symbol: str, file_path: str | None) -> str:
        path = str(file_path or "").replace("\\", "/").lower()
        label = str(symbol or "").lower()
        application_markers = (
            "/data/storage/",
            "/data/app/",
            "/data/local/",
            "/app/",
            "/proc/",
        )
        if any(marker in path for marker in application_markers) and (
            "/data/" in path
            or "/root/data/" in path
            or path.startswith("/app/")
        ):
            return "application"
        if (
            path.endswith((".hap", ".hsp", ".app"))
            and not path.startswith("/system/")
        ):
            return "application"
        context_markers = (
            "appspawn",
            "libbegetutil",
            "mainthread::start",
            "eventrunner::run",
            "__libc_start",
            "libc_start_main",
            "ld-musl",
        )
        if any(marker in path or marker in label for marker in context_markers):
            return "process-runtime-context"
        if path.startswith("/system/") or path.startswith("/lib/"):
            return "framework-runtime"
        if path in {"sysmgr.elf", "[kernel.kallsyms]"}:
            return "kernel"
        return "unknown"

    @staticmethod
    def _classify_bottom_up_operation(
        frames: list[dict[str, Any]],
        *,
        application_owner: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Turn a sampled leaf into a specific internal diagnosis.

        Bottom-up is useful only when it narrows a recurring stack to an
        application function or a concrete operation.  Module-wide offset
        buckets such as ``dingtalk.hap+offsets`` deliberately return ``None``
        instead of becoming user-facing "hotspots".
        """

        symbols = [str(frame.get("symbol") or "") for frame in frames]
        semantic_rules: tuple[
            tuple[str, str, tuple[str, ...], str], ...
        ] = (
            (
                "font-matching-text-shaping",
                "Font matching, fallback and text shaping",
                (
                    "FcFontMatch",
                    "FcFontSetMatch",
                    "FcConfigSubstitute",
                    "FcDefaultSubstitute",
                    "FcFontSort",
                    "QFontDatabase",
                    "QFontEngine",
                    "QTextEngine::shapeText",
                    "shapeTextWithHarfbuzz",
                    "hb_shape",
                ),
                "Cache font selection and fallback results, reuse font/text "
                "layout objects, reduce candidate families and styles, and "
                "avoid repeating shaping work for unchanged text.",
            ),
        )
        for category, operation, markers, direction in semantic_rules:
            matched = next(
                (
                    symbol
                    for symbol in reversed(symbols)
                    if any(marker.lower() in symbol.lower() for marker in markers)
                ),
                None,
            )
            if matched is not None:
                return {
                    "key": f"runtime-operation:{category}",
                    "operation": operation,
                    "category": category,
                    "application_function": None,
                    "matched_symbol": matched,
                    "optimization_direction": direction,
                }

        owner_symbol = str(application_owner.get("symbol") or "")
        owner_path = str(application_owner.get("file_path") or "")
        generic_application_markers = (
            ".plt",
            ".got",
            "[unknown]",
            "std::",
            "operator new",
            "operator delete",
            "__cxa",
            "_unwind",
            "napi::details",
            "napi::registermodule",
            "registermodule",
            "__init_array",
            "_global__sub_i_",
            "thunk",
            "trampoline",
        )
        application_frames = [
            frame
            for frame in frames
            if PerfAnalysisRepository._is_application_frame(
                str(frame.get("symbol") or ""),
                frame.get("file_path"),
            )
        ]
        actionable_owner: dict[str, Any] | None = None
        for candidate in reversed(application_frames):
            symbol = str(candidate.get("symbol") or "")
            path = str(candidate.get("file_path") or "")
            normalized = symbol.lower()
            basename = path.replace("\\", "/").rsplit("/", 1)[-1]
            is_offset_label = bool(
                re.search(r"\+0x[0-9a-f]+$", symbol, re.IGNORECASE)
                or normalized.endswith("+offsets")
            )
            is_module_label = (
                not symbol
                or normalized == basename.lower()
                or normalized.endswith((".hap", ".hsp", ".app", ".so"))
            )
            if (
                is_offset_label
                or is_module_label
                or any(marker in normalized for marker in generic_application_markers)
            ):
                continue
            actionable_owner = candidate
            break

        if actionable_owner is not None:
            owner_symbol = str(actionable_owner.get("symbol") or "")
            owner_path = str(actionable_owner.get("file_path") or "")
            return {
                "key": f"application-function:{owner_path}:{owner_symbol}",
                "operation": f"Application function: {owner_symbol}",
                "category": "application-function",
                "application_function": owner_symbol,
                "matched_symbol": owner_symbol,
                "optimization_direction": (
                    "Locate this function in the matching critical-path "
                    "stage, then reduce, defer, cache, batch, or move its work."
                ),
            }

        operation_rules: tuple[
            tuple[str, str, tuple[str, ...], str], ...
        ] = (
            (
                "native-module-loading",
                "Native module dynamic loading",
                (
                    "NativeModuleManager::LoadModuleLibrary",
                    "FindNativeModuleByDisk",
                    "LoadModuleLibrary",
                    "dlopen_impl",
                    "dlopen",
                ),
                "Reduce or lazily load non-critical native modules before "
                "the first usable frame.",
            ),
            (
                "ark-module-evaluation",
                "Ark module loading and evaluation",
                (
                    "SourceTextModule::Evaluate",
                    "ModuleManager::HostGetImportedModule",
                    "ModuleManager::ResolveImportedModule",
                    "ExecuteModuleBuffer",
                ),
                "Split the matching application module and defer non-critical "
                "module evaluation outside the startup critical path.",
            ),
            (
                "ark-object-materialization",
                "Ark literal, class, or object materialization",
                (
                    "FindOrCreateConstPool",
                    "LiteralDataExtractor",
                    "CreateObjectFromProperties",
                    "NewEcmaHClass",
                    "Createobjectwithbuffer",
                    "Createarraywithbuffer",
                    "Defineclasswithbuffer",
                    "Definemethod",
                    "Definefunc",
                ),
                "Reduce startup-time object/class construction and move "
                "non-essential initialization after the usable frame.",
            ),
            (
                "garbage-collection",
                "Garbage collection",
                (
                    "CollectGarbage",
                    "GarbageCollect",
                    "Heap::Collect",
                    "ConcurrentMarker",
                ),
                "Reduce allocation pressure in the correlated application "
                "stage and avoid triggering GC on the critical path.",
            ),
            (
                "image-decoding",
                "Image decoding",
                (
                    "ImageSource::CreatePixelMap",
                    "DecodeImage",
                    "decodeBitmap",
                    "SkCodec::getPixels",
                    "OHOS::Media::ImageSource",
                ),
                "Downsample, cache, or move the correlated image decode off "
                "the startup critical path.",
            ),
            (
                "database-operation",
                "Database initialization or query",
                (
                    "sqlite3_open",
                    "sqlite3_prepare",
                    "sqlite3_step",
                    "RdbStore::",
                    "RdbHelper::",
                    "speed_db",
                ),
                "Defer the correlated database open/query, reduce startup "
                "reads, or warm only data required for the first frame.",
            ),
            (
                "bundle-page-fault",
                "Bundle or native-library page fault and mapping",
                (
                    "fscache_page_get_and_readahead",
                    "vfs_op_mmap_fill_page",
                    "do_fusion_fault",
                    "filemap_fault",
                    "faultin",
                    "do_mmap",
                ),
                "Reduce cold-loaded application code/data and mappings in "
                "the correlated startup stage.",
            ),
        )
        for category, operation, markers, direction in operation_rules:
            for symbol in reversed(symbols):
                lowered = symbol.lower()
                if any(marker.lower() in lowered for marker in markers):
                    return {
                        "key": f"runtime-operation:{category}",
                        "operation": operation,
                        "category": category,
                        "application_function": None,
                        "matched_symbol": symbol,
                        "optimization_direction": direction,
                    }
        return None
