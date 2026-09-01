---
name: perf-sample-analysis
description: 将有效的 Perf 采样数据与任何支持的性能场景的有限 Trace 关键路径关联起来。当 get_trace_overview 报告 perf-samples 能力时使用，包括冷启动、响应延迟、完成延迟和帧卡顿分析。检查采集范围和质量，通过 timestamp_trace 对齐样本，聚合 self 和 inclusive 事件权重及调用路径，并在不使用 Perf 凭空推断 off-CPU 原因的情况下解释 CPU 运行工作。
---

# Perf 样本分析

使用 Perf 作为在 CPU 上执行的代码的统计证据。始终将其与相同场景区间、进程/线程选择和 Trace 调度证据结合使用。不要用整个 Trace 的热点列表替代关键路径分析。

## 必需的工作流

1. 在问题区间和目标 OS PID/TID 确定后，调用一次 `inspect_perf_profile`。此确定性工具有预留预算，在普通 Trace/SQL 调用用尽后仍可用。传入精确的半开区间、目标 OS PID，以及仅 Trace 证据置于关键路径上的主/渲染/工作 OS TID。不要仅仅因为进程中的每个线程有样本就传入它们。
   使用空 `thread_ids` 列表的仅进程调用仅用于发现。如果需要，使用返回的线程分布选择相关的 TID，并再次调用工具；只有线程范围的证据可以被最终确定性 Perf 投影引用。
2. 使用返回的采集元数据、事件配置文件、线程/CPU 分布、符号化计数、应用热点、应用模块、上下文帧和证据 ID 进行推理。不要将它们序列化到 Agent 草稿中：应用层在提交后直接从该证据创建最终的 `analysis.perf`。
3. 仅当确定性工具报告具体的数据缺口，需要有针对性的 SQL 跟进时，才阅读 `references/perf-trace-streamer-schema.md`。不要花费普通 SQL 调用来复现 `inspect_perf_profile` 已返回的数据。
4. 确认 `perf_sample.timestamp_trace` 与已确定的问题区间重叠，并且采集的 PID/TID 范围覆盖关键路径。
5. 将 OS `process_id`/`thread_id` 与 Trace Streamer 的 `ipid`/`itid` 分开，并独立聚合每个事件：
   - 样本计数；
   - 总 `event_count`；
   - depth-zero self 样本和 self 事件权重；
   - inclusive 样本和事件权重；
   - 完整的热调用路径；
   - 每进程、线程和 CPU 分布。
6. 将限定范围的配置文件与 Trace 中的 `sched_slice`、`thread_state`、Slice、阶段、帧和唤醒证据关联起来。
7. 仅将 `bottom_up_diagnostics` 用作 Top-down 路径的内部辅助索引。诊断项仅在命名了具体的应用函数或具体操作、标记为根因候选，并且匹配的 Top-down Trace 阶段将其置于关键路径上时，才能合并到发现中。永远不要发布独立的 Bottom-up 表格或全局列表。
8. 将相关热点连接到语义发现，并引用 `inspect_perf_profile` 证据 ID。如果 Perf 对选定区间不可用，说明确切限制。Agent 草稿有意省略 `analysis.perf`；确定性注入在提交后填充它。

最终确定性的 `analysis.perf.thread_ids` 必须仅包含为深度关键路径 Perf 分析选定的线程，而非目标进程中每个有样本的线程。全进程线程分布可用于发现一次，但不相关的线程不得扩展到最终热点表、线程轨道或火焰图中。仅当 Trace 阶段、帧、调度或依赖证据使其相关时，才包含主线程和任何 Render/Display/Worker 线程。

## 采集和质量规则

- 解析 `perf_report`，而非假设事件或范围。
- 将 `-p` 视为进程范围，`-t` 视为线程范围，仅当记录的命令证明时才是系统范围采集。
- 不要使用进程范围的 Perf 来识别竞争进程中运行的代码。
- 将 `cpu-cycles`、`instructions`、`cpu-clock`、缓存事件和其他事件视为不同单位。永远不要将其原始权重相加或比较。
- 不要将硬件周期直接转换为纳秒。
- 仅当事件配置和采集范围兼容时，才比较两个窗口或 Trace。适当地按事件权重份额、样本份额或区间持续时间进行归一化。
- 报告低样本数、不完整的时间覆盖、未解析的符号、截断的栈、采集间隙和不兼容的基线。
- 区分地址标签和已解析的函数符号。当可用时保留模块路径。

## Trace 关联规则

使用 Trace 调度作为执行状态的权威：

