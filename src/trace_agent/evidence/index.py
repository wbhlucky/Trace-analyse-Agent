from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from trace_agent.models import EvidenceRecord


EvidencePredicate = Callable[[EvidenceRecord], bool]


class EvidenceIndex:
    """Immutable, typed lookup facade over one run's Evidence ledger.

    Tool payloads remain versioned Evidence records, while consumers share one
    implementation for selecting the record that proves a process, interval,
    phase, thread profile, or Perf projection.
    """

    def __init__(self, records: Iterable[EvidenceRecord]) -> None:
        self._records = tuple(records)
        grouped: dict[str, list[EvidenceRecord]] = {}
        for record in self._records:
            grouped.setdefault(record.tool, []).append(record)
        self._by_tool = {
            tool: tuple(items) for tool, items in grouped.items()
        }

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return self._records

    def all(self, tool: str) -> tuple[EvidenceRecord, ...]:
        return self._by_tool.get(tool, ())

    def last(
        self,
        tool: str,
        predicate: EvidencePredicate | None = None,
    ) -> EvidenceRecord | None:
        for record in reversed(self.all(tool)):
            if predicate is None or predicate(record):
                return record
        return None

    def cold_start_candidate(self, ipid: int) -> EvidenceRecord | None:
        def matches(record: EvidenceRecord) -> bool:
            candidates = record.data.get("process_candidates")
            return isinstance(candidates, list) and any(
                isinstance(item, dict) and item.get("ipid") == ipid
                for item in candidates
            )

        return self.last("inspect_cold_start_candidates", matches)

    def cold_start_timeline(
        self,
        *,
        ipid: int,
        start_ns: int,
        end_ns: int,
    ) -> EvidenceRecord | None:
        candidates: list[tuple[int, int, EvidenceRecord]] = []
        for index, record in enumerate(
            self.all("inspect_cold_start_timeline")
        ):
            target = record.data.get("target_process")
            window = record.data.get("window")
            if not isinstance(target, dict) or target.get("ipid") != ipid:
                continue
            if not isinstance(window, dict):
                continue
            window_start = window.get("start_ns")
            window_end = window.get("end_ns")
            if not isinstance(window_start, int) or not isinstance(
                window_end, int
            ):
                continue
            if window_start <= start_ns and window_end >= end_ns:
                candidates.append((window_end - window_start, -index, record))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))[2]

    def completion_candidate(
        self,
        *,
        ipid: int,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> EvidenceRecord | None:
        candidates: list[tuple[int, int, EvidenceRecord]] = []
        records = self.all("inspect_completion_latency_candidates")
        for index, record in enumerate(records):
            if record.data.get("target_ipid") != ipid:
                continue
            if start_ns is None or end_ns is None:
                candidates.append((0, -index, record))
                continue
            window = record.data.get("discovery_window")
            if not isinstance(window, dict):
                continue
            window_start = window.get("start_ns")
            window_end = window.get("end_ns")
            if not isinstance(window_start, int) or not isinstance(
                window_end, int
            ):
                continue
            if window_start <= start_ns and window_end >= end_ns:
                candidates.append((window_end - window_start, -index, record))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1]))[2]

    def completion_phases(
        self,
        *,
        ipid: int,
        input_ns: int | None = None,
        response_ns: int | None = None,
        completion_ns: int | None = None,
        exact_boundaries: bool = True,
    ) -> EvidenceRecord | None:
        def matches(record: EvidenceRecord) -> bool:
            process = record.data.get("target_process")
            if not isinstance(process, dict) or process.get("ipid") != ipid:
                return False
            if not exact_boundaries:
                return True
            return (
                record.data.get("input_ns") == input_ns
                and record.data.get("response_ns") == response_ns
                and record.data.get("completion_ns") == completion_ns
            )

        return self.last("inspect_completion_latency_phases", matches)

    def perf_profile(self) -> EvidenceRecord | None:
        return self.last("inspect_perf_profile")

    def frame_jank(
        self,
        *,
        ipid: int,
        start_ns: int,
        end_ns: int,
    ) -> EvidenceRecord | None:
        def matches(record: EvidenceRecord) -> bool:
            process = record.data.get("target_process")
            interval = record.data.get("interval")
            return (
                isinstance(process, dict)
                and process.get("ipid") == ipid
                and isinstance(interval, dict)
                and interval.get("start_ns") == start_ns
                and interval.get("end_ns") == end_ns
            )

        return self.last("inspect_frame_jank", matches)

    def thread_profiles(
        self,
        *,
        start_ns: int,
        end_ns: int,
    ) -> list[tuple[EvidenceRecord, dict[str, Any]]]:
        profiles: list[tuple[EvidenceRecord, dict[str, Any]]] = []
        for record in self.all("inspect_thread_execution"):
            if (
                record.data.get("interval_start_ns") == start_ns
                and record.data.get("interval_end_ns") == end_ns
            ):
                raw_thread = record.data.get("thread_execution")
                if isinstance(raw_thread, dict):
                    profiles.append((record, raw_thread))

        for record in self.all("inspect_completion_latency_phases"):
            for raw_phase in record.data.get("phases") or []:
                if not isinstance(raw_phase, dict) or (
                    raw_phase.get("start_ns") != start_ns
                    or raw_phase.get("end_ns") != end_ns
                ):
                    continue
                for wrapper in raw_phase.get("thread_profiles") or []:
                    raw_thread = (
                        wrapper.get("thread_execution")
                        if isinstance(wrapper, dict)
                        else None
                    )
                    if isinstance(raw_thread, dict):
                        profiles.append((record, raw_thread))
        return profiles
