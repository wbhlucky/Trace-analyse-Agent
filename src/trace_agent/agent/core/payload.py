"""Provider-independent evidence payload compaction."""

from __future__ import annotations

from typing import Any


def select(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}



def agent_tool_payload(
    tool_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep full Evidence on disk while bounding model context growth."""

    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    if tool_name == "inspect_completion_latency_phases":
        compact_data = compact_completion_phases(data)
    elif tool_name == "inspect_perf_profile":
        compact_data = compact_perf_profile(data)
    else:
        return payload
    return {
        "evidence_id": payload.get("evidence_id"),
        "summary": payload.get("summary"),
        "data": compact_data,
        "full_evidence_persisted": True,
        "agent_view_complete_for_decision": True,
        "deterministic_hydration": (
            "Do not copy thread/perf transport data into the final Draft; "
            "the application layer hydrates it from this Evidence."
        ),
    }



def compact_completion_phases(
    data: dict[str, Any],
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    for raw_phase in data.get("phases") or []:
        if not isinstance(raw_phase, dict):
            continue
        profiles: list[dict[str, Any]] = []
        for wrapper in raw_phase.get("thread_profiles") or []:
            if not isinstance(wrapper, dict):
                continue
            thread = wrapper.get("thread_execution")
            if not isinstance(thread, dict):
                continue
            compact_thread = select(
                thread,
                "process_name",
                "thread_name",
                "pid",
                "ipid",
                "tid",
                "itid",
                "state_breakdown",
                "cpu_distribution",
                "cpu_migrations",
                "schedule_slices",
                "longest_running_ms",
                "longest_runnable_ms",
                "longest_sleep_ms",
                "priority",
                "diagnosis",
                "assessment",
                "confidence",
            )
            compact_thread["contention_intervals"] = list(
                thread.get("contention_intervals") or []
            )[:3]
            compact_thread["wakeup_chain"] = list(
                thread.get("wakeup_chain") or []
            )[:3]
            profiles.append(
                {
                    **select(
                        wrapper,
                        "absolute_active_ms",
                        "absolute_running_ms",
                        "selection_reasons",
                        "limitations",
                    ),
                    "thread_execution": compact_thread,
                }
            )
        frames = raw_phase.get("frames")
        compact_frames = dict(frames) if isinstance(frames, dict) else {}
        for key in ("long_frames", "mapped_presentations"):
            compact_frames[key] = list(compact_frames.get(key) or [])[:10]
        phases.append(
            {
                **select(
                    raw_phase,
                    "name",
                    "start_ns",
                    "end_ns",
                    "duration_ms",
                    "thread_rankings",
                    "recommended_perf_thread_ids",
                ),
                "thread_profiles": profiles,
                "slice_hotspots": list(
                    raw_phase.get("slice_hotspots") or []
                )[:10],
                "frames": compact_frames,
            }
        )
    return {
        **select(
            data,
            "target_process",
            "main_thread",
            "render_threads",
            "input_ns",
            "response_ns",
            "completion_ns",
            "recommended_perf_scope",
            "selection_policy",
            "limitations",
        ),
        "phases": phases,
    }



def compact_perf_profile(
    data: dict[str, Any],
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for raw_event in data.get("event_profiles") or []:
        if not isinstance(raw_event, dict):
            continue
        events.append(
            {
                **select(
                    raw_event,
                    "event_type_id",
                    "event_name",
                    "sample_count",
                    "total_event_count",
                    "application_hotspot_status",
                    "bottom_up_semantics",
                ),
                "threads": list(raw_event.get("threads") or [])[:10],
                "cpus": list(raw_event.get("cpus") or [])[:16],
                "hotspots": list(raw_event.get("hotspots") or [])[:12],
                "application_modules": list(
                    raw_event.get("application_modules") or []
                )[:10],
                "bottom_up_diagnostics": list(
                    raw_event.get("bottom_up_diagnostics") or []
                )[:8],
                "context_hotspots": list(
                    raw_event.get("context_hotspots") or []
                )[:5],
            }
        )
    return {
        **select(
            data,
            "interval_start_ns",
            "interval_end_ns",
            "collection",
            "requested_process_ids",
            "requested_thread_ids",
            "observed_process_ids",
            "observed_thread_ids",
            "sample_count",
            "total_callchain_frames",
            "symbolized_callchain_frames",
            "symbolization_rate",
            "limitations",
        ),
        "event_profiles": events,
    }



