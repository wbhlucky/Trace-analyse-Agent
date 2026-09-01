---
name: perf-sample-analysis
description: Correlate valid Perf sampling data with the bounded Trace critical path for any supported performance scenario. Use when get_trace_overview reports the perf-samples capability, including cold start, response latency, completion latency, and frame-jank analysis. Inspect collection scope and quality, align samples by timestamp_trace, aggregate self and inclusive event weight and call paths, and explain CPU-running work without using Perf to invent off-CPU causes.
---

# Perf Sample Analysis

Use Perf as statistical evidence for code executing on CPU. Always combine it
with the same scenario interval, process/thread selection, and Trace scheduling
evidence. Do not substitute a whole-Trace hotspot list for critical-path
analysis.

## Required Workflow

1. After the problem interval and target OS PID/TID are established, call
   `inspect_perf_profile` once. This deterministic tool has a reserved budget
   and remains available after ordinary Trace/SQL calls are exhausted. Pass
   the exact half-open interval, the target OS PID, and only main/render/worker
   OS TIDs that Trace evidence places on the critical path. Do not pass every
   thread in the process merely because it has samples.
   A process-only call with an empty `thread_ids` list is discovery only. If it
   is needed, use the returned thread distribution to choose relevant TIDs and
   call the tool once more; only the thread-scoped Evidence may be cited by the
   final deterministic Perf projection.
2. Use the returned collection metadata, event profiles, thread/CPU
   distribution, symbolization counts, application hotspots, application
   modules, context frames, and Evidence ID for reasoning. Do not serialize
   them into the Agent Draft: the application layer creates the final
   `analysis.perf` directly from this Evidence after submission.
3. Read `references/perf-trace-streamer-schema.md` only when a focused SQL
   follow-up is necessary because the deterministic tool reports a concrete
   data gap. Do not spend ordinary SQL calls reproducing data already returned
   by `inspect_perf_profile`.
4. Confirm that `perf_sample.timestamp_trace` overlaps the established problem
   interval and that the collected PID/TID scope covers the critical path.
5. Keep OS `process_id`/`thread_id` separate from Trace Streamer
   `ipid`/`itid`, and aggregate each event independently:
   - sample count;
   - total `event_count`;
   - depth-zero self samples and self event weight;
   - inclusive samples and event weight;
   - complete hot call paths;
   - per-process, thread, and CPU distribution.
6. Correlate the scoped profile with `sched_slice`, `thread_state`, Slices,
   stages, frames, and wakeup evidence from the Trace.
7. Use `bottom_up_diagnostics` only as an internal auxiliary index for the
   Top-down path. A diagnostic may be folded into a finding only when it names
   a specific application function or concrete operation, is marked as a root
   cause candidate, and the matching Top-down Trace stage places it on the
   critical path. Never publish a standalone Bottom-up table or global list.
8. Connect relevant hotspots to semantic findings and cite the
   `inspect_perf_profile` Evidence ID. If Perf is unusable for the selected
   interval, state the exact limitation. The Agent Draft intentionally omits
   `analysis.perf`; deterministic hydration fills it after submission.

The final deterministic `analysis.perf.thread_ids` must contain only the
threads selected for deep critical-path Perf analysis, not every sampled thread in the target process.
Whole-process thread distribution may be used once for discovery, but unrelated
threads must not be expanded into the final hotspot table, thread rail, or
flame graph. Include the main thread and any Render/Display/Worker thread only
when Trace stage, frame, scheduling, or dependency evidence makes it relevant.

## Collection and Quality Rules

- Parse `perf_report` rather than assuming the event or scope.
- Treat `-p` as process-scoped, `-t` as thread-scoped, and system-wide
  collection only when the recorded command proves it.
- Do not use process-scoped Perf to identify code running in competing
  processes.
- Treat `cpu-cycles`, `instructions`, `cpu-clock`, cache events, and other
  events as different units. Never sum or compare their raw weights together.
- Do not convert hardware cycles directly to nanoseconds.
- Compare two windows or traces only when event configuration and collection
  scope are compatible. Normalize by event weight share, sample share, or
  interval duration as appropriate.
- Report low sample counts, incomplete time coverage, unresolved symbols,
  truncated stacks, collection gaps, and incompatible baselines.
- Distinguish address labels from resolved function symbols. Preserve the
  module path when available.

## Trace Correlation Rules

Use Trace scheduling as the authority for execution state:

- Long `Running`: use scoped Perf stacks to explain CPU work.
- Long `R`/`R+`: use scheduling to identify CPU occupants; absence of target
  Perf samples is expected while the thread is not running.
