# SmartPerf AppStartup 语义

仅在解释 `app_startup` 中实际存在的行时使用此参考。它不是备用的 Marker 字典。

## 解析器定义的阶段

项目中绑定的 Trace Streamer `AppStartup` 配置定义了以下六个有序的阶段标签：

| 序号 | `start_name` | 含义 |
| --- | --- | --- |
| 1 | `ProcessTouchEvent` | 输入分发和触摸处理 |
| 2 | `StartUIAbilityBySCB` | 场景面板处理启动请求 |
| 3 | `LoadAbility` | Ability 加载和应用进程准备 |
| 4 | `Application Launching` | 应用启动工作 |
| 5 | `UI Ability Launching` | UIAbility 创建和启动 |
| 6 | `UI Ability OnForeground` | UIAbility 前台转换 |

解析器从配置的起始和结束 Marker 模式创建阶段区间。这些模式可以在不同的进程或线程中执行。

SmartPerf 视图还可以显示 `First Frame - App Phase` 和 `First Frame - Render Phase`。将它们视为相关的帧/渲染证据。除非当前数据库实际包含它们，否则不要假设它们是 `app_startup` 行。

## 字段处理

- 当 `start_name` 由字典支持时，通过 `data_dict` 解析。
- 将 `packed_name` 视为目标应用或包名。根据解析器版本，它可以是直接文本或 `data_dict` ID。
- 将 `start_time` 和 `end_time` 视为 Trace 时间线上的纳秒值。
- 将 `ipid` 视为执行该阶段的进程，而非自动视为目标应用进程。
- 将 `call_id` 和 `tid` 保留为原始标识符，直到其确切关系根据当前解析器模式得到确认。

## 分析规则

1. 优先使用实际行，按解析出的 `packed_name` 过滤，并按 `start_time` 排序。
2. 根据 `trace_range` 验证时间戳，拒绝负区间。
3. 保持跨进程链完整；不要将所有行过滤到选定的应用 `ipid`。
4. 在选择总冷启动边界之前，将阶段边界与生命周期 Slice、进程创建和首帧证据关联起来。
5. 不要盲目地对重叠或重复的阶段区间求和。
6. 如果表格缺失或为空，不要合成这六个标签。发现该 Trace 中实际的生命周期和渲染证据，并在边界仍然模糊时降低置信度。
7. 将此参考中的阶段名称视为版本化的解析器知识。实际行和当前绑定的 Trace Streamer 配置优先。