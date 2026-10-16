# AGENTS.md

DitingAgent 是面向性能工程师的任务型 Trace 分析 Agent：接收 `.htrace` 与场景描述，
在内部完成 Trace 转换、确定性数据提取、Agent 工具调用、证据记录与报告生成。
`diting-agent` 是主 CLI，`trace-agent` 是兼容别名。

## 环境与常用命令

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。Windows PowerShell 示例：

```powershell
uv sync --extra claude                 # 主 SDK（claude-agent-sdk）
uv sync --extra claude --extra qoder --extra openai --group dev   # 全部适配器 + 测试依赖
uv run pytest                          # 全量测试
uv run pytest tests/test_sdk_pluggability.py tests/test_contract.py -q   # 关键契约
uv run pytest tests/webui -m webui     # 本地 Web 面板的 Selenium 评测（需要 Chrome）
```

运行入口：

```powershell
.\.venv\Scripts\diting-agent.exe analyze <trace.htrace> --type <cold-start|response-latency|completion-latency|frame-jank> `
  --scenario "<业务场景>" --symptom "<现象与目标>" --problem-duration-ms 2000 `
  --agent <claude|qoder|openai|local> --provider deepseek --output ".\results\<case>"
.\.venv\Scripts\diting-agent.exe serve --host 127.0.0.1 --port 8790   # 浏览器交互入口
.\.venv\Scripts\diting-agent.exe eval run --root evals/cases --agent local --trials 1
.\.venv\Scripts\diting-agent.exe configure --provider deepseek        # 写入被 Git 忽略的 .env
.\.venv\Scripts\diting-agent.exe memory stats                         # 记忆库盘点
```

`--agent local` 是无模型依赖的确定性 Agent，用于端到端冒烟；没有凭证时优先用它验证链路。
模型凭证来自 `.env`（见 `.env.example`），不要提交真实 Key。

## 代码结构

```text
src/trace_agent/
  cli.py                 Typer 入口（analyze / configure / serve / eval / memory）
  application/           任务级编排：preflight、归一化、checkpoint
  agent/                 AnalysisAgent 协议 + 适配器（claude / qoder / openai / local）
    core/                与 SDK 无关的 prompt、解析、修复循环、审计、诊断
    adapters/<sdk>/      client / session / tools / skills 桥接
  tools/                 只读 Trace 工具的定义与 run 级预算注册表
  database/              Trace DB 查询（冷启动、时延、帧率、Perf、线程执行）
  skills/                SkillCatalog 与 SkillRouter（方法论路由）
  evidence/              追加式证据存储与统一证据索引
  memory/                候选/生效/已验证/归档四档信任、混合检索、Dreaming 治理
  report/                ReportProjection 与自包含 HTML 渲染
  validation/, verification/, policies/   结构化 Draft 校验与提交策略
.qoder/skills/           分析方法论的单一源（六个 Skill，含 references 与 CN 版本）
vendor/trace_streamer/   内置 Trace Streamer 二进制（darwin / linux / windows）
evals/, tests/, scripts/ 评测语料、契约测试与探针脚本
```

## 必须遵守的边界

- 分析方法论写在 `.qoder/skills/` 里，不要硬编码进 Python system prompt。
  改方法论就改 Skill；`plugins/diting-agent/skills/` 下那六个方法论技能是镜像生成物，
  用 `python scripts/sync_codex_plugin.py` 同步，不要手改；同目录的
  `diting-agent/`（Codex 入口技能）是手写的，脚本不会覆盖也不会删它。
- 更换或新增 SDK 只允许触碰三处：`agent/adapters/<sdk>/`、`pyproject.toml` 的
  optional extra、`agent/providers.py` 的注册。`tests/test_sdk_pluggability.py`
  会守住这条契约；应用层、校验层、报告层保持 SDK 无关。
- Agent 只能调用本进程注册的只读 Trace 工具，工具调用受 run 级预算与每工具次数限制
  （见 `tools/registry.py`）。不要放开 Shell、写入、网络或原始 Trace 文件路径。
- 结论必须引用工具返回的 Evidence ID，区分 `observed` / `suspected` / `confirmed`，
  采集缺口写进 `limitations`。不要用文件名、文件大小或通用性能常识推断指标。
- 只分析性能：CPU、调度、锁、IPC、同步 I/O、任务队列、渲染。当前阶段不分析内存、
  泄漏、GC 压力或 OOM。
- 确定性大对象（线程状态、CPU 分布、唤醒链、Perf Profile）由应用层从 Evidence 合并进
  最终 `AnalysisResult`；模型只提交语义 Draft，不要让模型重新序列化这些结构。
- 记忆默认落本地 SQLite。eval 语料必须与生产语料隔离（namespace + 独立 memory root），
  不要让评测写入生产信任链。

## 改动后的验证

1. `uv run pytest`（至少跑与被改模块相关的测试文件）。
2. 触及 Skill 或工具白名单时，跑 `tests/test_skill_runtime_wiring.py`、
   `tests/test_qoder_skill_access.py`、`tests/test_tool_registry.py`。
3. 触及报告或投影时，用 `--agent local` 跑一次 `evals/cases` 里的样例并打开
   `results/<case>/report.html` 确认渲染。
4. 触及 Codex 集成物时，跑 `python scripts/sync_codex_plugin.py --check` 与
   `docs/codex-integration.md` 中的插件校验命令。

## Codex 集成

三层：本文件（仓库指令，Codex 自动注入）、`plugins/diting-agent/` + 仓库级
`.agents/plugins/marketplace.json`（可安装的能力插件）、以及尚未实施的 MCP Server /
`--agent codex` 适配器。安装、更新、校验与卸载命令见 `docs/codex-integration.md`。

## 临时产物

`.codex_tmp/`、`.trace-agent/`、`results/`、`reports/`、`.venv/` 均已被 Git 忽略，
可作为评测、冒烟与调试的输出根目录。不要把一次性产物写到仓库根目录或 `src/` 下；
仓库里已有的 `.fresh1/`、`.vi-bt*/`、`tmp-agent*/` 属于历史调试残留，不要当作契约。
