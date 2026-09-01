# 线程执行和调度关键路径

每当有限性能区间依赖于一个或多个关键线程时，阅读此参考。使用此 Skill 中的固定 Trace Streamer 模式。

## 目录

- [区间规范](#区间规范)
- [线程状态决策树](#线程状态决策树)
- [CPU 执行和分配](#cpu-执行和分配)
- [可运行延迟和优先级](#可运行延迟和优先级)
- [睡眠和唤醒链](#睡眠和唤醒链)
- [结论规则](#结论规则)

## 区间规范

一次分析一个有限区间和一个关键线程。将任何重叠区间裁剪到 `[interval_start, interval_end)`：

```sql
MIN(ts + dur, ?) - MAX(ts, ?)
```

将墙钟时间和 CPU 运行时间分开。一个长的 `callstack.dur` 可能包含睡眠或可运行延迟。通过将 Slice 与 `thread_state.state = 'Running'` 或等效的 `sched_slice` 区间相交来测量 CPU 工作。

在填充 `critical_threads` 条目之前，使用精确的区间和 Trace 内部 `itid` 调用 `inspect_thread_execution`。使用其模型就绪的 `thread_execution` 字段并引用其证据 ID。该工具确定性地裁剪状态和调度切片，因此不要用人工聚合的 SQL 替换其值。整个问题窗口的结果永远不得复制到阶段级条目中；使用该阶段的精确边界再次调用工具。

不要对嵌套的 Slice 持续时间求和。使用非重叠区间或报告单个顶级贡献者。

## 线程状态决策树

使用原始状态汇总裁剪后的 `thread_state` 持续时间，然后仅映射已知值：

```text
Running  在 CPU 上执行
R, R+   可运行但未执行
S       睡眠或等待；原因未知，直到追踪到
D-IO    不可中断 I/O 等待
D, D-NIO 不可中断等待，未证明为 I/O
```

根据长连续区间和实质区间份额分支：

- `Running`：检查 CPU 重叠的 Callstack 和 CPU 分配。
- `R`/`R+`：检查同期的调度和优先级。
- `S`：通过包含的 Slice 和唤醒链证明依赖关系。
- `D-IO`：检查 I/O/系统调用证据。
- 未知：报告原始值而不凭空创造语义。

不要使用整个 Trace 的状态百分比作为性能根因。事件循环和空闲 UI 线程在其关键路径之外通常处于睡眠状态。

## CPU 执行和分配

`inspect_thread_execution` 使用 `sched_slice` 获取实际的 CPU 执行。对于每个关键线程和有限区间，记录：

- 裁剪后的总运行时间；
- 调度 Slice 数量；
- 最长连续运行区间；
- 每个 `cpu` 的运行时间和份额；
- 相邻调度 Slice 之间的有序 CPU 变化；
- 观察到的原始优先级值。

CPU 分布示例：

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

仅当同一线程的连续调度 Slice 使用不同的 CPU ID 时，才计算迁移。在没有重复短运行、可运行延迟或其他支持证据的情况下，不要称迁移有害。

仅 CPU ID 不能识别大核或小核。仅当 Trace 中收集了显式的拓扑/频率数据时，才推断核心类别或频率。

## 可运行延迟和优先级

对于每个实质性的 `R`/`R+` 区间：

1. 将可运行区间裁剪到有限分析区间。
2. 检查跨 CPU 的重叠 `sched_slice` 行。
3. 将占用的 `itid`/`ipid` 值解析为线程和进程名称。
4. 仅在确定平台排序语义后比较原始优先级。
5. 寻找重复模式，而非一次边界大小的调度转换。
6. 在判断调度器行为之前，检查亲和性、调度组、RTG 或 CPU 资格是否可观察。

保守分类：

- CPU 忙于同等/更高优先级工作：观察到 CPU 竞争。
- 目标反复等待而符合条件的 CPU 运行较低优先级工作：疑似调度/配置问题。
- 资格或优先级语义缺失：报告竞争和限制，而非调度器错误。

不要使用绑定的引擎中不存在的 SQLite 函数，如 `PERCENTILE`。使用有序行/窗口函数或报告最小/最大/平均。

## 睡眠和唤醒链

`S` 不是原因。仅当睡眠延迟了关键路径进度时才追踪它。

对于实质性睡眠区间：

1. 找到等待线程上包含的 Callstack。
2. 在睡眠结束附近找到 `instant.name = 'sched_wakeup'`，其中 `ref_type = 'itid'` 且 `ref = waiting_itid`。
3. 将 `wakeup_from` 视为候选唤醒者 `itid`。
4. 解析唤醒者线程/进程。
5. 在唤醒时间戳检查唤醒者 Callstack。
6. 仅当唤醒者本身在同一因果路径上等待时才继续。

将递归限制为五跳，在重复的 `itid` 上停止，当唤醒在时间上不与睡眠结束相邻时降低置信度。

不要将包含的 Binder、futex、epoll 或 I/O 名称等同于已确认的原因，除非唤醒/依赖时序支持它。

## 结论规则

使用以下最低证据组合：

- CPU 绑定：长裁剪后 Running 时间加上 CPU 重叠 Callstack 证据。
- CPU 竞争：长 Runnable 时间加上同期 CPU 占用者。
- 调度问题：竞争加上已确定的优先级语义和 CPU 资格/配置证据。
- 阻塞等待：关键路径 Sleep 加上包含的等待和匹配的唤醒。
- I/O 等待：关键路径等待加上显式的 I/O/系统调用证据。

仅当延迟、关键路径依赖和原因被独立证明时使用 `confirmed`。否则使用 `observed` 或 `suspected`。