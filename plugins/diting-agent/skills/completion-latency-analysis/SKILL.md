---
name: completion-latency-analysis
description: Analyze interaction completion latency from input through first effective response and business completion. Use for scenario_type completion-latency; select evidence-backed boundaries, split response and post-response work, and diagnose the critical path with scheduling and Perf evidence when available.
---

# Completion Latency Analysis

Completion latency is `input → business completion`. Response latency is the
included submetric `input → first effective feedback`; the remaining work is
`response → completion`.

## Required Candidate Discovery

Use `inspect_completion_latency_candidates` Evidence before choosing boundaries.

1. If `target_ipid` is unknown, use the preloaded call with `target_ipid=0` and
   the request hints. Call it yourself only if preflight Evidence is absent.
   Select the actual application process from the returned process
   candidates; do not select RenderService, appspawn, or a system host process.
2. Call it again with the selected non-zero application `target_ipid`. Pass all
   available request hints unchanged: `operation_marker`, `start_marker`,
   `end_marker`, `response_marker`, `completion_marker`, and
   `problem_duration_ms`.
3. The second call is the Evidence source for the final process and boundaries.
   Candidate discovery does not make the choice; explain why the selected point
   has the requested business meaning.

## Boundary Precedence

Use the strongest applicable definition:

1. An explicit `time_range` supplied by the user.
2. A unique positive-duration application Slice named by `operation_marker`.
   Its exact interval is `[ts, ts + dur]`. The same behavior is allowed when the
   only marker hint is one unique positive-duration `start_marker`.
3. A unique application-defined start/completion marker pair.
4. Under the one-operation assumption, the final relevant TouchEvent,
   PointEvent, or click marker is the input candidate. For TouchUp/release
   events, use the Slice end, not its start. When the user supplies
   `problem_duration_ms`, `last valid input + duration` is the user-defined
   completion metric interval. It may set `completion_proven=true` after the
   input and one-operation assumption are validated; state that assumption and
   use lower confidence than an application Marker.
5. App frame, mapped presentation frame, RenderService animation end, frame
   quiescence, and last-frame results are discovery candidates only. Validate
   them against the requested action before selecting one.

The frame-quiescence candidate (at least five prior frames in 300 ms followed
by a 200 ms frame gap) and the final-frame fallback are adapted from Harmony
Trace Analyzer v1.0.6. They never prove business completion on their own. An
animation end must also be causally tied to this operation; temporal proximity
is insufficient.

## Three Boundaries and Metrics

Establish:

- `input_boundary`: completed user input or application-defined operation start.
- `response_boundary`: first effective, user-visible feedback. A technical first
  frame is only a candidate until its content is shown to be effective feedback.
- `completion_boundary`: a marker, state transition, or presented frame that
  proves the requested action has completed according to this application.

Calculate from the exact timestamps:

```text
response_latency_ms       = (response - input) / 1e6
post_response_duration_ms = (completion - response) / 1e6
completion_latency_ms     = (completion - input) / 1e6
```

If completion cannot be proven, set `completion_proven=false`; leave
`completion_boundary`, `completion_latency_ms`, and
`post_response_duration_ms` null. Keep a proven response boundary and response
latency when available. Do not convert a discovery candidate into a fact merely
to make the result complete.

## Required Phase Evidence

After the boundaries are selected, call `inspect_completion_latency_phases`
once when CPU scheduling data is available. Pass the exact non-zero application
`target_ipid` and the selected timestamps; pass `0` for an unavailable response
or completion boundary.

The tool does not select or prove boundaries. Use it to:

1. Create exact, non-overlapping response and post-response evidence windows.
2. Compare absolute per-thread Running time before any percentage.
3. Keep the application main thread as pipeline context, then consider the
   highest-running application worker, the strongest Runnable/D-state thread,
   and only frame-mapped RenderService threads.
4. Use returned `thread_profiles` to reason about the critical path. Do not
   serialize `critical_threads` in the final Agent Draft; deterministic
   hydration adds them after submission. A
   targeted `inspect_thread_execution` follow-up is only needed for a relevant
   thread not already projected by the phase tool. Do not repeat phase
   extraction or call `inspect_thread_execution` for threads already returned.
5. Treat `slice_hotspots` as inclusive overlap evidence. Never report an item
   marked `interval_wrapper_candidate` as the root cause without a deeper
   application operation beneath it.
6. Use the returned `recommended_perf_scope.thread_ids` for the final
   thread-scoped `inspect_perf_profile` call. Do not expand back to every thread
   in the process.
7. Use `frames.long_frames` and `mapped_presentations` to correlate a costly
   phase; the 30 ms screen is not a completion-boundary definition.

## Critical-Path Analysis

1. Create separate response and post-response phases when their boundaries are
   proven. Preserve exact, non-overlapping phase boundaries.
2. Identify only critical application/render/IPC threads for each phase. Use
   the phase tool's exact projections; call `inspect_thread_execution` only for
   a relevant thread absent from those projections. Never copy a whole-window
   profile into a phase.
3. Use the common CPU, scheduling, priority, sleep, wakeup-chain, Slice, frame,
   and IPC tools to decide whether elapsed time is running work, runnable delay,
   blocking, I/O, queueing, or rendering.
4. If Perf samples exist, analyze relevant critical threads over the completion
   window and correlate application-level Top-down stacks with Bottom-up hotspot
   assistance. Do not report appspawn, package+offset roots, loaders, or generic
   runtime roots as the optimization target.

## Output Contract

Populate `analysis.completion_latency` with the resolved application process,
boundaries, three metrics, phases, business completion semantics, critical-path
summary, and Evidence IDs. When completion is proven, make
`analysis.problem_interval` use the same input/completion boundaries and exact
duration. Conclusions and recommendations must identify the causal phase and
application-level work, not merely list observed Slices or symbols.

The submitted Agent Draft contains semantic phase objects only. It must not
contain `critical_threads` or `perf`; the application layer hydrates those
deterministic structures from the cited phase and Perf Evidence.
