# Thread Execution and Scheduling Critical Path

Read this reference whenever a bounded performance interval depends on one or
more critical threads. Use the fixed Trace Streamer schema from this Skill.

## Contents

- [Interval discipline](#interval-discipline)
- [Thread-state decision tree](#thread-state-decision-tree)
- [CPU execution and placement](#cpu-execution-and-placement)
- [Runnable delay and priority](#runnable-delay-and-priority)
- [Sleep and wakeup chains](#sleep-and-wakeup-chains)
- [Conclusion rules](#conclusion-rules)

## Interval Discipline

Analyze one bounded interval and one critical thread at a time. Clip any
overlapping interval to `[interval_start, interval_end)`:

```sql
MIN(ts + dur, ?) - MAX(ts, ?)
```

Keep wall time and CPU-running time separate. A long `callstack.dur` can contain
sleep or runnable delay. Measure CPU work by intersecting the Slice with
`thread_state.state = 'Running'` or equivalent `sched_slice` intervals.

Call `inspect_thread_execution` for the exact interval and Trace-internal
`itid` before filling a `critical_threads` entry. Use its model-ready
`thread_execution` fields and cite its Evidence ID. The tool deterministically
clips states and scheduling slices, so do not replace its values with manually
aggregated SQL. A whole-problem-window result must never be copied into a
stage-level entry; call the tool again with that stage's exact boundaries.

Do not add nested Slice durations. Use non-overlapping intervals or report
individual top contributors.

## Thread-State Decision Tree

Summarize clipped `thread_state` duration using raw states, then map only known
values:

```text
Running  executing on a CPU
R, R+   runnable but not executing
S       sleeping or waiting; cause unknown until traced
D-IO    uninterruptible I/O wait
D, D-NIO uninterruptible wait not proven to be I/O
```

Branch on long continuous intervals and material interval share:

- `Running`: inspect CPU-overlap Callstacks and CPU placement.
- `R`/`R+`: inspect contemporaneous scheduling and priority.
- `S`: prove a dependency through the enclosing Slice and wakeup chain.
- `D-IO`: inspect I/O/syscall evidence.
- unknown: report the raw value without inventing semantics.

Do not use whole-Trace state percentages as a performance root cause. Event
loops and idle UI threads normally sleep outside their critical path.

## CPU Execution and Placement

`inspect_thread_execution` uses `sched_slice` for actual CPU execution. For each critical thread and
bounded interval, record:

- total clipped running time;
- schedule-Slice count;
- longest continuous running interval;
- running time and share per `cpu`;
- ordered CPU changes between adjacent schedule Slices;
- observed raw priority values.

Example CPU distribution:

```sql
SELECT s.cpu,
       SUM(MIN(s.ts_end, ?) - MAX(s.ts, ?)) AS running_ns,
       COUNT(*) AS schedule_slices
FROM sched_slice AS s
WHERE s.itid = ?
  AND s.ts < ?
  AND s.ts_end > ?
GROUP BY s.cpu
ORDER BY running_ns DESC
```

Calculate a migration only when consecutive scheduled Slices for the same
thread use different CPU IDs. Do not call migration harmful without repeated
short runs, runnable delay, or other supporting evidence.

CPU ID alone does not identify a big or little core. Infer core class or
frequency only from explicit topology/frequency data collected in the Trace.

## Runnable Delay and Priority

For every material `R`/`R+` interval:

1. Clip the runnable interval to the bounded analysis interval.
2. Inspect overlapping `sched_slice` rows across CPUs.
3. Resolve occupying `itid`/`ipid` values to thread and process names.
4. Compare raw priorities only after establishing platform ordering semantics.
5. Look for a repeated pattern, not one boundary-sized scheduling transition.
6. Check whether affinity, scheduling groups, RTG, or CPU eligibility are
   observable before judging scheduler behavior.

Classify conservatively:

- CPUs busy with equal/higher-priority work: observed CPU contention.
- target repeatedly waits while eligible CPUs run lower-priority work:
  suspected scheduling/configuration issue.
- eligibility or priority semantics missing: report contention and limitation,
  not scheduler error.

Do not use SQLite functions absent from the bundled engine, such as
`PERCENTILE`. Use ordered rows/window functions or report min/max/average.

## Sleep and Wakeup Chains

`S` is not a cause. Trace a sleep only when it delays critical-path progress.

For a material sleep interval:

1. Find the enclosing Callstack on the waiting thread.
2. Find `instant.name = 'sched_wakeup'` near the sleep end where
   `ref_type = 'itid'` and `ref = waiting_itid`.
3. Treat `wakeup_from` as the candidate waker `itid`.
4. Resolve the waker thread/process.
5. Inspect the waker Callstack at the wakeup timestamp.
6. Continue only when the waker itself waited on the same causal path.

Limit recursion to five hops, stop on repeated `itid`, and lower confidence
when the wakeup is not temporally adjacent to the sleep end.

Do not equate an enclosing Binder, futex, epoll, or I/O name with a confirmed
cause unless the wakeup/dependency timing supports it.

## Conclusion Rules

Use these minimum evidence combinations:

- CPU-bound: long clipped Running time plus CPU-overlap Callstack evidence.
- CPU contention: long Runnable time plus contemporaneous CPU occupants.
- scheduling issue: contention plus established priority semantics and CPU
  eligibility/configuration evidence.
- blocked wait: critical-path Sleep plus enclosing wait and matching wakeup.
- I/O wait: critical-path wait plus explicit I/O/syscall evidence.

Use `confirmed` only when the delay, critical-path dependency, and cause are
independently evidenced. Otherwise use `observed` or `suspected`.
