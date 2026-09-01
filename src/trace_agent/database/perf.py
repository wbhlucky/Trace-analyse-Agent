from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Iterable


class PerfProjectionError(RuntimeError):
    """Raised when a bounded Perf visualization projection cannot be built."""


@dataclass(frozen=True, slots=True)
class PerfProfileProjection:
    event_type_id: int
    sample_count: int
    total_event_count: int
    threads: list[dict[str, Any]]
    modules: list[dict[str, Any]]
    flame_root: dict[str, Any] | None
    flame_max_depth: int
    min_node_share: float
    projected_nodes: int
    omitted_nodes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type_id": self.event_type_id,
            "sample_count": self.sample_count,
            "total_event_count": self.total_event_count,
            "threads": self.threads,
            "modules": self.modules,
            "flame_root": self.flame_root,
            "flame_max_depth": self.flame_max_depth,
            "min_node_share": self.min_node_share,
            "projected_nodes": self.projected_nodes,
            "omitted_nodes": self.omitted_nodes,
        }


class PerfTraceRepository:
    """Project bounded Perf samples into chart and flame-graph data.

    TraceStreamer Perf tables do not consistently index callchain_id. The
    repository first scopes the small sample set into a temporary table, then
    scans perf_callchain once. Only the connection-local temporary database is
    written; the Trace database stays read-only.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        timeout_seconds: float = 20,
        min_node_share: float = 0.001,
        max_nodes: int = 10_000,
        max_children_per_node: int = 1_000,
        max_depth: int = 256,
    ) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(f"Trace 数据库不存在：{database_path}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if not 0 <= min_node_share <= 1:
            raise ValueError("min_node_share 必须在 0 到 1 之间")
        self._database_path = database_path.resolve()
        self._timeout_seconds = timeout_seconds
        self._min_node_share = min_node_share
        self._max_nodes = max_nodes
        self._max_children_per_node = max_children_per_node
        self._max_depth = max_depth

    def project(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        event_type_id: int,
        process_ids: Iterable[int] = (),
        thread_ids: Iterable[int] = (),
    ) -> PerfProfileProjection:
        if interval_end_ns <= interval_start_ns:
            raise ValueError("Perf 投影结束时间必须晚于开始时间")
        process_scope = sorted(set(int(value) for value in process_ids))
        thread_scope = sorted(set(int(value) for value in thread_ids))
        deadline = monotonic() + self._timeout_seconds
        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2) as connection:
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    10_000,
                )
                samples = self._read_samples(
                    connection,
                    interval_start_ns=interval_start_ns,
                    interval_end_ns=interval_end_ns,
                    event_type_id=event_type_id,
                    process_ids=process_scope,
                    thread_ids=thread_scope,
                )
                if not samples:
                    return PerfProfileProjection(
                        event_type_id=event_type_id,
                        sample_count=0,
                        total_event_count=0,
                        threads=[],
                        modules=[],
                        flame_root=None,
                        flame_max_depth=0,
                        min_node_share=self._min_node_share,
                        projected_nodes=0,
                        omitted_nodes=0,
                    )
                connection.execute(
                    "CREATE TEMP TABLE scoped_perf_samples("
                    "sample_id INTEGER, callchain_id INTEGER, "
                    "event_count INTEGER, thread_id INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO scoped_perf_samples VALUES (?, ?, ?, ?)",
                    [
                        (
                            row["sample_id"],
                            row["callchain_id"],
                            row["event_count"],
                            row["thread_id"],
                        )
                        for row in samples
                    ],
                )
                connection.execute(
                    "CREATE INDEX scoped_perf_callchain_idx "
                    "ON scoped_perf_samples(callchain_id)"
                )
                (
                    flame_root,
                    max_depth,
                    module_stats,
                ) = self._aggregate_call_tree(
                    connection,
                    samples=samples,
                    process_scope=process_scope,
                    thread_scope=thread_scope,
                )
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise PerfProjectionError("Perf 可视化投影超时") from exc
            raise PerfProjectionError(f"Perf 可视化投影失败：{exc}") from exc

        total_event_count = sum(row["event_count"] for row in samples)
        threads = self._thread_distribution(
            samples,
            total_event_count=total_event_count,
        )
        modules = self._serialize_modules(
            module_stats,
            total_samples=len(samples),
            total_event_count=total_event_count,
        )
        serialized, projected_nodes, omitted_nodes = self._serialize_tree(
            flame_root,
            total_event_count=total_event_count,
        )
        return PerfProfileProjection(
            event_type_id=event_type_id,
            sample_count=len(samples),
            total_event_count=total_event_count,
            threads=threads,
            modules=modules,
            flame_root=serialized,
            flame_max_depth=max_depth,
            min_node_share=self._min_node_share,
            projected_nodes=projected_nodes,
            omitted_nodes=omitted_nodes,
        )

    @staticmethod
    def _read_samples(
        connection: sqlite3.Connection,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        event_type_id: int,
        process_ids: list[int],
        thread_ids: list[int],
    ) -> list[dict[str, Any]]:
        filters = [
            "s.timestamp_trace >= ?",
            "s.timestamp_trace < ?",
            "s.event_type_id = ?",
        ]
        parameters: list[int] = [
            interval_start_ns,
            interval_end_ns,
            event_type_id,
        ]
        if process_ids:
            placeholders = ",".join("?" for _ in process_ids)
            filters.append(f"t.process_id IN ({placeholders})")
            parameters.extend(process_ids)
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            filters.append(f"s.thread_id IN ({placeholders})")
            parameters.extend(thread_ids)
        sql = (
            "SELECT s.id, s.callchain_id, s.event_count, s.thread_id, "
            "s.cpu_id, t.thread_name, t.process_id "
            "FROM perf_sample AS s "
            "LEFT JOIN perf_thread AS t ON t.thread_id = s.thread_id "
            "WHERE "
            + " AND ".join(filters)
            + " ORDER BY s.id"
        )
        return [
            {
                "sample_id": int(row[0]),
                "callchain_id": int(row[1]),
                "event_count": int(row[2] or 0),
                "thread_id": int(row[3]),
                "cpu_id": int(row[4]) if row[4] is not None else None,
                "thread_name": str(row[5]) if row[5] else None,
                "process_id": int(row[6]) if row[6] is not None else None,
            }
            for row in connection.execute(sql, parameters)
        ]

    def _aggregate_call_tree(
        self,
        connection: sqlite3.Connection,
        *,
        samples: list[dict[str, Any]],
        process_scope: list[int],
        thread_scope: list[int],
    ) -> tuple[dict[str, Any], int, dict[int, dict[str, Any]]]:
        observed_thread_ids = sorted(
            {int(row["thread_id"]) for row in samples}
        )
        if len(observed_thread_ids) == 1:
            thread_id = observed_thread_ids[0]
            thread_name = next(
                (
                    row["thread_name"]
                    for row in samples
                    if row["thread_id"] == thread_id
                    and row["thread_name"]
                ),
                None,
            )
            root_label = (
                f"TID {thread_id} · {thread_name}"
                if thread_name
                else f"TID {thread_id}"
            )
        elif thread_scope:
            root_label = "Selected critical threads"
        elif len(process_scope) == 1:
            root_label = f"PID {process_scope[0]}"
        else:
            root_label = "Selected process scope"
        root = self._new_node(root_label, None)
        root["thread_id"] = (
            observed_thread_ids[0]
            if len(observed_thread_ids) == 1
            else None
        )
        split_by_thread = len(observed_thread_ids) > 1
        thread_nodes: dict[int, dict[str, Any]] = {}
        sample_lookup = {row["sample_id"]: row for row in samples}
        module_paths = self._module_paths(connection)
        module_stats: dict[int, dict[str, Any]] = {}
        for sample in samples:
            root["samples"] += 1
            root["event_count"] += sample["event_count"]
            if split_by_thread:
                thread_id = int(sample["thread_id"])
                thread_node = thread_nodes.get(thread_id)
                if thread_node is None:
                    thread_name = sample.get("thread_name")
                    label = (
                        f"TID {thread_id} · {thread_name}"
                        if thread_name
                        else f"TID {thread_id}"
                    )
                    thread_node = self._new_node(
                        label,
                        None,
                        thread_id=thread_id,
                    )
                    thread_nodes[thread_id] = thread_node
                    root["_children"][("thread", thread_id)] = thread_node
                thread_node["samples"] += 1
                thread_node["event_count"] += sample["event_count"]

        cursor = connection.execute(
            "SELECT s.sample_id, c.depth, d.data AS symbol, c.file_id "
            "FROM perf_callchain AS c "
            "JOIN scoped_perf_samples AS s "
            "ON s.callchain_id = c.callchain_id "
            "LEFT JOIN data_dict AS d ON d.id = c.name "
            "ORDER BY s.sample_id, c.depth, c.id"
        )
        current_sample_id: int | None = None
        current_frames: list[tuple[int, str, int | None]] = []
        max_depth = 0

        def flush() -> None:
            nonlocal max_depth
            if current_sample_id is None:
                return
            sample = sample_lookup.get(current_sample_id)
            if sample is None:
                return
            ordered = sorted(current_frames, key=lambda item: item[0])
            max_depth = max(
                max_depth,
                len(ordered) + (1 if split_by_thread else 0),
            )
            parent = (
                thread_nodes[int(sample["thread_id"])]
                if split_by_thread
                else root
            )
            sample_modules: dict[int, set[str]] = {}
            for _, symbol, file_id in ordered[: self._max_depth]:
                key = (symbol, file_id)
                child = parent["_children"].get(key)
                if child is None:
                    child = self._new_node(symbol, file_id)
                    parent["_children"][key] = child
                child["samples"] += 1
                child["event_count"] += sample["event_count"]
                parent = child
                if (
                    file_id is not None
                    and self._is_shared_object(
                        module_paths.get(file_id),
                        symbol,
                    )
                ):
                    sample_modules.setdefault(file_id, set()).add(symbol)
            parent["self_samples"] += 1
            parent["self_event_count"] += sample["event_count"]
            for file_id, symbols in sample_modules.items():
                path = module_paths.get(file_id)
                module = module_stats.setdefault(
                    file_id,
                    {
                        "file_id": file_id,
                        "path": path,
                        "sample_count": 0,
                        "event_count": 0,
                        "_symbols": {},
                    },
                )
                module["sample_count"] += 1
                module["event_count"] += sample["event_count"]
                for symbol in symbols:
                    symbol_row = module["_symbols"].setdefault(
                        symbol,
                        {"sample_count": 0, "event_count": 0},
                    )
                    symbol_row["sample_count"] += 1
                    symbol_row["event_count"] += sample["event_count"]

        for sample_id, depth, symbol, file_id in cursor:
            normalized_sample_id = int(sample_id)
            if current_sample_id != normalized_sample_id:
                flush()
                current_sample_id = normalized_sample_id
                current_frames = []
            label = str(symbol).strip() if symbol else "[unknown]"
            current_frames.append(
                (
                    int(depth),
                    label,
                    int(file_id) if file_id is not None else None,
                )
            )
        flush()
        return root, min(max_depth, self._max_depth), module_stats

    @staticmethod
    def _new_node(
        name: str,
        file_id: int | None,
        *,
        thread_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "file_id": file_id,
            "thread_id": thread_id,
            "samples": 0,
            "event_count": 0,
            "self_samples": 0,
            "self_event_count": 0,
            "_children": {},
        }

    @staticmethod
    def _thread_distribution(
        samples: list[dict[str, Any]],
        *,
        total_event_count: int,
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[int, str | None], dict[str, Any]] = {}
        for sample in samples:
            key = (sample["thread_id"], sample["thread_name"])
            row = grouped.setdefault(
                key,
                {
                    "thread_id": sample["thread_id"],
                    "thread_name": sample["thread_name"] or "未命名线程",
                    "sample_count": 0,
                    "event_count": 0,
                },
            )
            row["sample_count"] += 1
            row["event_count"] += sample["event_count"]
        ranked = sorted(
            grouped.values(),
            key=lambda row: (-row["sample_count"], -row["event_count"]),
        )
        visible = ranked[:8]
        if len(ranked) > 8:
            visible.append(
                {
                    "thread_id": None,
                    "thread_name": f"其他 {len(ranked) - 8} 个线程",
                    "sample_count": sum(
                        row["sample_count"] for row in ranked[8:]
                    ),
                    "event_count": sum(
                        row["event_count"] for row in ranked[8:]
                    ),
                }
            )
        total_samples = len(samples)
        for row in visible:
            row["sample_share"] = (
                row["sample_count"] / total_samples if total_samples else 0
            )
            row["event_share"] = (
                row["event_count"] / total_event_count
                if total_event_count
                else 0
            )
        return visible

    @staticmethod
    def _module_paths(
        connection: sqlite3.Connection,
    ) -> dict[int, str]:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'perf_files' LIMIT 1"
        ).fetchone()
        if has_table is None:
            return {}
        return {
            int(file_id): str(path)
            for file_id, path in connection.execute(
                "SELECT file_id, MIN(path) "
                "FROM perf_files "
                "WHERE path IS NOT NULL AND TRIM(path) != '' "
                "GROUP BY file_id"
            )
            if file_id is not None and path
        }

    @staticmethod
    def _is_shared_object(
        path: str | None,
        symbol: str,
    ) -> bool:
        candidate = (path or symbol).lower()
        return ".so" in candidate

    @staticmethod
    def _serialize_modules(
        module_stats: dict[int, dict[str, Any]],
        *,
        total_samples: int,
        total_event_count: int,
    ) -> list[dict[str, Any]]:
        ranked = sorted(
            module_stats.values(),
            key=lambda row: (
                -int(row["event_count"]),
                -int(row["sample_count"]),
                str(row.get("path") or ""),
            ),
        )
        serialized: list[dict[str, Any]] = []
        for rank, row in enumerate(ranked, start=1):
            path = str(row.get("path") or "")
            symbols = sorted(
                row["_symbols"].items(),
                key=lambda item: (
                    -int(item[1]["event_count"]),
                    -int(item[1]["sample_count"]),
                    item[0],
                ),
            )
            serialized.append(
                {
                    "rank": rank,
                    "file_id": int(row["file_id"]),
                    "name": (
                        PurePosixPath(path.replace("\\", "/")).name
                        if path
                        else f"file_id:{row['file_id']}"
                    ),
                    "path": path or None,
                    "sample_count": int(row["sample_count"]),
                    "event_count": int(row["event_count"]),
                    "sample_share": (
                        int(row["sample_count"]) / total_samples
                        if total_samples
                        else 0
                    ),
                    "event_share": (
                        int(row["event_count"]) / total_event_count
                        if total_event_count
                        else 0
                    ),
                    "representative_symbols": [
                        {
                            "symbol": symbol,
                            "sample_count": int(values["sample_count"]),
                            "event_count": int(values["event_count"]),
                        }
                        for symbol, values in symbols[:3]
                    ],
                }
            )
        return serialized

    def _serialize_tree(
        self,
        root: dict[str, Any],
        *,
        total_event_count: int,
    ) -> tuple[dict[str, Any], int, int]:
        state = {"projected": 0, "omitted": 0}

        def visit(node: dict[str, Any], depth: int) -> dict[str, Any]:
            state["projected"] += 1
            children = sorted(
                node["_children"].values(),
                key=lambda child: -child["event_count"],
            )
            visible_children: list[dict[str, Any]] = []
            for index, child in enumerate(children):
                share = (
                    child["event_count"] / total_event_count
                    if total_event_count
                    else 0
                )
                if (
                    depth >= self._max_depth
                    or share < self._min_node_share
                    or index >= self._max_children_per_node
                    or state["projected"] >= self._max_nodes
                ):
                    state["omitted"] += self._count_nodes(child)
                    continue
                visible_children.append(visit(child, depth + 1))
            return {
                "name": node["name"],
                "file_id": node["file_id"],
                "thread_id": node.get("thread_id"),
                "samples": node["samples"],
                "event_count": node["event_count"],
                "share": (
                    node["event_count"] / total_event_count
                    if total_event_count
                    else 0
                ),
                "self_samples": node["self_samples"],
                "self_event_count": node["self_event_count"],
                "children": visible_children,
            }

        return visit(root, 0), state["projected"], state["omitted"]

    @staticmethod
    def _count_nodes(node: dict[str, Any]) -> int:
        return 1 + sum(
            PerfTraceRepository._count_nodes(child)
            for child in node["_children"].values()
        )