- Long `S`: use the enclosing Slice and wakeup chain. Perf does not explain the
  sleep cause.
- `D-IO`: use I/O and syscall evidence.
- Mixed execution and waits: attribute CPU work and off-CPU delay separately.

A global hotspot is not a cause unless it overlaps the bounded problem interval
and lies on, delays, or competes with the critical path. A hot function outside
that path may be an optimization opportunity but not the latency root cause.

## Hotspot Semantics

- In the bundled Trace Streamer output, `depth=0` is the outer process root
  and larger depth walks toward the sampled leaf. `self` therefore belongs to
  the maximum-depth terminal frame of each sample, not blindly to depth zero.
  Verify orientation again when the parser version changes.
- `inclusive` means the function appears anywhere in a sampled call chain.
- Avoid inflating inclusive share through recursive duplicate frames; count a
  symbol at most once per sample when calculating normalized inclusive share.
- Keep self and inclusive sample counts separate from `event_count` weights.
- Do not call a parent frame expensive solely because its inclusive weight is
  high.
- Inspect complete recurring call paths before recommending a leaf-level
  optimization.

## Application-Layer Attribution

Perf conclusions must reach the application layer. Separate every recurring
path into these roles:

- **process/runtime context**: `appspawn`, `libbegetutil`, loader/libc startup,
  `MainThread::Start`, and `EventRunner::Run` explain how the process entered
  the stack. Their high inclusive share is expected because they are common
  ancestors. Never report them as the application bottleneck or recommend
  optimizing them.
- **framework/runtime bridge**: Ability/UI framework, NAPI, Ark VM dispatch,
  GC, loader, and system libraries explain the mechanism. They may support a
  conclusion such as module evaluation or object creation, but are not an
  application fix point by themselves.
- **application layer**: application HAP/HSP/ABC/AOT frames and application-
  bundled native libraries under the app data/bundle path. Rank these first,
  group them by module, and correlate them with the Trace stage and Slice that
  invoked the work.

Use `event_profiles[].hotspots` only as application-layer candidates.
`context_hotspots` and `runtime_hotspots` are supporting call context, not root
causes. Prefer resolved application functions. When only `app.hap+0x...` or
`libfoo.so+0x...` is available, report the application module and offset,
explain that symbols are incomplete, and request the matching application
symbol/AOT mapping for function-level attribution. Do not fall back to
`appspawn` merely because its percentage is larger.

Rank application paths primarily by inclusive sample/event share, then use
`self` to distinguish terminal CPU work from ancestry. A one-sample leaf must
not outrank a recurring application path solely because the recurring path has
zero self samples. Likewise, high inclusive share proves path membership, not
exclusive cost; combine the path with stage Slices and terminal/runtime frames
before stating what the application is doing.

## Bottom-up Correlation

Bottom-up is an internal root-cause aid, not a report section. It starts from
terminal Self work and asks whether recurring samples identify either a
resolved application function or a concrete operation such as native-module
loading, Ark module evaluation, object materialization, image decode, database
work, GC, or application bundle page faults. Then return to Top-down to prove
which application path, stage, and thread invoked that operation.

Do not create or expose generic buckets such as `app.hap+offsets`,
`libfoo.so+offsets`, `appspawn`, Ark runtime dispatch, libc, or kernel leaves.
They do not tell the developer what to optimize. Unresolved application
module offsets may remain Top-down path context, but they are not a Bottom-up
diagnosis.

A `bottom_up_diagnostics` item may influence a finding or recommendation only
when all of these conditions hold:

- it identifies a specific application function or concrete runtime
  operation;
- `root_cause_candidate` is true and the sample/event share is material;
- its reverse path has an application-owned ancestor;
- Top-down Trace evidence places the same operation in the bounded critical
  stage/thread.

Rank internal candidates by Self event share (or Self sample share when the
event has no weight). Do not use Inclusive weight for Bottom-up ranking. When
those conditions are not met, keep the data internal and make no Bottom-up
claim in the report.

## Confidence and Conclusions

Use Perf as statistical evidence, not exact duration:

- High confidence requires adequate scoped samples, usable stacks, compatible
  collection scope, and Trace evidence that places the hotspot on the critical
  path.
- Lower confidence when symbols are missing, sampling coverage is short,
  samples are sparse, or the event semantics are unclear.
- Do not claim causality from correlation alone.
- Do not interpret a lack of samples as proof of idle time.
- Keep material collection limitations in the top-level Draft limitations;
  deterministic hydration also preserves them in final `analysis.perf`.
