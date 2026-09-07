# DitingAgent

面向性能工程师的任务型 Trace 分析 Agent。它以 **Claude Agent SDK 为主 SDK**，
通过可插拔的 SDK 适配层同时支持 Qoder、OpenAI（示例）和本地确定性 Agent——
更换 SDK 不需要改动业务逻辑。

DitingAgent 接收 Trace 文件、分析场景与问题现象，在内部完成确定性数据提取、
Agent 工具调用、证据记录和报告生成。`diting-agent` 是主 CLI，同时保留
`trace-agent` 兼容命令。CLI 只是入口，核心能力位于可复用的应用层中。

## SDK 可插拔设计

所有 Agent SDK 都实现同一个 `AnalysisAgent` 协议，并在统一的 provider
registry 中登记。运行时按 `--agent` 选择适配器，业务层不感知具体 SDK：

| `--agent` | SDK / 实现 | 说明 |
| --- | --- | --- |
| `claude` | `claude-agent-sdk` | **主 SDK**，内建 Claude Code CLI 与 MCP Server |
| `qoder` | `qoder-agent-sdk` | 并列适配器，BYOK 路由到 DeepSeek |
| `openai` | `openai` | 契约示例，证明适配层可插拔 |
| `local` | 内置确定性 Agent | 无模型依赖，用于端到端冒烟 |

新增一个 SDK 只需：

1. 在 `src/trace_agent/agent/adapters/<sdk>/` 增加 `client/session/tools` 适配；
2. 在 `pyproject.toml` 增加对应的 optional extra；
3. 在 `src/trace_agent/agent/providers.py` 注册 provider。

应用、校验、配置与报告层保持 SDK 无关（见 `tests/test_sdk_pluggability.py`）。

## 快速开始

### 1. 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。在项目根目录执行：

```powershell
git clone https://gitcode.com/diting/DitingAgent.git
Set-Location DitingAgent

# 主 SDK：Claude Agent SDK
uv sync --extra claude

# 或按需一并安装其它 SDK 适配器
uv sync --extra claude --extra qoder --extra openai --group dev
```

这会创建 `.venv` 并安装 DitingAgent 与所选 SDK。Claude Agent SDK 默认随包
捆绑 Claude Code CLI；如本机已安装 `claude` 亦可复用。

### 2. 配置模型凭证

Claude Code 原生遵循 Anthropic 环境变量，因此主 SDK 默认读取
`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` 与 `ANTHROPIC_BASE_URL`。项目
也兼容 DeepSeek 的 Anthropic 风格 BYOK 端点：

```powershell
.\.venv\Scripts\diting-agent.exe configure --provider deepseek
```

命令会隐藏 Key 输入，并写入已被 Git 忽略的 `.env`。它会设置
`TRACE_AGENT_LLM_*` 与 `DEEPSEEK_API_KEY`；运行时由 Claude 适配器把这些值桥
接为 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` 再交给 Claude Code 子进程。

若直接使用 Anthropic / Claude Code 登录，无需调用 `configure`，在 `.env`
或进程环境中提供 `ANTHROPIC_API_KEY` 即可。参考 `.env.example`。

### 3. 分析 Trace

Windows PowerShell 示例（默认主 SDK，省略 `--agent` 时由 CLI 指定）：

```powershell
.\.venv\Scripts\diting-agent.exe analyze `
  "D:\traces\case.htrace" `
  --type completion-latency `
  --scenario "点击进入详情并完成渲染" `
  --symptom "点击后约 2 秒页面才稳定" `
  --problem-duration-ms 2000 `
  --agent claude `
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
  --agent claude \
  --provider deepseek \
  --output ./results/case-001
```

切换到其它 SDK 只改 `--agent`（例如 `--agent qoder`）。分析完成后直接打开
`results/case-001/report.html`。首次转换大 Trace 可能较慢；相同 Trace 再次
运行会命中内容寻址 DB 缓存。

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
| `--agent` | `claude`（默认主 SDK）/ `qoder` / `openai` / `local` |
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
  --agent claude `
  --provider deepseek `
  --output ".\results\scroll-jank"
```

## CLI 命令

```powershell
# 分析单个 Trace
diting-agent analyze <trace> --type <type> ...

# 配置 DeepSeek / Anthropic 兼容的 BYOK 凭证
diting-agent configure --provider deepseek

# 启动本地 Web 面板（浏览器交互版分析入口）
diting-agent serve --host 127.0.0.1 --port 8790

