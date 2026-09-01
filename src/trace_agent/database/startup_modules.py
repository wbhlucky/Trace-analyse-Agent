from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from trace_agent.database.repository import SQLiteTraceRepository


class StartupModuleRepository:
    """Aggregate observable startup SO/module slices without double counting."""

    _MODULE_PATTERN = re.compile(
        r"(?P<module>(?:(?:/[^\s,|]+)*/)?"
        r"lib[A-Za-z0-9_.+\-]+\.so(?:\.\d+(?:\.\d+)*)?)",
        re.IGNORECASE,
    )

    def __init__(self, database_path: Path) -> None:
        self._repository = SQLiteTraceRepository(
            database_path,
            query_timeout_seconds=10,
        )

    def inspect(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        thread_itids: Iterable[int] = (),
        max_modules: int = 500,
    ) -> dict[str, Any]:
        if interval_end_ns <= interval_start_ns:
            raise ValueError("启动模块区间终点必须晚于起点")
        if not 1 <= max_modules <= 500:
            raise ValueError("max_modules 必须在 1 到 500 之间")

        selected_itids = sorted(set(int(value) for value in thread_itids))
        filters = [
            "c.ts < ?",
            "c.ts + MAX(COALESCE(c.dur, 0), 0) > ?",
            "LOWER(COALESCE(c.name, '')) LIKE '%.so%'",
        ]
        parameters: list[int] = [interval_end_ns, interval_start_ns]
        if selected_itids:
            placeholders = ",".join("?" for _ in selected_itids)
            filters.append(f"c.callid IN ({placeholders})")
            parameters.extend(selected_itids)

        result = self._repository.query(
            "SELECT c.id, c.ts, c.dur, c.name, c.cat, c.callid, c.depth "
            "FROM callstack AS c WHERE "
            + " AND ".join(filters)
            + " ORDER BY c.ts, c.id",
            parameters,
            max_rows=500,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in result.rows:
            name = str(row.get("name") or "")
            ts = row.get("ts")
            dur = row.get("dur")
            if not isinstance(ts, int) or not isinstance(dur, int):
                continue
            clipped_start = max(ts, interval_start_ns)
            clipped_end = min(ts + max(dur, 0), interval_end_ns)
            if clipped_end <= clipped_start:
                continue
            for module_name, module_path in self._extract_modules(name):
                module = grouped.setdefault(
                    module_name.lower(),
                    {
                        "name": module_name,
                        "path": module_path,
                        "intervals": [],
                        "occurrences": 0,
                        "kinds": set(),
                        "slices": [],
                    },
                )
                if module_path and not module["path"]:
                    module["path"] = module_path
                module["intervals"].append((clipped_start, clipped_end))
                module["occurrences"] += 1
                module["kinds"].add(self._marker_kind(name))
                module["slices"].append(
                    {
                        "source_id": f"callstack:{row.get('id')}",
                        "name": name,
                        "start_ns": clipped_start,
                        "end_ns": clipped_end,
                        "duration_ns": clipped_end - clipped_start,
                        "callid": row.get("callid"),
                        "depth": row.get("depth"),
                    }
                )

        modules: list[dict[str, Any]] = []
        window_ns = interval_end_ns - interval_start_ns
        for module in grouped.values():
            merged = self._merge_intervals(module["intervals"])
            observed_ns = sum(end - start for start, end in merged)
            slices = sorted(
                module["slices"],
                key=lambda item: (
                    -int(item["duration_ns"]),
                    int(item["start_ns"]),
                ),
            )
            max_single_ns = (
                int(slices[0]["duration_ns"]) if slices else 0
            )
            first_start_ns = min(
                (int(item["start_ns"]) for item in slices),
                default=interval_start_ns,
            )
            modules.append(
                {
                    "name": module["name"],
                    "path": module["path"],
                    "observed_duration_ms": observed_ns / 1_000_000,
                    "max_single_duration_ms": (
                        max_single_ns / 1_000_000
                    ),
                    "window_share": observed_ns / window_ns,
                    "occurrences": int(module["occurrences"]),
                    "first_offset_ms": (
                        first_start_ns - interval_start_ns
                    )
                    / 1_000_000,
                    "kinds": sorted(module["kinds"]),
                    "representative_slice": (
                        slices[0]["name"] if slices else None
                    ),
                    "source_ids": [
                        item["source_id"] for item in slices[:5]
                    ],
                }
            )

        modules.sort(
            key=lambda item: (
                -float(item["observed_duration_ms"]),
                -float(item["max_single_duration_ms"]),
                str(item["name"]).lower(),
            )
        )
        total_module_count = len(modules)
        modules = modules[:max_modules]
        top_duration = max(
            (
                float(item["observed_duration_ms"])
                for item in modules
            ),
            default=0.0,
        )
        for rank, module in enumerate(modules, start=1):
            module["rank"] = rank
            module["relative_width"] = (
                float(module["observed_duration_ms"]) / top_duration
                if top_duration
                else 0
            )

        limitations = []
        if result.truncated:
            limitations.append(
                "模块 Slice 候选超过 500 行，排行基于有界结果。"
            )
        if total_module_count > max_modules:
            limitations.append(
                f"共识别 {total_module_count} 个模块，当前仅返回前 "
                f"{max_modules} 个。"
            )
        return {
            "available": bool(modules),
            "modules": modules,
            "total_module_count": total_module_count,
            "row_count": result.returned_rows,
            "truncated": result.truncated,
            "thread_itids": selected_itids,
            "metric": (
                "同一模块重叠 Slice 先合并，再按区间并集 wall time 排序；"
                "不同模块之间仍可能嵌套，不能直接求和；"
                "该值不是 CPU Running 时间。"
            ),
            "limitations": limitations,
        }

    @classmethod
    def _extract_modules(
        cls,
        slice_name: str,
    ) -> list[tuple[str, str | None]]:
        found: dict[str, tuple[str, str | None]] = {}
        for match in cls._MODULE_PATTERN.finditer(slice_name):
            raw = match.group("module").rstrip("]/")
            normalized = raw.replace("\\", "/")
            module_name = PurePosixPath(normalized).name
            path = normalized if "/" in normalized else None
            found.setdefault(
                module_name.lower(),
                (module_name, path),
            )
        return list(found.values())

    @staticmethod
    def _marker_kind(slice_name: str) -> str:
        lowered = slice_name.lower()
        if "async_load_" in lowered:
            return "async-load"
        if (
            "sourcetextmodule" in lowered
            or "jspandafileexecutor" in lowered
        ):
            return "arkts-evaluate"
        return "module-load"

    @staticmethod
    def _merge_intervals(
        intervals: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
                continue
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        return merged
