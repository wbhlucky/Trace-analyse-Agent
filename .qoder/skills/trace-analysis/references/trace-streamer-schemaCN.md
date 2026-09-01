# Trace Streamer 4.3.7 核心模式

在编写 `query_trace_sql` 的 SQL 之前使用此参考。它记录了绑定的 Trace Streamer 4.3.7 生成的 SQLite 模式中与性能分析相关的子集。当数据源未被采集时，表可能存在但行数为零。

## 目录

- [时间和标识符规则](#时间和标识符规则)
- [核心关系](#核心关系)
- [边界和标记表](#边界和标记表)
- [Slice 表](#slice-表)
- [调度表](#调度表)
- [帧表](#帧表)
- [查询规则](#查询规则)
- [查询模式](#查询模式)

## 时间和标识符规则

- 除非字段明确另有说明，将 `ts`、`start_ts`、`end_ts`、`dur`、`start_time` 和 `end_time` 视为纳秒。
- 保持整数纳秒计算。仅使用 `milliseconds = nanoseconds / 1000000.0` 转换显示结果。
- 将时间戳视为 Trace 单调时间线上的位置，而非墙上时钟时间戳。
- 从 `trace_range(start_ts, end_ts)` 读取采集区间。
- `ipid` 和 `itid` 是 Trace Streamer 内部进程和线程标识符。
- `pid` 和 `tid` 是操作系统标识符。不要将 `itid` 连接到 `tid`，或将 `ipid` 连接到 `pid`。
- 连接 `thread.ipid = process.ipid`。
- 通过 `process.name` 和 `thread.name` 解析名称；名称可能为 null 或截断，因此在证据中保留 ID。

## 核心关系

```text
process.ipid
  └── thread.ipid
        ├── callstack.callid = thread.itid
        ├── sched_slice.itid = thread.itid
        ├── thread_state.itid = thread.itid
        ├── instant.ref 当 instant.ref_type = 'itid' 时
        └── frame_slice.itid = thread.itid

callstack.id
  ├── callstack.parent_id
  └── frame_slice.callstack_id

frame_maps.src_row / frame_maps.dst_row
  └── frame_slice.id
```

即使某个样本恰好有 `id = ipid` 或 `id = itid`，也要使用 `ipid`/`itid` 连接；这些列的相等性不是契约。

## 边界和标记表

### `trace_range`

```text
start_ts INT, end_ts INT
```

使用此表验证每个请求的区间。

### `instant`

```text
ts INT, name TEXT, ref INT, wakeup_from INT, ref_type TEXT, value REAL
```

`instant` 包含点事件。根据 `ref_type` 解释 `ref`；对于 `ref_type = 'itid'`，将 `ref` 连接到 `thread.itid`。不要假设每个 instant 都是用户标记：调度器唤醒也出现在这里。

### `app_startup`

```text
id INT, call_id INT, ipid INT, tid INT,
start_time INT, end_time INT, start_name INT, packed_name TEXT|INT
```

仅当行存在时使用此表。`start_name` 是阶段标签，通常通过 `data_dict(id, data)` 由字典支持。`packed_name` 是目标应用或包名；解析器版本可能将其存储为直接文本或字典 ID，因此支持两种表示。

`ipid` 标识执行记录阶段的进程，可连接到 `process.ipid`；它不是目标应用进程的证明。启动阶段可以跨系统和应用进程。将 `call_id` 和 `tid` 保留为原始字段，除非它们的关系对当前 Trace Streamer 模式已验证；不要假设 `tid` 是 OS 线程 ID 或盲目连接。

阶段行可能缺失、嵌套、重叠或不完整；不要盲目求和。在没有独立进程、Slice 和帧证据的情况下，不要将最早阶段开始等同于启动请求，或将最晚阶段结束等同于呈现。对于冷启动语义，遵循选定场景 Skill 的 SmartPerf AppStartup 参考。

标记类范围也常出现在 `callstack` 中。在确定边界缺失之前，搜索点事件和 Slice 名称。

## Slice 表

### `callstack`

```text
id INT, ts INT, dur INT, callid INT, cat TEXT, name TEXT, depth INT,
cookie INT, parent_id INT, argsetid INT, chainId TEXT, spanId TEXT,
parentSpanId TEXT, flag TEXT, trace_level TEXT, trace_tag TEXT,
custom_category TEXT, custom_args TEXT, child_callid INT
```

- 连接 `callid = thread.itid`。
- 将区间解释为 `[ts, ts + dur)`。
- 使用 `depth` 和 `parent_id` 进行嵌套。
- 仅当 `parent_id` 解析为相关线程/区间中的真实 `callstack.id` 时，才将其视为有效；可能出现根或哨兵值。
- 仅当需要参数详情时，使用 `argsetid` 配合 `args.argset`。
- 在 `name` 上使用参数化的 `LIKE` 进行候选发现。在没有周围证据的情况下，不要将名称分类为起始、响应或完成边界。

### `args` 和 `data_dict`

```text
args(id, key, datatype, value, argset)
data_dict(id, data)
```

`args.key` 由字典支持。`value` 的解释取决于 `datatype`；不要盲目地将每个 value 连接到 `data_dict`。

## 调度表

### `sched_slice`

```text
id INT, ts INT, dur INT, ts_end INT, cpu INT, itid INT, ipid INT,
end_state TEXT, priority INT, arg_setid INT
```

此表描述实际在 CPU 上调度的时间。将 `itid` 连接到 `thread.itid`，使用 `[ts, ts_end)`；在新的 Trace 中依赖它之前，验证 `ts_end = ts + dur`。

### `thread_state`

```text
id INT, ts INT, dur INT, cpu INT, itid INT, tid INT, pid INT,
state TEXT, arg_setid INT
```

此表描述线程状态区间。使用 `thread.itid` 连接，而非 `thread.tid`。常见的观察状态包括：

```text
Running  在 CPU 上执行
R, R+   可运行
S       睡眠或等待
D       不可中断等待
D-IO    不可中断 I/O 等待
D-NIO   不可中断非 I/O 等待
X       退出/死亡状态
```

将未知状态值报告为观察到的；不要凭空创造语义映射。在求和持续时间之前，将区间裁剪到请求的分析窗口。

## 帧表

### `frame_slice`

```text
id INT, ts INT, vsync INT, ipid INT, itid INT, callstack_id INT,
dur INT, src TEXT, dst INT, type INT, type_desc TEXT, flag INT,
depth INT, frame_no INT
```

- 将 `ipid`/`itid` 连接到进程和线程。
- 当非 null 且可解析时，将 `callstack_id` 连接到 `callstack.id`。
- Trace Streamer 4.3.7 对实际帧行发出字面值 `type_desc = 'actural'`，对预期行发出 `type_desc = 'expect'`。在 SQL 过滤中保留此拼写。
- 不要仅将实际行 `dur` 视为显示帧区间，或仅凭 `dur > refresh_period` 就声明卡顿。将实际行和预期行、它们的时间戳、映射和相邻帧关联起来。

### `frame_maps`

```text
id INT, src_row INT, dst_row INT
```

`src_row` 和 `dst_row` 引用 `frame_slice.id`，表达帧关系。保留一对多映射；不要假设一个源恰好映射到一个目标。

## 查询规则

1. 仅使用 `query_trace_sql` 执行单个只读语句。
2. 通过 `parameters` 传值；永远不要将用户或 Trace 文本插值到 SQL 中。
3. 仅选择所需的列。在模式探索后避免 `SELECT *`。
4. 尽可能通过验证的时间区间限制事件查询。
5. 有意识地设置 `max_rows`，使用聚合或第二次查询，而非请求无限制的事件列表。
6. 在证据中包含稳定的 ID、时间戳和持续时间，而不仅仅是名称。
7. 将零行视为可能的采集间隙，并在限制中报告。
8. 使用 SQL 处理事实和精确算术；使用 Agent 进行语义边界选择、假设检验和根因决策。

## 查询模式

解析进程及其线程：

```sql
SELECT p.ipid, p.pid, p.name AS process_name,
       t.itid, t.tid, t.name AS thread_name, t.is_main_thread
FROM process AS p
JOIN thread AS t ON t.ipid = p.ipid
WHERE p.name LIKE ?
ORDER BY t.is_main_thread DESC, t.itid
LIMIT 100
```

查找 Slice 边界候选：

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

汇总 `[start_ns, end_ns)` 内裁剪后的线程状态：

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