# 运行确定性 Agent 评测
diting-agent eval run --root evals/cases --agent claude --trials 1
diting-agent eval report <run-id>
diting-agent eval compare <run-a-id> <run-b-id>
```

## 当前实现

- Typer CLI，`diting-agent` 主命令与 `trace-agent` 兼容别名；
- Pydantic 输入、证据、结论与 Run 持久化 Schema；
- HTrace Adapter 与项目内置 Trace Streamer 转换；
- 默认启用内容寻址 Trace DB 缓存，支持刷新/禁用；
- 事件驱动运行时：SSE 事件、取消、持久化与心跳；
- Trace 转换后按场景与数据能力动态选择 Skill；
- 按 Trace 能力动态开放只读工具的 Tool Registry；
- 冷启动、响应时延、完成时延、帧率/丢帧四类场景；
- 只读领域工具：Trace 概览、基线对比、问题区间候选、冷启动候选、
  完成时延候选、线程执行、Perf Profile 与受限通用 SQL；
- 提交策略与确定性 Schema 校验，结构化 Draft 与最终 `AnalysisResult`；
- 可选择的 Memory（会话回忆、情节记忆与离线 Dreaming）；
- 自包含 HTML 报告与机器可读 JSON 输出；
- 可插拔 SDK：Claude（主）、Qoder、OpenAI（示例）、Local。

## SDK 适配与巡检约束

Agent 执行只向模型开放本进程注册的只读 Trace 工具，不把原始 Trace 路径交给
模型。`Read` 仅允许访问本次选中的 Skill 目录，以便按需读取 Schema 和领域参考
资料；Shell、写入、网络与其它工程文件能力保持禁用。Agent 生成的 SQL 只能通过
受限的 `query_trace_sql` 对当前 Trace DB 执行。

Claude 主 SDK 使用 `permission_mode="dontAsk"`、`strict_mcp_config=True` 以及
显式 `allowed_tools` / `disallowed_tools` 白黑名单；Qoder 适配器使用同等的
`Read` 只读 hook 与 MCP 工具约束。两个适配器共享无 SDK 依赖的核心层
（`trace_agent/agent/core`）处理 prompt、修复循环、解析与诊断。

### Claude 主 SDK 配置

主 SDK 使用 Claude Code 的 Anthropic 风格接口。配置来源：

- `.env` 中的 `DEEPSEEK_API_KEY`、`TRACE_AGENT_LLM_BASE_URL`、`TRACE_AGENT_LLM_MODEL`
  会被适配器桥接给 Claude Code（BYOK）；
- 或直接使用 `ANTHROPIC_API_KEY`（以及可选的 `ANTHROPIC_BASE_URL`）；
- 或复用本机 Claude Code 登录（`claude` CLI + 其本地凭证）。

### Qoder 与 DeepSeek BYOK 配置

Qoder 是并列适配器。它会复用本机 Qoder CLI 登录；模型调用继续由 DeepSeek
API Key 以 BYOK 方式支付（不影响 Claude 主链路）：

```powershell
.\.venv\Scripts\diting-agent.exe configure --provider deepseek
# 然后分析时使用 --agent qoder --provider deepseek
```

Qoder 的 `resolve_model` 会把每轮模型调用路由到 DeepSeek BYOK。若没有本机
Qoder CLI 登录，可另行设置 `QODER_PERSONAL_ACCESS_TOKEN`；它只用于 Qoder 会话
认证，不替代或覆盖 DeepSeek API Key。

### 常见问题

- `claude-agent-sdk` 缺失：执行 `uv sync --extra claude`。
- Claude Code CLI 不可用：安装 Claude Agent SDK wheel（含捆绑 CLI），或在
  PATH 中提供 `claude`，或设置 `CLAUDE_CLI_EXECUTABLE` 指向二进制。
- Qoder `Not logged in`：执行 `qodercli login`，并以 `qodercli --list-models`
  验证，或配置 `QODER_PERSONAL_ACCESS_TOKEN`。
- Qoder `Failed to generate custom pool`：通常是 BYOK 模型 ID 不在 Qoder 目录；
  项目已兼容旧的 `deepseek-v4-pro[1m]` 与 `deepseek-v4-flash` 配置。
- Trace 转换重复耗时：确认未使用 `--no-trace-cache` 或 `--refresh-trace-cache`。
- 自动化环境：在 Secret 管理中同时提供模型 Key 与可选 SDK Token，不要把它们
  提交到仓库。

## 输出

```text
results/case-001/
├── report.html
├── findings.json
├── evidence.json
├── run.json
├── agent-result.json              # Agent 最终消息、解析来源和修复诊断
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
 ├── AnalysisAgent          分析决策（可插拔 SDK）
 ├── SubmissionPolicy       与模型运行时无关的提交证据约束
 ├── EvidenceStore          追加式证据与审计
 ├── EvidenceIndex          统一的进程、区间、阶段和 Perf 证据查询
 ├── ReportProjection       Analysis/Evidence/DB → Report View
 └── ReportRenderer         Report View → 自包含 HTML
