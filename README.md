# DitingAgent

`DitingAgent` 是一个面向性能工程师的任务型 Trace 分析 Agent。它以
`diting-agent` 作为主 CLI，同时保留 `trace-agent` 兼容命令。

它接收 Trace、分析场景和问题现象，在内部完成确定性数据提取、Agent 工具调用、证据记录和报告生成。CLI 只是第一版入口，核心能力位于可复用的应用层中。

## 快速开始

### 1. 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。在项目根目录执行：

```powershell
git clone https://gitcode.com/diting/DitingAgent.git
Set-Location DitingAgent
uv sync --extra qoder
```

这会创建 `.venv`，安装 DitingAgent、Qoder Agent SDK 和项目运行依赖。

### 2. 登录 Qoder CLI

如果系统已经安装 `qodercli`：

```powershell
qodercli login
qodercli --list-models
```

如果没有全局命令，Windows 可以直接使用 SDK 内置的 CLI：

```powershell
$qoderCli = & .\.venv\Scripts\python.exe -c `
  "from pathlib import Path; import qoder_agent_sdk; print(Path(qoder_agent_sdk.__file__).parent / '_bundled' / 'qodercli.exe')"
& $qoderCli login
& $qoderCli --list-models
```

浏览器授权成功且 `--list-models` 能返回模型后，才算完成 CLI 登录。仅登录
Qoder 网页但未让 CLI 收到授权回调是不够的。

### 3. 配置 DeepSeek BYOK

项目使用 Qoder 负责 Agent 执行，实际模型调用继续使用 DeepSeek API Key：

```powershell
.\.venv\Scripts\diting-agent.exe configure --provider deepseek
```

命令会隐藏 Key 输入，并写入已被 Git 忽略的 `.env`。已有 `.env` 可直接
继续使用，不需要更换 `DEEPSEEK_API_KEY`。旧模型名
`deepseek-v4-pro[1m]` 会在运行时自动映射为 Qoder BYOK 目录中的
`deepseek-v4-pro-pg`。

Qoder 登录与 DeepSeek Key 是两套独立凭证：前者建立 Agent 会话，后者支付
实际模型调用。也可以用 `QODER_PERSONAL_ACCESS_TOKEN` 代替本机 Qoder CLI
登录，但它不能代替 DeepSeek Key。

### 4. 分析 Trace

Windows PowerShell 示例：

```powershell
.\.venv\Scripts\diting-agent.exe analyze `
  "D:\traces\case.htrace" `
  --type completion-latency `
  --scenario "点击进入详情并完成渲染" `
  --symptom "点击后约 2 秒页面才稳定" `
  --problem-duration-ms 2000 `
  --agent qoder `
  --provider deepseek `
  --output ".\results\case-001"
```

Linux/macOS 示例：

```bash
uv run diting-agent analyze /path/to/case.htrace \
  --type completion-latency \
  --scenario "点击进入详情并完成渲染" \
  --symptom "点击后约 2 秒页面才稳定" \
  --problem-duration-ms 2000 \
  --agent qoder \
  --provider deepseek \
  --output ./results/case-001
```

分析完成后直接打开 `results/case-001/report.html`。首次转换大 Trace 可能较慢；
相同 Trace 再次运行会命中内容寻址 DB 缓存。

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--type` | `cold-start`、`response-latency`、`completion-latency` 或 `frame-jank` |
| `--scenario` | 本次操作的业务场景描述 |
| `--symptom` | 用户观察到的性能现象与分析目标 |
| `--time-range` | 已知问题区间，如 `788297.524337656s-788298.373582031s` |
| `--problem-duration-ms` | 只知道持续时间时，与 Trace 最后一次有效输入组合推导区间 |
| `--target-process` | 目标应用进程提示；省略时由工具和 Agent 联合判断 |
| `--start-marker` / `--end-marker` | 应用自定义的唯一 Slice/Marker 边界 |
| `--operation-marker` | 一个正 duration 的业务 Slice，直接定义完整操作区间 |
| `--refresh-rate` | 帧率/卡顿场景的显示刷新率提示 |
| `--baseline` | 可选基线 Trace，用于对比 |
| `--no-progress` | 关闭交互式进度，适合 CI 或日志重定向 |

帧率/丢帧示例：

