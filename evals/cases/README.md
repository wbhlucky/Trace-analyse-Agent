白屏问题回归 case 集（white_screen_001 ~ 006）

数据来源：桌面上的「白屏问题性能测试数据汇总.xlsx」，均为人工 trace 分析结论。
当前 6 个白屏 case 的 input.trace 已指向目标机器（用户名 w50065629）桌面上的 ftrace 文件。
注意：这些输入文件是 ftrace/systrace 格式，工程内的 trace_streamer 仅支持 bytrace/htrace，
运行前必须先转换为 htrace（或已转换出的 SQLite DB）。转换完成前直接执行 eval run 会在
trace 转换阶段失败，这是预期行为而非 case 配置错误。

## case 清单

| case_id | 产品 | 场景 | input.trace |
| --- | --- | --- | --- |
| white_screen_001 | 京东 | 京东进入商品详情白屏 | C:\Users\w50065629\Desktop\京东_商品详情\in_systrace.ftrace |
| white_screen_002 | 支付宝 | 首页-点击卡包 | C:\Users\w50065629\Desktop\支付宝_点击卡包\in_systrace.ftrace |
| white_screen_003 | 哔哩哔哩 | 我的-点击动态 | C:\Users\w50065629\Desktop\B站_点击动态\in_systrace.ftrace |
| white_screen_004 | 美团众包 | 点击左上角-点击外卖柜 | C:\Users\w50065629\Desktop\美团众包_外卖柜\in_systrace.ftrace |
| white_screen_005 | 美团众包 | 点击左上角-点击邀请有奖 | C:\Users\w50065629\Desktop\美团众包_邀请有奖\in_systrace.ftrace |
| white_screen_006 | 高德地图 | 首页-点击实时公交 | C:\Users\w50065629\Desktop\高德地图_实时公交\in_systrace.ftrace |

## 人工标注问题区间

以下区间来自人工分析（相对各条 trace 起点 t0 的约值），精确 ns 待 trace 回填后确认：

- white_screen_001 京东：转场事件 ABILITY_OR_PAGE_sWITCH 结束后约 3.84s 开始，
  约 4.59s 白屏结束，持续约 750ms。
- white_screen_002 支付宝：转场事件结束后约 3.63s 开始，约 3.95s 白屏结束，
  持续约 320ms。
- white_screen_003 哔哩哔哩：转场事件结束后约 5.49s 开始，约 5.724s 白屏结束，
  持续约 234ms。
- white_screen_004 美团众包（外卖柜）：约 6.403s 开始，约 6.92s 结束，
  持续约 517ms。
- white_screen_005 美团众包（邀请有奖）：转场事件结束后约 3.96s 开始，
  约 4.26s 白屏结束，持续约 300ms。
- white_screen_006 高德地图：约 3.98s 开始，白屏持续约 562ms，约 4.542s 结束。

## 处理约定

- 所有 case 使用 type: frame-jank（白屏本质是点击后首帧/渲染延迟，属于丢帧/卡顿）。
- suite: white-screen，便于按场景过滤。
- gold.root_causes / gold.bottlenecks 已从人工「根因」结论转成 canonical + aliases。
- gold.evidence 先保留人工结论里的框架/符号标识词，拿到 trace 后替换为真实 slice/symbol。
- metrics、uncertainty_(status/topics)、forbidden_conclusions 需要 trace 后才能回填，
  目前留空；留空项在现有 grader 中按满分处理，不影响 case 加载。

## 各 case 原始结论（含解决方案，供 gold 回填与 README 参考）

### white_screen_001 京东

根因：Taro 切换框架导致动效实现/渲染链路变化，部分动效丢失或无法正常执行；
同时页面需同时加载自有框架包，依赖资源与初始化链路增加，复杂度提升，
导致加载及首屏渲染耗时增加。

解决方案：评估框架切换后的动效降级策略；拆分自有框架包，延迟非首屏依赖加载；
对首屏关键资源做预加载优化。备注：测试用例中 1.3s。

### white_screen_002 支付宝

根因：页面转场期间组件更新及布局处理任务较重，主线程承担较多组件构建、布局测量
与页面更新，叠加 Cube 布局及 JS 逻辑执行，首屏渲染关键路径耗时增加。

解决方案：组件构建和布局测量任务拆分到子线程或分帧执行；优化 Cube 布局引擎渲染时序。

### white_screen_003 哔哩哔哩

根因：(1) bilibili 内部渲染 [aku.bili:render] 框架卡顿，长时间 running 但无具体内容；
(2) Compile Shaders Inner 编译耗时较重；(3) loading 时机有待优化。

解决方案：预编译 Shader；优化 loading 时机提前展示骨架屏；排查 aku.bili:render 空转。

### white_screen_004 美团众包（外卖柜）

根因：wdsource:render/a.hmcrowdsource 长时间 running 且无具体运行内容。

解决方案：排查 render 阶段空转；检查是否存在不必要的组件重渲染；优化渲染管线调度。

### white_screen_005 美团众包（邀请有奖）

根因：RN_OH_JS 框架页面/组件创建与更新，主要涉及 TurboModule 异步调用、
ShadowNode 创建/克隆、Load/Layout 事件分发，框架开销较大。

解决方案：优化 RN TurboModule 异步调用链路；减少 ShadowNode 创建/克隆频次；
对非首屏组件做懒加载。

### white_screen_006 高德地图

根因：(1) JavaScriptThread 运行致使高德主线程阻塞；(2) 主线程多个
TextLayoutAlgorithm::MeasureContent 文本测量任务，needReCreateParagraph=1 需重建段落，
文本排版/测量开销较大；(3) JavaScriptThread 运行过久，主线程长时间等待。

解决方案：文本测量结果缓存避免重复计算；JS 任务分片避免阻塞主线程；主线程与 JS 线程隔离。

## 运行前必须完成

1. 将 6 个输入 ftrace 转为工程可消费的 bytrace/htrace；或直接准备转换后的 SQLite DB。
2. 若转换产物保持不同后缀/路径，需同步修改对应 case.yaml 的 input.trace。
3. 在目标机器上执行：
   `python -m trace_agent eval run --root evals/cases --suite white-screen --agent local`
   或按项目实际入口使用 `eval run`（先完成 `uv sync`，并确认本机 vendor 下存在对应平台的
   trace_streamer 二进制）。
4. 转换阶段失败属于前置条件未满足，不是 golden case 加载问题；先用
   `load_case` 验证 6 个 case 均可加载，再排查转换。

## input 字段与生产入口对齐

case.yaml 的 input 与生产 AnalyzeRequest 对齐，除已有的 trace / type / scenario / symptom 外，还支持以下可选字段，直接映射到正常用户命令行输入（见 src/trace_agent/eval/runner.py 的 _to_request）：

- time_range：问题时间窗口，格式 <start><unit>-<end><unit>；
- problem_duration_ms：已知问题持续时间；
- operation_marker / start_marker / end_marker / response_marker / completion_marker：业务/边界 Marker 提示；
- target_process：目标应用或进程；
- refresh_rate_hz：帧率场景刷新率（白屏/丢帧建议填写）；
- device / build：设备与构建版本；
- baseline_trace_path：可选基线 trace，相对路径会按 case 文件所在目录解析。

白屏 case 的人工区间已回填为 input.time_range（约值）与 input.problem_duration_ms，精确 ns 待 trace 回填后更新。