```

`AnalyzeApplication` 只负责任务级编排，不包含 Trace SQL、根因判断规则或报告
模板内容。分析方法位于 Skill 中，而不是硬编码在 Python system prompt 里。

“Trace 概览、场景候选发现、精确阶段统计、线程调度投影、Perf 聚合、结果校验、
报告投影和 HTML 渲染”属于确定性流程。Agent SDK 负责选择存在业务语义分歧的边界、
关联证据、判断根因和生成建议。完成时延场景会在模型启动前预载 Trace 概览和第一轮
候选，减少不必要的 LLM 工具往返；阶段工具只允许调用一次，完整 Evidence 保存在
结果目录，传给模型的是有界紧凑视图。模型最终只提交语义 Draft（边界、阶段判断、
根因、建议和 Evidence ID），不再重新序列化线程状态、CPU 分布、唤醒链和 Perf
Profile；这些确定性大对象由应用层从 Evidence 自动合并进最终 `AnalysisResult`。

## 支持的问题类型

```text
cold-start          应用启动到首帧
response-latency    点击或输入到第一帧有效反馈
completion-latency  点击或输入到动作完全完成
frame-jank          帧率下降、慢帧、丢帧和卡顿
```

CPU、调度、锁、IPC、同步 I/O、任务队列和渲染属于可能的根因维度，不是独立问题
类型。当前阶段不分析内存问题。

## 评测（eval）

项目内置确定性 Agent 评测框架（`diting-agent eval`），按
`task → trial → transcript → outcome → grader → harness → suite` 组织。每个
trial 同时产出三层观察：

- **Outcome**：硬 gates 下的确定性加权分（`diagnosis` / `evidence` / `metrics` / `overall`）；
- **Trajectory**：工具轨迹行为评分（tool errors / 重复调用 / budget / forbidden actions）；
- **Operational**：turns / tool_calls / tool_errors / tokens / ttft / latency。

`MultiTrialAggregator` 输出 `pass@1` / `pass@k` / `pass^k`，彼此不折叠进
correctness。trial 失败按 `success` / `agent_failure` / `infra_failure` /
`timeout` / `invalid` / `eval_failure` 分类，避免环境噪声污染统计。

> **涉密说明**：正式测试集涉及内部 Trace 数据，不能公开上传。因此本仓库只
> 提供一个脱敏的评测 demo（`evals/`），用于展示 case / gold oracle / grader /
> trials 的完整结构和运行方式；正式 case 以同样的目录与 Schema 私有维护。

### 已上传的 eval demo

```text
evals/
├── README.md                 # eval 框架与分层
├── KNOWN_LIMITATIONS.md      # 已知限制
├── agents.md                 # 评测方法说明
├── cases/
│   └── cold_start_001/       # 脱敏冷启动回归演示
│       ├── case.yaml         # 题面 + Gold Oracle
│       ├── grader.json       # 确定性评分权重与硬 gates
│       ├── result.json       # 多 trial 聚合
│       └── trials/           # trial-001/002/003 示例
└── examples/
    └── cold-start-demo/      # 最小可运行示例
```

运行演示评测：

```powershell
# 本地确定性 Agent（无需模型凭证）
.\.venv\Scripts\diting-agent.exe eval run --root evals/cases --agent local

# 使用生产 Agent（示例：Claude 主 SDK）
.\.venv\Scripts\diting-agent.exe eval run --root evals/cases --agent claude --trials 3
```

注意：demo 的 `case.yaml` 中 `input.trace` 指向本机路径，直接运行前请替换为
仓库内提供的脱敏 `evals/fixtures/current.db` 或其它可用 Trace。

## 测试

```bash
uv run pytest
```

关键契约测试位于 `tests/test_sdk_pluggability.py`（确保更换 SDK 只触碰
适配器、额外依赖与注册表）和 `tests/test_contract.py`。
