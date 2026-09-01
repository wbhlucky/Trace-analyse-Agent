# Trace Streamer 4.3.7 Perf 模式

在 `get_trace_overview` 报告 `perf-samples` 后，使用此参考进行 Perf 查询。该能力意味着 `perf_sample` 至少包含一行；相关表仍可能缺失或不完整。

## 目录

- [时间和标识符规则](#时间和标识符规则)
- [采集元数据](#采集元数据)
- [样本和线程](#样本和线程)
- [调用链和符号](#调用链和符号)
- [NAPI 异步样本](#napi-异步样本)
- [有限查询模式](#有限查询模式)

## 时间和标识符规则

- 使用 `perf_sample.timestamp_trace` 将样本与 Trace Streamer 的 `ts` 值和场景边界对齐。
- 将 `perf_sample.timeStamp` 视为 Perf 原生时间戳。在测量和验证偏移之前，不要将其与 Trace 时间戳混合。
- `perf_sample.thread_id` 和 `perf_thread.thread_id` 是 OS TID。
- `perf_thread.process_id` 是 OS PID。
- 将这些 ID 连接到 `thread.tid` 和 `process.pid`；永远不要将它们连接到 Trace 内部的 `itid` 或 `ipid`。
- 使用半开区间过滤：`timestamp_trace >= start_ns AND timestamp_trace < end_ns`。

## 采集元数据

### `perf_report`

```text
id INT, report_type TEXT, report_value TEXT
```

观察到的报告类型包括 `config_name` 和 `cmdline`。读取每一行。仅在存在时提取事件配置、目标范围、频率、调用栈模式和时钟。将未知选项保留为采集限制。

当采集了多个事件时，不要假设一个配置映射到 `event_type_id = 0`。当显式映射不可用时，保留数字事件类型。

## 样本和线程

### `perf_sample`

```text
id INT, callchain_id INT, timeStamp INT, thread_id INT,
event_count INT, event_type_id INT, timestamp_trace INT,
cpu_id INT, thread_state TEXT
```

- 将 `callchain_id` 连接到 `perf_callchain.callchain_id`。
- 使用 `event_count` 作为事件特定权重，使用行数作为样本计数。
- 独立聚合不同的 `event_type_id` 值。
- `cpu_id` 是采样时的 CPU。
- 不要将 `thread_state` 用作调度器真相。它可能包含不可用的哨兵值，如 `-`；使用 `thread_state` 和 `sched_slice` Trace 表。

### `perf_thread`

```text
id INT, thread_id INT, process_id INT, thread_name TEXT
```

连接 `perf_sample.thread_id = perf_thread.thread_id`。在假设跨长 Trace 的唯一性之前，验证一个 OS TID 是否有多个映射。

## 调用链和符号

### `perf_callchain`

```text
id INT, callchain_id INT, depth INT, ip INT, vaddr_in_file INT,
offset_to_vaddr INT, file_id INT, symbol_id INT, name INT,
source_file_id INT, line_number INT
```

- 连接 `perf_sample.callchain_id = perf_callchain.callchain_id`。
- 在本项目使用的绑定的 4.3.7 输出中，depth zero 是外部进程/根帧，增加 depth 向采样叶遍历。终端帧是该样本的最大 depth。当解析器版本更改时验证这一点，而非假设 depth zero 是 self。
- 通过 `data_dict.id` 解析 `name`；使用 `data_dict.data` 作为显示的帧标签。
- 包含 `module+0x...` 的标签是地址标签，而非完全解析的函数。

### `perf_files`

```text
id INT, file_id INT, serial_id INT, symbol TEXT, path TEXT
```

对于精确的已解析符号，同时连接：

```sql
pf.file_id = pc.file_id AND pf.serial_id = pc.symbol_id
```

不要仅连接 `file_id`：`perf_files` 通常对一个文件有多行符号，该连接会倍增调用链行。当 `symbol_id < 0` 时，通过 `data_dict` 解析帧标签，并通过按 `file_id` 分组的单行子查询获取模块路径。

## NAPI 异步样本

### `perf_napi_async`

```text
id INT, ts INT, traceid TEXT, cpu_id INT, thread_id INT, process_id INT,
caller_callchainid INT, callee_callchainid INT, perf_sample_id INT,
event_count INT, event_type_id INT
```

仅当 JavaScript/NAPI 异步因果关系相关时使用。保留调用者和被调用者调用链，并关联 `perf_sample_id`；不要从此表推断一般唤醒关系。

## 有限查询模式

检查配置和样本覆盖：

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

按 OS 线程汇总对齐的窗口：

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

解析有限调用链帧而不倍增符号：

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

使用有限聚合查询获取最终热点汇总。如果需要超过 500 个原始帧，在 SQL 中聚合而非请求无限制的行。