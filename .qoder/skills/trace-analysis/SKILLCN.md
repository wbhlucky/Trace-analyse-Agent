---
name: trace-analysis
description: "协调基于证据的性能 Trace 分析，涵盖四种支持的问题类型：冷启动、响应延迟、完成延迟和帧率或卡顿。作为每个 DitingAgent 任务的顶层方法论使用，验证采集范围，强制有限的只读工具使用，路由到匹配的场景 Skill，检验假设，并在不进行内存分析的情况下生成可追溯的发现。"
---

# Trace 分析

使用工具获取事实，使用场景 Skill 进行问题特定的决策。仅分析性能。不要调查内存使用、泄漏、GC 压力或 OOM。

## 路由场景

尊重显式的 `scenario_type`；不要重新分类：

- `cold-start` → 使用 `cold-start-analysis`
- `response-latency` → 使用 `response-latency-analysis`
- `completion-latency` → 使用 `completion-latency-analysis`
- `frame-jank` → 使用 `frame-jank-analysis`

## 通用工作流

1. 确认场景、症状、设备/构建上下文、请求的时间范围、问题持续时间、应用 Marker 提示、基线可用性和 Trace 采集限制。
2. 检查预加载的 `get_trace_overview` 证据。仅当它在 `preloaded_evidence` 中缺失时才调用它；确定性预检通常在模型启动前提供它。
3. 当 Trace 数据库可用时，首先检查预加载的场景候选证据，然后在区间尚未由显式 `time_range` 完全固定，或提供了应用 Marker 提示时，调用一次 `inspect_problem_window_candidates`。当存在 `end_marker` 时传入它；否则传入场景适当的 `response_marker` 或 `completion_marker`。对于缺失的值传入空字符串和 `0`。当预检已提供时，不要重复相同的调用。
4. 遵循选定的场景 Skill 建立其所需的边界和指标。
5. 当 `perf-samples` 存在时，在问题区间确定后应用已启用的 `perf-sample-analysis` Skill。Perf 必须与该区间及其 Trace 关键路径关联。
6. 在第一次 `query_trace_sql` 调用之前，阅读 `references/trace-streamer-schema.md`。使用其固定标识符、连接、时间单位和有限查询模式；永远不要猜测表语义。
7. 形成最多三个竞争的、可证伪的假设。
8. 选择能检验每个假设的最窄可用工具。当没有更窄的确定性工具存在时，使用 `query_trace_sql` 作为通用的只读数据路径。
9. 当区间依赖于一个或多个关键线程时，阅读 `references/thread-execution-critical-path.md` 并应用通用的 CPU、调度、优先级、竞争、睡眠和唤醒链方法。
10. 构建证据链：

   ```text
   症状 → 边界和指标 → 假设 → 工具结果
   → 根因状态 → 建议 → 回归检查
   ```

11. 当证据充分或可用能力无法检验剩余假设时停止。

## 确定问题区间

使用此优先级并记录哪个规则胜出：

1. 显式的用户 `time_range`。
2. 用户或应用定义的起始/结束 Slice 对，当两个标识符在目标进程中唯一解析且结束在起始之后时。
3. 选定场景的语义边界。例如，应用的冷启动完成可能是其第一个稳定的首页帧，而非 Trace 中任何位置可见的最早帧。
4. 作为通用回退方案，假设 Trace 包含一个用户操作，选择最后有效的 TouchEvent、PointerEvent 或点击点作为起始，并结合 `problem_duration_ms` 推导结束。

单操作规则是显式的回退假设，而非 Trace 事实。当多个操作、不相关的晚期输入标记、不完整的 Trace 覆盖或矛盾的业务上下文可见时，拒绝它或降低置信度。没有持续时间或可靠结束 Marker 的最后输入点仅是一个起始候选，而非完整区间。

当选择了完整区间时，用其指标定义、精确边界、持续时间、胜出的选择规则、证据 ID 和 `single_operation_assumption` 标志填充 `problem_interval`。对于应用定义的冷启动边界，使用边界类型 `application-defined-start`、`application-marker-start`、`application-defined-completion`、`application-marker-end` 或 `stable-home-frame` 作为适用项，以便标准平台首帧候选保持为比较指标，而非不正确的验证器覆盖。

应用 Marker 名称不是全局硬编码的。将 `start_marker`/`end_marker` 和场景特定的响应/完成标记视为可扩展性接口。子串发现可能提出候选，但自动配对要求在目标范围内有一个唯一的起始和一个唯一的结束。

## 证据契约

- 仅引用工具返回的证据 ID。
- 将观察到的事实与解释分开。
- 对于没有已证明原因的直接 Trace 事实使用 `observed`。
- 当证据支持某个原因但替代方案仍存在时使用 `suspected`。
- 仅当独立证据或基线比较排除了可信的替代方案时使用 `confirmed`。
- 将缺失的标记、计数器、区间、工具和采集间隙放入 `limitations`。
- 永远不要从文件名、文件大小、提示或一般性能知识推断指标。
- 最终 Agent 草稿是有意保持语义化和紧凑的。永远不要将确定性的 `critical_threads`、CPU/状态/唤醒投影或 `perf` 配置文件复制到其中；应用层从证据中注入它们。

## 允许的诊断维度

在四种支持的问题类型中，调查 CPU 执行、调度、锁、IPC、同步 I/O、任务队列和渲染作为可能的原因。

仅在相关且 Trace 能力支持时加载领域参考：

- 编写 Trace SQL 前阅读 `references/trace-streamer-schema.md`
- `references/thread-execution-critical-path.md`
- `references/io-analysis.md`
- `references/rendering-analysis.md`
- `references/report-standard.md`