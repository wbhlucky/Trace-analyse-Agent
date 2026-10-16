# Trace Streamer 4.3.7 Perf Schema

Use this reference for Perf queries after `get_trace_overview` reports
`perf-samples`. The capability means `perf_sample` contains at least one row;
related tables can still be absent or incomplete.

## Contents

- [Time and identifier rules](#time-and-identifier-rules)
- [Collection metadata](#collection-metadata)
- [Samples and threads](#samples-and-threads)
- [Call chains and symbols](#call-chains-and-symbols)
- [NAPI async samples](#napi-async-samples)
- [Bounded query patterns](#bounded-query-patterns)

## Time and Identifier Rules

- Use `perf_sample.timestamp_trace` to align samples with Trace Streamer `ts`
  values and scenario boundaries.
- Treat `perf_sample.timeStamp` as the Perf-native timestamp. Do not mix it with
  Trace timestamps without measuring and validating the offset.
- `perf_sample.thread_id` and `perf_thread.thread_id` are OS TIDs.
- `perf_thread.process_id` is an OS PID.
- Join these IDs to `thread.tid` and `process.pid`; never join them to
  Trace-internal `itid` or `ipid`.
- Filter using half-open intervals:
  `timestamp_trace >= start_ns AND timestamp_trace < end_ns`.

## Collection Metadata

### `perf_report`

```text
id INT, report_type TEXT, report_value TEXT
```

Observed report types include `config_name` and `cmdline`. Read every row.
Extract the event configuration, target scope, frequency, call-stack mode, and
clock only when present. Preserve unknown options as collection limitations.

Do not assume one config maps to `event_type_id = 0` when multiple events were
collected. Keep the numeric event type when an explicit mapping is unavailable.

## Samples and Threads

### `perf_sample`

```text
id INT, callchain_id INT, timeStamp INT, thread_id INT,
event_count INT, event_type_id INT, timestamp_trace INT,
cpu_id INT, thread_state TEXT
```

- Join `callchain_id` to `perf_callchain.callchain_id`.
- Use `event_count` as event-specific weight and row count as sample count.
- Aggregate different `event_type_id` values independently.
- `cpu_id` is the sampled CPU.
- Do not use `thread_state` as scheduler truth. It can contain an unavailable
  sentinel such as `-`; use `thread_state` and `sched_slice` Trace tables.

### `perf_thread`

```text
id INT, thread_id INT, process_id INT, thread_name TEXT
```

Join `perf_sample.thread_id = perf_thread.thread_id`. Verify whether an OS TID
has more than one mapping before assuming uniqueness across a long Trace.

## Call Chains and Symbols

### `perf_callchain`

```text
id INT, callchain_id INT, depth INT, ip INT, vaddr_in_file INT,
offset_to_vaddr INT, file_id INT, symbol_id INT, name INT,
source_file_id INT, line_number INT
```

- Join `perf_sample.callchain_id = perf_callchain.callchain_id`.
- In the bundled 4.3.7 output used by this project, depth zero is the outer
  process/root frame and increasing depth walks toward the sampled leaf. The
  terminal frame is the maximum depth for that sample. Verify this when parser
  versions change instead of assuming that depth zero is self.
- Resolve `name` through `data_dict.id`; use `data_dict.data` as the displayed
  frame label.
- A label containing `module+0x...` is an address label, not a fully resolved
  function.

### `perf_files`

```text
id INT, file_id INT, serial_id INT, symbol TEXT, path TEXT
```

For an exact resolved symbol, join both:

```sql
pf.file_id = pc.file_id AND pf.serial_id = pc.symbol_id
```

Do not join only on `file_id`: `perf_files` commonly has many symbol rows for
one file and that join multiplies call-chain rows. When `symbol_id < 0`, resolve
the frame label through `data_dict` and obtain a module path through a
one-row-per-`file_id` grouped subquery.

## NAPI Async Samples

### `perf_napi_async`

```text
id INT, ts INT, traceid TEXT, cpu_id INT, thread_id INT, process_id INT,
caller_callchainid INT, callee_callchainid INT, perf_sample_id INT,
event_count INT, event_type_id INT
```

Use only when JavaScript/NAPI async causality is relevant. Preserve the caller
and callee call chains and correlate `perf_sample_id`; do not infer a general
wakeup relationship from this table.

## Bounded Query Patterns

Inspect configuration and sample coverage:

```sql
SELECT report_type, report_value
FROM perf_report
ORDER BY id
```

```sql
SELECT event_type_id,
       COUNT(*) AS sample_count,
       SUM(event_count) AS total_event_count,
       MIN(timestamp_trace) AS first_trace_ns,
       MAX(timestamp_trace) AS last_trace_ns,
       COUNT(DISTINCT thread_id) AS thread_count,
       COUNT(DISTINCT cpu_id) AS cpu_count
FROM perf_sample
GROUP BY event_type_id
ORDER BY event_type_id
```

Summarize an aligned window by OS thread:

```sql
SELECT s.event_type_id, t.process_id, s.thread_id, t.thread_name,
       COUNT(*) AS sample_count,
       SUM(s.event_count) AS total_event_count
FROM perf_sample AS s
LEFT JOIN perf_thread AS t ON t.thread_id = s.thread_id
WHERE s.timestamp_trace >= ?
  AND s.timestamp_trace < ?
GROUP BY s.event_type_id, t.process_id, s.thread_id, t.thread_name
ORDER BY total_event_count DESC
LIMIT 100
```

Resolve bounded call-chain frames without multiplying symbols:

```sql
WITH file_paths AS (
  SELECT file_id, MIN(path) AS path
  FROM perf_files
  GROUP BY file_id
)
SELECT s.id AS sample_id, s.timestamp_trace, s.thread_id,
       s.event_type_id, s.event_count, c.depth,
       d.data AS frame_name, f.path
FROM perf_sample AS s
JOIN perf_callchain AS c ON c.callchain_id = s.callchain_id
LEFT JOIN data_dict AS d ON d.id = c.name
LEFT JOIN file_paths AS f ON f.file_id = c.file_id
WHERE s.timestamp_trace >= ?
  AND s.timestamp_trace < ?
  AND s.thread_id = ?
ORDER BY s.timestamp_trace, s.id, c.depth
LIMIT 500
```

Use bounded aggregation queries for final hotspot totals. If more than 500 raw
frames are required, aggregate in SQL rather than requesting unbounded rows.
