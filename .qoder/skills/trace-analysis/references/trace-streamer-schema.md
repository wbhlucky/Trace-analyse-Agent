# Trace Streamer 4.3.7 Core Schema

Use this reference before writing SQL for `query_trace_sql`. It documents the
performance-analysis subset of the SQLite schema produced by the bundled Trace
Streamer 4.3.7. A table may exist with zero rows when its data source was not
collected.

## Contents

- [Time and identifier rules](#time-and-identifier-rules)
- [Core relationships](#core-relationships)
- [Boundary and marker tables](#boundary-and-marker-tables)
- [Slice tables](#slice-tables)
- [Scheduling tables](#scheduling-tables)
- [Frame tables](#frame-tables)
- [Query rules](#query-rules)
- [Query patterns](#query-patterns)

## Time and Identifier Rules

- Treat `ts`, `start_ts`, `end_ts`, `dur`, `start_time`, and `end_time` as
  nanoseconds unless a field is explicitly documented otherwise.
- Keep calculations in integer nanoseconds. Convert only displayed results with
  `milliseconds = nanoseconds / 1000000.0`.
- Treat timestamps as positions on the Trace monotonic timeline, not wall-clock
  timestamps.
- Read the captured interval from `trace_range(start_ts, end_ts)`.
- `ipid` and `itid` are Trace Streamer internal process and thread identifiers.
- `pid` and `tid` are operating-system identifiers. Do not join an `itid` to a
  `tid`, or an `ipid` to a `pid`.
- Join `thread.ipid = process.ipid`.
- Resolve names through `process.name` and `thread.name`; names may be null or
  truncated, so retain IDs in evidence.

## Core Relationships

```text
process.ipid
  └── thread.ipid
        ├── callstack.callid = thread.itid
        ├── sched_slice.itid = thread.itid
        ├── thread_state.itid = thread.itid
        ├── instant.ref when instant.ref_type = 'itid'
        └── frame_slice.itid = thread.itid

callstack.id
  ├── callstack.parent_id
  └── frame_slice.callstack_id

frame_maps.src_row / frame_maps.dst_row
  └── frame_slice.id
```

Use `ipid`/`itid` joins even when a sample happens to have `id = ipid` or
`id = itid`; equality of those columns is not the contract.

## Boundary and Marker Tables

### `trace_range`

```text
start_ts INT, end_ts INT
```

Use this table to validate every requested interval.

### `instant`

```text
ts INT, name TEXT, ref INT, wakeup_from INT, ref_type TEXT, value REAL
```

`instant` contains point events. Interpret `ref` according to `ref_type`; for
`ref_type = 'itid'`, join `ref` to `thread.itid`. Do not assume every instant is
a user marker: scheduler wakeups also appear here.

### `app_startup`

```text
id INT, call_id INT, ipid INT, tid INT,
start_time INT, end_time INT, start_name INT, packed_name TEXT|INT
```

Use this table only when rows exist. `start_name` is the stage label and is
normally dictionary-backed through `data_dict(id, data)`. `packed_name` is the
target application or bundle name; parser versions may store it as direct text
or as a dictionary ID, so support both representations.

`ipid` identifies the process executing the recorded phase and may be joined to
`process.ipid`; it is not proof that this is the target application process.
Startup stages can cross system and application processes. Preserve `call_id`
and `tid` as raw fields unless their relationship is verified for the current
Trace Streamer schema; do not assume that `tid` is an OS thread ID or join it
blindly.

Stage rows can be missing, nested, overlapping, or incomplete; do not sum them
blindly. Do not equate the earliest stage start with the launch request or the
latest stage end with presentation without independent process, Slice, and
frame evidence. For cold-start semantics, follow the selected scenario Skill's
SmartPerf AppStartup reference.

Marker-like ranges also commonly appear in `callstack`. Search both point
events and Slice names before deciding that a boundary is absent.

## Slice Tables

### `callstack`

```text
id INT, ts INT, dur INT, callid INT, cat TEXT, name TEXT, depth INT,
cookie INT, parent_id INT, argsetid INT, chainId TEXT, spanId TEXT,
parentSpanId TEXT, flag TEXT, trace_level TEXT, trace_tag TEXT,
custom_category TEXT, custom_args TEXT, child_callid INT
```

- Join `callid = thread.itid`.
- Interpret the interval as `[ts, ts + dur)`.
- Use `depth` and `parent_id` for nesting.
- Treat `parent_id` as valid only when it resolves to a real `callstack.id` in
  the relevant thread/interval; root or sentinel values can occur.
- Use `argsetid` with `args.argset` only when argument details are necessary.
- Use parameterized `LIKE` on `name` for candidate discovery. Do not classify a
  name as a start, response, or completion boundary without surrounding
  evidence.

### `args` and `data_dict`

```text
args(id, key, datatype, value, argset)
data_dict(id, data)
```

`args.key` is dictionary-backed. The interpretation of `value` depends on
`datatype`; do not blindly join every value to `data_dict`.

## Scheduling Tables

### `sched_slice`

```text
id INT, ts INT, dur INT, ts_end INT, cpu INT, itid INT, ipid INT,
end_state TEXT, priority INT, arg_setid INT
```

This table describes time actually scheduled on a CPU. Join `itid` to
`thread.itid` and use `[ts, ts_end)`; verify `ts_end = ts + dur` before relying
on it in a new Trace.

### `thread_state`

```text
id INT, ts INT, dur INT, cpu INT, itid INT, tid INT, pid INT,
state TEXT, arg_setid INT
```

This table describes thread-state intervals. Join with `thread.itid`, not
`thread.tid`. Common observed states include:

```text
Running  executing on a CPU
R, R+   runnable
S       sleeping or waiting
D       uninterruptible wait
D-IO    uninterruptible I/O wait
D-NIO   uninterruptible non-I/O wait
X       exit/dead state
```

Report unknown state values as observed; do not invent a semantic mapping.
Clip intervals to the requested analysis window before summing duration.

## Frame Tables

### `frame_slice`

```text
id INT, ts INT, vsync INT, ipid INT, itid INT, callstack_id INT,
dur INT, src TEXT, dst INT, type INT, type_desc TEXT, flag INT,
depth INT, frame_no INT
```

- Join `ipid`/`itid` to process and thread.
- Join `callstack_id` to `callstack.id` when non-null and resolvable.
- Trace Streamer 4.3.7 emits the literal `type_desc = 'actural'` for actual
  frame rows and `type_desc = 'expect'` for expected rows. Preserve this
  misspelling in SQL filters.
- Do not treat actual-row `dur` alone as the display frame interval or declare
  jank solely from `dur > refresh_period`. Correlate actual and expected rows,
  their timestamps, mapping, and adjacent frames.

### `frame_maps`

```text
id INT, src_row INT, dst_row INT
```

`src_row` and `dst_row` reference `frame_slice.id` and express frame
relationships. Preserve one-to-many mappings; do not assume one source maps to
exactly one destination.

## Query Rules

1. Use `query_trace_sql` only for a single read-only statement.
2. Pass values through `parameters`; never interpolate user or Trace text into
   SQL.
3. Select only required columns. Avoid `SELECT *` after schema exploration.
4. Bound event queries by a validated time interval whenever possible.
5. Set `max_rows` deliberately and use aggregation or a second query instead of
   requesting an unbounded event list.
6. Include stable IDs, timestamps, and durations in evidence, not names alone.
7. Treat zero rows as a possible collection gap and report it in limitations.
8. Use SQL for facts and exact arithmetic; use the Agent for semantic boundary
   selection, hypothesis testing, and root-cause decisions.

## Query Patterns

Resolve a process and its threads:

```sql
SELECT p.ipid, p.pid, p.name AS process_name,
       t.itid, t.tid, t.name AS thread_name, t.is_main_thread
FROM process AS p
JOIN thread AS t ON t.ipid = p.ipid
WHERE p.name LIKE ?
ORDER BY t.is_main_thread DESC, t.itid
LIMIT 100
```

Find Slice boundary candidates:

```sql
SELECT c.id, c.ts, c.dur, c.name, c.cat, c.depth,
       p.ipid, p.pid, p.name AS process_name,
       t.itid, t.tid, t.name AS thread_name
FROM callstack AS c
JOIN thread AS t ON t.itid = c.callid
JOIN process AS p ON p.ipid = t.ipid
WHERE c.name LIKE ?
  AND c.ts < ?
  AND c.ts + c.dur > ?
ORDER BY c.ts
LIMIT 100
```

Summarize clipped thread states inside `[start_ns, end_ns)`:

```sql
SELECT s.state,
       SUM(MIN(s.ts + s.dur, ?) - MAX(s.ts, ?)) AS clipped_dur_ns,
       COUNT(*) AS intervals
FROM thread_state AS s
WHERE s.itid = ?
  AND s.ts < ?
  AND s.ts + s.dur > ?
GROUP BY s.state
ORDER BY clipped_dur_ns DESC
```