- 长 `Running`：使用限定范围的 Perf 栈解释 CPU 工作。
- 长 `R`/`R+`：使用调度识别 CPU 占用者；线程未运行时，目标 Perf 样本缺失是预期的。
- 长 `S`：使用包含的 Slice 和唤醒链。Perf 不解释睡眠原因。
- `D-IO`：使用 I/O 和系统调用证据。
- 混合执行和等待：分别归因 CPU 工作和 off-CPU 延迟。

全局热点不是原因，除非它与有限的问题区间重叠，并且位于关键路径上、延迟关键路径或与之竞争。该路径之外的热函数可能是优化机会，但不是延迟根因。

## 热点语义

- 在绑定的 Trace Streamer 输出中，`depth=0` 是外部进程根，更大的深度向采样叶方向遍历。因此 `self` 属于每个样本最大深度的终端帧，而非盲目地属于 depth zero。当解析器版本更改时，再次验证方向。
- `inclusive` 表示函数出现在采样调用链中的任何位置。
- 避免通过递归重复帧膨胀 inclusive 份额；在计算归一化 inclusive 份额时，每个样本每个符号最多计数一次。
- 将 self 和 inclusive 样本计数与 `event_count` 权重分开。
- 不要仅因为父帧的 inclusive 权重高就称其为昂贵。
- 在推荐叶级优化之前，检查完整的重复调用路径。

## 应用层归属

Perf 结论必须到达应用层。将每个重复路径分为以下角色：

- **进程/运行时上下文**：`appspawn`、`libbegetutil`、加载器/libc 启动、`MainThread::Start` 和 `EventRunner::Run` 解释了进程如何进入栈。它们的高 inclusive 份额是预期的，因为它们是公共祖先。永远不要将它们报告为应用瓶颈或推荐优化它们。
- **框架/运行时桥接**：Ability/UI 框架、NAPI、Ark VM 调度、GC、加载器和系统库解释了机制。它们可能支持诸如模块评估或对象创建之类的结论，但本身不是应用修复点。
- **应用层**：应用 HAP/HSP/ABC/AOT 帧和位于应用 data/bundle 路径下的应用绑定的原生库。首先按模块分组，并将其与调用工作的 Trace 阶段和 Slice 关联起来。

仅将 `event_profiles[].hotspots` 用作应用层候选。`context_hotspots` 和 `runtime_hotspots` 是支持的调用上下文，而非根因。优先使用已解析的应用函数。当仅有 `app.hap+0x...` 或 `libfoo.so+0x...` 可用时，报告应用模块和偏移，解释符号不完整，并请求匹配的应用符号/AOT 映射以进行函数级归属。不要仅仅因为 `appspawn` 的百分比较大就回退到它。

主要按 inclusive 样本/事件份额对应用路径排名，然后使用 `self` 区分终端 CPU 工作和祖先。一个单样本叶不得仅因为其 self 样本为零的重复路径排名更高。同样，高 inclusive 份额证明路径成员资格，而非独占成本；在声明应用正在做什么之前，将路径与阶段 Slice 和终端/运行时帧结合起来。

## Bottom-up 关联

Bottom-up 是内部根因辅助工具，而非报告章节。它从终端 Self 工作开始，询问重复样本是否识别出已解析的应用函数或具体操作，如原生模块加载、Ark 模块评估、对象物化、图像解码、数据库工作、GC 或应用包页面错误。然后返回 Top-down 证明哪个应用路径、阶段和线程调用了该操作。

不要创建或暴露通用桶，如 `app.hap+offsets`、`libfoo.so+offsets`、`appspawn`、Ark 运行时调度、libc 或内核叶。它们不会告诉开发者要优化什么。未解析的应用模块偏移可以保留为 Top-down 路径上下文，但它们不是 Bottom-up 诊断。

`bottom_up_diagnostics` 项仅在满足以下所有条件时才能影响发现或建议：

- 它识别了具体的应用函数或具体的运行时操作；
- `root_cause_candidate` 为 true 且样本/事件份额是实质性的；
- 其反向路径有应用拥有的祖先；
- Top-down Trace 证据将相同操作置于有限的关键阶段/线程中。

按 Self 事件份额（或当事件没有权重时的 Self 样本份额）对内部候选排名。不要使用 Inclusive 权重进行 Bottom-up 排名。当这些条件不满足时，将数据保留在内部，不在报告中提出 Bottom-up 声明。

## 置信度和结论

使用 Perf 作为统计证据，而非精确持续时间：

- 高置信度需要足够的限定范围样本、可用的栈、兼容的采集范围，以及将热点置于关键路径上的 Trace 证据。
- 当符号缺失、采样覆盖时间短、样本稀疏或事件语义不清晰时，置信度较低。
- 不要仅凭相关性就声称因果关系。
- 不要将样本缺失解释为空闲时间的证明。
- 将实质性的采集限制保留在顶层草稿限制中；确定性注入也将其保留在最终的 `analysis.perf` 中。