```powershell
.\.venv\Scripts\diting-agent.exe analyze `
  "D:\traces\scroll-jank.htrace" `
  --type frame-jank `
  --scenario "滑动聊天列表" `
  --symptom "连续两次滑动明显卡顿，需要计算问题区间 FPS 并定位丢帧原因" `
  --time-range "2441.036097969s-2446.036097969s" `
  --refresh-rate 60 `
  --agent qoder `
  --provider deepseek `
  --output ".\results\scroll-jank"
```

## 当前状态

当前仓库提供可运行的最小框架：

- Typer CLI；
- Pydantic 输入、证据和结论 Schema；
- HTrace Adapter 和项目内置 Trace Streamer 转换；
- 默认启用的内容寻址 Trace DB 缓存；
- CLI 生命周期、当前 Agent 工具与实际耗时进度；
- 冷启动、响应时延、完成时延、帧率/丢帧四类场景；
- Trace 转换后按场景和数据能力选择 Skill；
- 按 Trace 能力动态开放工具的 Tool Registry；
- 只读领域能力：Trace 概览、基线元数据对比、通用问题区间候选、
  冷启动候选、完成时延候选、线程执行、Perf Profile 与受限通用 SQL；
- 项目级总控、场景和通用 Perf Skill 及其版本指纹；
- 本地确定性 Agent，用于在无 API Key 时验证完整链路；
- Qoder Agent SDK 适配器；
- 运行时工具硬预算、最终结构化输出自动修复和 `agent-result.json`
  SDK 诊断；
- JSONL 工具审计日志；
- `findings.json`、`evidence.json`、`run.json` 和 `report.html`；
- 端到端测试。

HTrace 现在会先通过工程内置的 Trace Streamer 转换成 SQLite DB，并
根据实际表结构声明进程、线程、Slice、调度、I/O、帧和 Marker 等
Trace 能力。`perf-samples` 只有在 `perf_sample` 存在真实数据时才会
声明；Qoder Agent 随后自动追加通用 `perf-sample-analysis` Skill。
`app-startup-stages` 同样要求 `app_startup` 存在真实行，并作为冷启动
阶段和边界的辅助证据。
当 `perf-samples` 可用时，还会开放确定性的 `inspect_perf_profile`：它在
已确定的问题窗口和相关 OS PID/TID 范围内返回采集配置、样本/事件权重、
线程与 CPU 分布、符号化率和热点调用栈。该工具使用独立保留预算，不会被
普通时间线或 SQL 取证耗尽；验证层会拒绝只有 `sample_count=0` 的 Perf
占位结果。
事件级领域查询工具仍在建设中，因此本地 Agent 不会猜测性能根因。

## 前端面板

项目附带一个零依赖的本地 Web 面板，用于浏览 `results/` 下的分析用例，查看概览、发现项、证据、运行信息与原始数据，并可一键打开每个用例已有的完整 `report.html`。

```powershell
.\.venv\Scripts\diting-agent.exe serve
```

默认监听 `http://127.0.0.1:8080` 并自动打开浏览器。常用参数：

| 参数 | 含义 |
| --- | --- |
| `--host` | 监听地址，默认 `127.0.0.1` |
| `--port` / `-p` | 监听端口，默认 `8080`，被占用时自动选择可用端口 |
| `--results-dir` | 分析结果目录，默认 `results/` |
| `--no-open` | 启动后不自动打开浏览器 |

面板为本地只读工具，不会修改分析结果，也不需要额外的 Python 依赖。

## 依赖安装

仅使用无网络的本地确定性 Agent：

```bash
uv sync
```

执行完整的 Qoder Agent 分析使用 `uv sync --extra qoder`。本地 Agent 主要用于
验证 Trace 转换、确定性工具和报告链路，不会猜测性能根因。

## 问题窗口与场景参数

先准备一个 Trace 文件，然后执行：

```bash
uv run diting-agent analyze app-start.htrace \
  --type cold-start \
  --scenario "应用冷启动" \
  --symptom "启动耗时从1.2秒增加到2.1秒" \
  --agent qoder \
  --provider deepseek \
  --output ./results/case-001
```

当应用提供唯一的自定义 Slice 时，可以显式定义问题区间：

```bash
uv run diting-agent analyze app-start.htrace \
  --type cold-start \
  --scenario "进入首页并稳定" \
  --symptom "首页稳定耗时偏长" \
  --start-marker "APP_START_BEGIN" \
  --end-marker "HOME_PAGE_STABLE" \
  --agent qoder \
  --provider deepseek \
  --output ./results/case-001
```

若未提供明确边界，但已知问题持续时间，通用窗口工具会默认假设本
Trace 只包含一次用户操作，以最后一个有效 TouchEvent、PointerEvent
或点击打点作为起点：

