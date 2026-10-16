---
name: cold-start-analysis
description: Analyze application cold-start performance from process discovery and a launch or process-start boundary through the application-defined startup completion, which may be a uniquely marked Slice or a stable home-page frame rather than the earliest presented frame. Use when scenario_type is cold-start to identify the target app without requiring a process hint, reject process reuse, preserve the metric definition, measure and decompose startup, and follow the cross-thread critical path through CPU execution, CPU placement, priority, runnable delay, sleep wakeups, I/O, IPC, task queues, and rendering.
---

# Cold Start Analysis

Treat cold start as a latency scenario whose exact completion semantics can be
application-specific. Fill `cold_start` in the structured result. Do not
require a business marker or a user-provided process, but honor a reliable
application definition when one is supplied.

## Resolve the Target Process

1. Call `inspect_cold_start_candidates` once after `get_trace_overview`. Pass
   `target_process` or an empty string and use a small bounded candidate count.
2. Treat `target_process` as an optional hint. Verify it against Trace facts.
3. Use returned processes, main threads, actual `app_startup` rows, earliest
   Slices, and first frames as candidates, not decisions. Do not assume a fixed
   Marker vocabulary.
4. Prefer a process created inside the Trace with a continuous lifecycle-to-
   frame chain. Exclude system daemons and long-lived processes.
5. Record the selected `pid`, `ipid`, main `tid`/`itid`, reason, confidence,
   evidence, and credible alternatives.
6. If candidates remain ambiguous, continue only with the strongest candidate,
   lower confidence, and report the ambiguity.
7. After selecting an `ipid`, call `inspect_cold_start_timeline` once. Start
   with a bounded 3-second lookback, 5-second lookahead, and at most 160
   events. Do not request the 500-event maximum for initial discovery. Expand
   the window or event count only when returned evidence shows that a credible
   boundary lies outside the result.
8. Treat the returned anchor, events, stages, frame links, and
   `boundary_evidence` as structured facts. The tool's `metric_candidate` is a
   deterministic platform-metric candidate, not proof of input causality or a
   substitute for the scenario decision. Use `query_trace_sql` for focused
   follow-up when any link in that candidate is ambiguous.
9. Keep the investigation bounded. After overview, candidate, and timeline
   discovery, use no more than 12 focused SQL calls. Combine stage comparisons
   with CTEs, conditional aggregation, or `UNION ALL`; do not probe the same
   schema or condition through repeated trial queries.

## Establish Cold-Start Boundaries

1. Prove that the target process was not reused. Reject warm or hot starts.
   When `process.start_ts` is absent, use the timeline's fallback anchor only
   for discovery and require lifecycle/process-spawn evidence before claiming
   a cold start.
2. When `app_startup` has rows, read
   `references/smartperf-app-startup.md`. Treat the rows as strong
   parser-produced evidence, filter them by the resolved package name, and
   preserve raw identifiers. A startup chain can cross processes and threads.
3. When `app_startup` is empty, inspect the actual lifecycle and rendering
   Slices in this Trace with `query_trace_sql`. Do not synthesize SmartPerf
   phase labels or blindly match example Marker names from another Trace.
4. Do not treat the earliest `app_startup.start_time` as the launch boundary or
   the latest `end_time` as the first frame without independent evidence.
5. Select the strongest observable launch-request or process-creation boundary.
   When `boundary_evidence.metric_candidate.status` is
   `proven_platform_boundary_pair`, use its platform launch marker as the
   standardized `start_boundary`; do not silently substitute an earlier
   process timestamp. A different metric must be clearly labeled and must not
   replace this standardized duration.
6. Do not use the earliest `frame_slice` row as cold-start completion merely
   because it is first in time. First establish the metric definition for this
   application. It may be the first main-thread Actual frame, a later frame
   proving that the home page is entered and stable, or a uniquely identified
   application completion Slice.
7. When the metric is defined as an application frame, require the selected
   main-thread `ReceiveVsync` to own the intended Actual frame and be linked by
   `frame_maps` to a render-service frame. Record why this frame satisfies the
   application's startup semantics. When a unique application Slice pair is
   supplied, use the two Slice timestamps as the problem interval and keep any
   technical first-frame metric separate.
