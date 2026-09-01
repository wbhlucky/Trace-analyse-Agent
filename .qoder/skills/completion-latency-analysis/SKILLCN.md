---
name: completion-latency-analysis
description: 分析从输入到首次有效响应及业务完成的交互完成延迟。当 scenario_type 为 completion-latency 时使用；选择有证据支持的边界，拆分响应和响应后工作，并在有调度和 Perf 证据时诊断关键路径。
---

# 完成延迟分析

完成延迟是 `输入 → 业务完成`。响应延迟是包含的子指标 `输入 → 首次有效反馈`；剩余工作是 `响应 → 完成`。

## 必需的候选发现

在选择边界之前，使用 `inspect_completion_latency_candidates` 证据。

1. 如果 `target_ipid` 未知，使用预加载的调用，传入 `target_ipid=0` 和请求提示。仅当预检证据缺失时才自行调用。从返回的进程候选中选择实际的应用进程；不要选择 RenderService、appspawn 或系统宿主进程。
2. 使用选定的非零应用 `target_ipid` 再次调用。原样传入所有可用的请求提示：`operation_marker`、`start_marker`、`end_marker`、`response_marker`、`completion_marker` 和 `problem_duration_ms`。
3. 第二次调用是最终进程和边界的证据来源。候选发现不做出选择；解释为什么选定的点具有所请求的业务含义。

## 边界优先级

使用最强的适用定义：

1. 用户提供的显式 `time_range`。
2. 由 `operation_marker` 命名的唯一正持续时间应用 Slice。其精确区间为 `[ts, ts + dur]`。当唯一的标记提示是一个唯一正持续时间的 `start_marker` 时，允许相同的行为。
3. 唯一的应用定义起始/完成标记对。
4. 在单操作假设下，最后相关的 TouchEvent、PointEvent 或点击标记是输入候选。对于 TouchUp/释放事件，使用 Slice 结束而非其开始。当用户提供 `problem_duration_ms` 时，`最后有效输入 + 持续时间` 是用户定义的完成指标区间。在验证输入和单操作假设后，它可以设置 `completion_proven=true`；声明该假设并使用比应用 Marker 更低的置信度。
5. 应用帧、映射的呈现帧、RenderService 动画结束、帧静止和最后一帧结果是仅用于发现的候选。在选择之前，根据请求的操作对其进行验证。

帧静止候选（300 ms 内至少 5 个在先帧，后跟 200 ms 的帧间隙）和最后一帧回退方案改编自 Harmony Trace Analyzer v1.0.6。它们本身永远不能证明业务完成。动画结束也必须因果关联到此操作；仅时间上的接近是不够的。

## 三个边界和指标

确定：

- `input_boundary`：完成的用户输入或应用定义的操作开始。
- `response_boundary`：首次有效、用户可见的反馈。技术性的首帧仅在其内容被证明为有效反馈之前是候选。
- `completion_boundary`：一个标记、状态转换或呈现的帧，证明请求的操作已根据此应用完成。

从精确的时间戳计算：

```text
response_latency_ms       = (response - input) / 1e6
post_response_duration_ms = (completion - response) / 1e6
completion_latency_ms     = (completion - input) / 1e6
```

如果无法证明完成，设置 `completion_proven=false`；将 `completion_boundary`、`completion_latency_ms` 和 `post_response_duration_ms` 留空。当可用时，保留已证明的响应边界和响应延迟。不要仅仅为了使结果完整而将发现候选转换为事实。

## 必需的阶段证据

选择边界后，当 CPU 调度数据可用时，调用一次 `inspect_completion_latency_phases`。传入精确的非零应用 `target_ipid` 和选定的时间戳；对于不可用的响应或完成边界传入 `0`。

该工具不选择或证明边界。使用它来：

1. 创建精确的、不重叠的响应和响应后证据窗口。
2. 在考虑百分比之前比较绝对的单线程 Running 时间。
3. 将应用主线程作为流水线上下文，然后考虑运行时间最高的应用工作线程、最强的 Runnable/D 状态线程，以及仅帧映射的 RenderService 线程。
4. 使用返回的 `thread_profiles` 推理关键路径。不要在最终 Agent 草稿中序列化 `critical_threads`；确定性注入在提交后添加它们。仅当阶段工具未投影的相关线程需要时，才进行有针对性的 `inspect_thread_execution` 跟进。不要重复阶段提取，或为已返回的线程调用 `inspect_thread_execution`。
5. 将 `slice_hotspots` 视为包含性重叠证据。永远不要在没有更深层应用操作支持的情况下，将标记为 `interval_wrapper_candidate` 的项目报告为根因。
6. 使用返回的 `recommended_perf_scope.thread_ids` 进行最终的线程范围 `inspect_perf_profile` 调用。不要扩展回进程中的每个线程。
7. 使用 `frames.long_frames` 和 `mapped_presentations` 关联代价高昂的阶段；30 ms 屏幕不是完成边界定义。

## 关键路径分析

1. 当响应和响应后阶段的边界被证明时，创建单独的阶段。保留精确的、不重叠的阶段边界。
2. 仅为每个阶段识别关键的应用/渲染/IPC 线程。使用阶段工具的精确投影；仅对不在这些投影中的相关线程调用 `inspect_thread_execution`。永远不要将整个窗口的配置文件复制到阶段中。
3. 使用通用的 CPU、调度、优先级、睡眠、唤醒链、Slice、帧和 IPC 工具来决定经过的时间是运行工作、可运行延迟、阻塞、I/O、排队还是渲染。
4. 如果存在 Perf 样本，分析完成窗口内相关的关键线程，并将应用级 Top-down 栈与 Bottom-up 热点辅助关联。不要将 appspawn、包+偏移根、加载器或通用运行时根报告为优化目标。

## 输出契约

用解析后的应用进程、边界、三个指标、阶段、业务完成语义、关键路径摘要和证据 ID 填充 `analysis.completion_latency`。当完成被证明时，使 `analysis.problem_interval` 使用相同的输入/完成边界和精确持续时间。结论和建议必须识别因果阶段和应用级工作，而不仅仅是列出观察到的 Slice 或符号。

提交的 Agent 草稿仅包含语义阶段对象。它不得包含 `critical_threads` 或 `perf`；应用层从引用的阶段和 Perf 证据中注入这些确定性结构。