```bash
uv run diting-agent analyze interaction.htrace \
  --type response-latency \
  --scenario "点击进入详情" \
  --symptom "响应约持续1800ms" \
  --problem-duration-ms 1800 \
  --agent qoder \
  --provider deepseek \
  --output ./results/case-002
```

区间解析优先级为：显式 `--time-range`、唯一应用 Slice 对、场景语义
边界、最后输入打点加问题持续时间。输入兜底是可审计假设，不会覆盖
应用对冷启动结束的定义；例如某应用可以把进入首页后的稳定帧定义为
冷启动结束，而不是 Trace 中最早出现的应用帧。

完成时延场景支持应用以一个唯一、正 duration 的业务 Slice 定义完整
操作区间：

```bash
uv run diting-agent analyze interaction.htrace \
  --type completion-latency \
  --scenario "点击进入详情并完成渲染" \
  --symptom "点击后约 2 秒页面才稳定" \
  --operation-marker "OPEN_DETAIL_OPERATION" \
  --response-marker "DETAIL_FIRST_FEEDBACK" \
  --output ./results/completion-001 \
  --agent qoder \
  --provider deepseek
```

其中 `--operation-marker` 对应 Slice 的 `[ts, ts+dur]`。Agent 仍会分别
给出输入到有效反馈、反馈后到业务完成、输入到业务完成三段指标。没有
应用完成语义时，动画结束、帧静止和窗口最后一帧只作为发现候选，不能
单独把 `completion_proven` 判为真。

每次任务都会先执行：

```text
app-start.htrace
→ vendor/trace_streamer/<platform>/trace_streamer
→ .trace-agent/cache/trace-db              # 相同内容时直接命中
→ results/case-001/work/<run-id>/current.db
→ 只读 Schema 检查
→ 注册 TraceCapabilities
→ 按能力选择通用 Skill
→ 确定性预分析
→ Agent 分析
```

默认缓存键包含 Trace 全量 SHA-256、TraceStreamer 二进制 SHA-256 和转换
格式版本。命中后优先使用硬链接生成本次 run 的 `current.db`，因此相同
Trace 重跑不会再次执行耗时的 TraceStreamer 转换。可按需控制：

```powershell
# 临时关闭缓存
.\.venv\Scripts\diting-agent.exe analyze ... --no-trace-cache

# 强制重新转换并刷新缓存
.\.venv\Scripts\diting-agent.exe analyze ... --refresh-trace-cache

# 指定共享缓存目录
.\.venv\Scripts\diting-agent.exe analyze ... `
  --trace-cache-dir D:\trace-agent-cache
```

CLI 默认显示 8 个阶段以及 Agent 当前正在调用的取证工具，不显示
TraceStreamer 无法可靠提供的伪百分比，而显示 spinner 和真实耗时。脚本
或 CI 中可使用 `--no-progress` 关闭。

项目内置目标：

```text
windows-x86_64
linux-x86_64
darwin-aarch64
```

默认不会扫描系统其他目录，也不依赖 PATH。开发调试或未内置的平台
可以显式覆盖：

```bash
uv run diting-agent analyze app-start.htrace \
  --type cold-start \
  --scenario "应用冷启动" \
  --symptom "启动耗时增加" \
  --trace-streamer /path/to/trace_streamer \
  --trace-streamer-timeout 900 \
  --output ./results/case-001
```

默认使用不访问网络的 `local` Agent。启用 Qoder：

```bash
uv run diting-agent analyze app-start.htrace \
  --type cold-start \
  --scenario "应用冷启动" \
  --symptom "启动耗时从1.2秒增加到2.1秒" \
  --output ./results/case-001 \
  --agent qoder