8. Also record the mapped render-service frame end as
   `presentation_boundary`. Calculate `presentation_duration_ms` from the same
   `start_boundary` to `presentation_boundary`; it is not the duration of the
   render phase. Keep this metric distinct from the selected application
   completion boundary. When the selected completion is a later application
   marker or stable-home boundary, the standardized technical first-frame
   presentation may legitimately precede it; do not force the presentation
   timestamp to the end of the application-defined interval.
9. Call a metric click-to-frame only when a touch/click event has a
   proven causal chain to the launch. Timing proximity alone is insufficient.
10. Record exact timestamps, sources, confidence, metric semantics, and
   evidence for all selected
   boundaries.
11. If either required boundary is not reliable, report candidate boundaries and do not
   claim a confirmed cold-start duration or root cause.

## Build the Critical Path

1. Measure the exact boundary-to-boundary duration.
2. Split only phases supported by Trace facts: process creation, runtime
   initialization, application lifecycle, page construction, first render, and
   presentation.
   Prefer actual valid `app_startup` rows as phase candidates, but verify their
   labels and boundaries against Slices. Never invent missing rows or force the
   reference phase list onto a Trace whose table is absent or empty.
3. Keep phases inside the cold-start interval and avoid double-counting nested
   or overlapping `app_startup`/Slice ranges.
   `cold_start.stages` must end at or before the selected `end_boundary`. Do not
   extend a stage to
   `presentation_boundary`; describe the short app-to-RS presentation tail in
   the summary instead.
4. Identify the phase that dominates total latency or a like-for-like baseline
   regression.
5. Identify the thread or dependency chain that controls phase completion.
6. For every dominant phase, apply the common method in
   `.qoder/skills/trace-analysis/references/thread-execution-critical-path.md`
   and analyze:
   - actual CPU-running time rather than Slice wall time;
   - runnable delay and contemporaneous CPU competitors;
   - CPU distribution, migrations, and raw priority observations;
   - sleep or uninterruptible waits and their causal wakeup chain;
   - cross-process IPC, I/O, task queues, and render dependencies.
   Analyze the dominant phase and the whole startup window first. Do not issue
   separate CPU-distribution queries for every minor phase merely to populate
   optional fields. Use `inspect_thread_execution` with exact stage boundaries
   when thread evidence is required, but do not copy its model-ready values
   into the Agent Draft. The application layer hydrates supported stage
   threads after submission.
7. Compare equivalent boundaries and phases when a baseline exists.
8. When recommending that SOs or modules be delayed or removed from the
   startup path, enumerate the concrete startup-window Slice observations that
   support the recommendation. Rank modules by merged per-module wall
   interval, preserve the marker kind and path, and distinguish application
   modules from platform modules when the path makes that possible. State that
   different modules may be nested and therefore cannot be summed, and do not
   describe this wall interval as CPU Running time.

## Guardrails

- Clip every state and Slice interval to the stage window.
- Do not sum nested Slice wall durations.
- Do not label a long wall Slice as CPU work without intersecting it with
  `Running` intervals.
- Do not label `S` as a blocker without proving that progress waited for its
  wakeup.
- Do not label CPU migration or concentration as harmful by itself.
- Do not infer big/little CPU class, frequency, affinity, RTG, or scheduler
  policy when the Trace does not expose it.
- Do not declare scheduling unreasonable from priority values alone.
- Preserve raw priority values; interpret their ordering only when platform
  semantics are established.

## Output Requirements

- Fill `cold_start.resolved_process`, launch and selected application-defined
  completion boundaries, total duration, semantic stages, and evidence IDs.
  Do not serialize `critical_threads` or `perf`; deterministic hydration adds
  those large structures after submission.
  When an app-to-render-service mapping is available, also fill
  `presentation_boundary` and `presentation_duration_ms`.
- Distinguish total startup regression from phase-level regression.
- Keep stage, thread-state, CPU distribution, wakeup, and contention evidence
  independently traceable.
- Recommend a measurable cold-start rerun using the same collection conditions.
- Stop without a root-cause finding when only file metadata is available.
