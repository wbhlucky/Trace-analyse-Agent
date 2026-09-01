---
name: trace-analysis
description: "Coordinate evidence-driven performance Trace analysis across exactly four supported problem types: cold start, response latency, completion latency, and frame rate or jank. Use as the top-level methodology for every DitingAgent task to validate collection scope, enforce bounded read-only tool use, route to the matching scenario Skill, test hypotheses, and produce traceable findings without memory analysis."
---

# Trace Analysis

Use tools for facts and scenario Skills for problem-specific decisions. Analyze
only performance. Do not investigate memory usage, leaks, GC pressure, or OOM.

## Route the Scenario

Respect the explicit `scenario_type`; do not reclassify it:

- `cold-start` → use `cold-start-analysis`
- `response-latency` → use `response-latency-analysis`
- `completion-latency` → use `completion-latency-analysis`
- `frame-jank` → use `frame-jank-analysis`

## Common Workflow

1. Confirm the scenario, symptom, device/build context, requested time range,
   problem duration, application Marker hints, baseline availability, and
   Trace collection limitations.
2. Inspect the preloaded `get_trace_overview` Evidence. Call it only when it is
   absent from `preloaded_evidence`; deterministic preflight normally supplies
   it before the model starts.
3. When a Trace database is available, inspect preloaded scenario-candidate
   Evidence first, then call
   `inspect_problem_window_candidates` once before writing boundary SQL if the
   interval is not already fully fixed by an explicit `time_range`, or when
   application Marker hints were supplied. Pass `end_marker` when present;
   otherwise pass the scenario-appropriate `response_marker` or
   `completion_marker`. Pass empty strings and `0` for absent values. Do not
   repeat the same call when preflight already supplied it.
4. Follow the selected scenario Skill to establish its required boundaries and
   metrics.
5. When `perf-samples` is present, apply the enabled
   `perf-sample-analysis` Skill after the problem interval is established.
   Perf must be correlated with that interval and its Trace critical path.
6. Before the first `query_trace_sql` call, read
   `references/trace-streamer-schema.md`. Use its fixed identifiers, joins,
   time units, and bounded query patterns; never guess table semantics.
7. Form at most three competing, falsifiable hypotheses.
8. Select the narrowest available tool that can test each hypothesis. Use
   `query_trace_sql` as the general read-only data path when no narrower
   deterministic tool exists.
9. When an interval depends on one or more critical threads, read
   `references/thread-execution-critical-path.md` and apply the common CPU,
   scheduling, priority, contention, sleep, and wakeup-chain method.
10. Build an evidence chain:

   ```text
   symptom → boundary and metric → hypothesis → tool result
   → root-cause status → recommendation → regression check
   ```

11. Stop when evidence is sufficient or available capabilities cannot test the
    remaining hypotheses.

## Resolve the Problem Interval

Use this precedence and record which rule won:

1. An explicit user `time_range`.
2. A user- or application-defined start/end Slice pair when both identifiers
   resolve uniquely in the intended process and the end follows the start.
3. The selected scenario's semantic boundaries. For example, an application's
   cold-start completion may be its first stable home-page frame, not the
   earliest frame visible anywhere in the Trace.
4. As a general fallback, assume the Trace contains one user operation, select
   the last valid TouchEvent, PointerEvent, or click point as the start, and
   combine it with `problem_duration_ms` to derive the end.

The one-operation rule is an explicit fallback assumption, not a Trace fact.
Reject or lower confidence for it when multiple operations, unrelated late
input markers, incomplete Trace coverage, or contradictory business context
are visible. A last input point without a duration or a reliable end Marker is
only a start candidate, not a complete interval.

When a complete interval is selected, fill `problem_interval` with its metric
definition, exact boundaries, duration, winning selection rule, evidence IDs,
and the `single_operation_assumption` flag. For application-defined cold-start
boundaries, use boundary kinds `application-defined-start`,
`application-marker-start`, `application-defined-completion`,
`application-marker-end`, or `stable-home-frame` as applicable so the standard
platform first-frame candidate remains a comparison metric rather than an
incorrect validator override.

Application Marker names are not globally hard-coded. Treat
`start_marker`/`end_marker` and scenario-specific response/completion markers
as an extensibility interface. Substring discovery may propose candidates, but
automatic pairing requires one unique start and one unique end in the intended
scope.

## Evidence Contract

- Cite only evidence IDs returned by tools.
- Keep observed facts separate from interpretation.
- Use `observed` for a direct Trace fact without a proven cause.
- Use `suspected` when evidence supports a cause but alternatives remain.
- Use `confirmed` only when independent evidence or a baseline comparison rules
  out credible alternatives.
- Put missing markers, counters, intervals, tools, and collection gaps in
  `limitations`.
- Never infer a metric from filenames, file sizes, prompts, or general
  performance knowledge.
- The final Agent Draft is deliberately semantic and compact. Never copy
  deterministic `critical_threads`, CPU/state/wakeup projections, or `perf`
  profiles into it; the application layer hydrates them from Evidence.

## Allowed Diagnostic Dimensions

Investigate CPU execution, scheduling, locks, IPC, synchronous I/O, task queues,
and rendering as possible causes inside the four supported problem types.

Load a domain reference only when relevant and supported by Trace capabilities:

- `references/trace-streamer-schema.md` before writing Trace SQL
- `references/thread-execution-critical-path.md`
- `references/io-analysis.md`
- `references/rendering-analysis.md`
- `references/report-standard.md`