```

Qoder 模式支持复用本机 Qoder CLI 登录，也支持 Personal Access Token；
实际模型仍通过已有的 DeepSeek API Key 以 Qoder BYOK 方式调用。
Trace 转换完成后会显式装载
总控 `trace-analysis`、与 `--type` 匹配的一个场景 Skill；存在有效
Perf 样本时还会追加 `perf-sample-analysis`。由这些 Skill 共同规定
分析方法和证据标准。
该模式只向模型开放本进程注册的只读 Trace 工具，不把原始 Trace
路径交给模型。`Read` 仅允许访问本次选中的 Skill 目录，以便按需读取
Schema 和领域参考资料；其他工程文件、Shell、写入和网络能力保持禁用。
Agent 生成的 SQL 只能通过受限的 `query_trace_sql` 对当前 Trace DB
执行。

### Qoder 与 DeepSeek BYOK 配置

Qoder SDK 会复用本机 Qoder CLI 登录；原有 DeepSeek API Key 和模型配置
保持在当前工作目录下、已被 Git 忽略的 `.env`：

```powershell
.\.venv\Scripts\diting-agent.exe configure --provider deepseek
```

命令会隐藏 DeepSeek API Key 输入。之后执行分析时使用
`--agent qoder --provider deepseek`；Qoder 的 `resolve_model` 会把每轮
模型调用路由到 DeepSeek BYOK，不消耗 Qoder 模型额度。模型仍可由 `.env`
中的 `TRACE_AGENT_LLM_MODEL` 或命令行 `--model` 覆盖。
如果没有本机 Qoder CLI 登录，可另行设置 `QODER_PERSONAL_ACCESS_TOKEN`；
它只用于 Qoder 会话认证，不替代或覆盖 DeepSeek API Key。

### 常见问题

- `Not logged in`：执行 `qodercli login`，并用 `qodercli --list-models` 验证；
  SDK 内置 CLI 的调用方式见“快速开始”。
- `Failed to generate custom pool`：通常是 BYOK 模型 ID 不在 Qoder 当前目录；
  项目已兼容旧的 `deepseek-v4-pro[1m]` 和 `deepseek-v4-flash` 配置。
- Trace 转换重复耗时：确认未使用 `--no-trace-cache` 或
  `--refresh-trace-cache`。
- 自动化环境：在 Secret 管理中同时提供 `QODER_PERSONAL_ACCESS_TOKEN` 和
  `DEEPSEEK_API_KEY`，不要把它们提交到仓库。

## 输出

```text
results/case-001/
├── report.html
├── findings.json
├── evidence.json
├── run.json
├── agent-result.json              # Qoder 最终消息、解析来源和修复诊断
├── agent-log.jsonl
└── work/
    └── run-xxxxxxxxxxxx/
        ├── current.db
        ├── baseline.db                  # 提供基线时生成
        ├── current-trace-streamer.stdout.log
        └── current-trace-streamer.stderr.log
```

## 架构边界

```text
CLI
 ↓
AnalyzeApplication          任务生命周期
 ├── TraceAdapter           Trace 校验与 DB 转换
 ├── TraceCapabilities      DB 中实际可用的数据能力
 ├── ScenarioCatalog        场景 Skill、预分析和展示元数据
 ├── ToolRegistry           动态注册受约束的只读查询
 ├── AgentFactory           能力确定后创建 Agent
 ├── SkillRouter            总控、场景与条件方法论路由
 ├── AnalysisAgent          分析决策
 ├── SubmissionPolicy       与模型运行时无关的提交证据约束
 ├── EvidenceStore          追加式证据与审计
 ├── EvidenceIndex          统一的进程、区间、阶段和 Perf 证据查询
 ├── ReportProjection       Analysis/Evidence/DB → Report View
 └── ReportRenderer         Report View → 自包含 HTML
```

`AnalyzeApplication` 只负责任务级编排，不包含 Trace SQL、根因判断规则或报告模板内容。
分析方法位于 Skill 中，而不是硬编码在 Python system prompt 中。

其中“Trace 概览、场景候选发现、精确阶段统计、线程调度投影、Perf 聚合、
结果校验、报告投影和 HTML 渲染”属于确定性流程。Qoder Agent 负责选择存在业务语义
分歧的边界、关联证据、判断根因和生成建议。完成时延场景会在模型启动前
预载 Trace 概览和第一轮候选，减少不必要的 LLM 工具往返；阶段工具只允
许调用一次，完整 Evidence 保存在结果目录，传给模型的是有界紧凑视图。
模型最终只提交语义 Draft（边界、阶段判断、根因、建议和 Evidence ID），
不再重新序列化线程状态、CPU 分布、唤醒链和 Perf Profile；这些确定性
大对象由应用层从 Evidence 自动合并进最终 `AnalysisResult`。

## 支持的问题类型

```text
cold-start          应用启动到首帧
response-latency    点击或输入到第一帧有效反馈
completion-latency  点击或输入到动作完全完成
frame-jank          帧率下降、慢帧、丢帧和卡顿
```

CPU、调度、锁、IPC、同步 I/O、任务队列和渲染属于可能的根因维度，
不是独立问题类型。当前阶段不分析内存问题。

## 测试

```bash
uv run pytest
```
