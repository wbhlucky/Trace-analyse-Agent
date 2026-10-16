# Codex 平台集成

本文记录 DitingAgent 接入 Codex（CLI / IDE 扩展 / 桌面 App 共用同一套配置）的方式。
验证环境：`codex-cli 0.142.3`，Windows。

## 三层结构

| 层 | 载体 | 生效范围 | 需要安装 |
| --- | --- | --- | --- |
| 仓库指令 | `AGENTS.md` | 只在本仓库的 Codex 会话 | 否，自动注入 |
| 能力插件 | `plugins/diting-agent/` + `.agents/plugins/marketplace.json` | 安装后所有会话 | 是 |
| 深度集成 | MCP Server 或 `--agent codex` 适配器 | 见文末 | 未实施 |

`AGENTS.md` 会被 Codex 直接拼进模型可见上下文，适合放命令、目录地图和硬约束；
插件负责把分析方法论带到本仓库之外的会话里。两者都不需要改业务代码。

## 目录职责

```text
AGENTS.md                                  仓库级 Codex 指令（手写）
.agents/plugins/marketplace.json           仓库级 marketplace，name = diting-agent-local
plugins/diting-agent/
  .codex-plugin/plugin.json                插件清单：name / version / interface / skills
  skills/diting-agent/                     Codex 专属入口技能（手写）：驱动 CLI、读结果
  skills/trace-analysis/                   以下六个方法论技能由脚本镜像，勿手改
  skills/cold-start-analysis/
  skills/response-latency-analysis/
  skills/completion-latency-analysis/
  skills/frame-jank-analysis/
  skills/perf-sample-analysis/
scripts/sync_codex_plugin.py               .qoder/skills -> 插件 skills 的同步器
```

方法论的单一源仍是 `.qoder/skills/`（Qoder 原生读取，Claude 通过把 `.qoder` 声明为
本地插件读取）。Codex 安装插件时会把整个目录复制进
`~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`，软链在 Windows 上不可靠，
因此这里选择"脚本镜像 + `--check` 守漂移"。`SKILLCN.md` 与 `*CN.md` 是给人看的译本，
不进插件包。

## 安装（一次性）

```powershell
codex plugin marketplace add D:\path\to\DitingAgent      # 仓库根目录，不是 marketplace.json
codex plugin add diting-agent@diting-agent-local
codex plugin list | Select-String diting
```

安装后需要**新开一个线程**，Codex 才会加载新的技能与工具。

## 更新

```powershell
python scripts/sync_codex_plugin.py                       # .qoder/skills 改动后同步镜像
codex plugin add diting-agent@diting-agent-local          # 重新复制插件到缓存
```

本地 marketplace 的 re-add 会重新拷贝源目录，实测无需改 `version` 即可刷新缓存；
只有当 Codex 侧出现按版本去重的行为时，才需要按
`<version>+codex.<时间戳>` 的 cachebuster 规则抬版本号。

## 校验

```powershell
python scripts/sync_codex_plugin.py --check               # 镜像是否与 .qoder/skills 一致（CI 用）
.\.venv\Scripts\python.exe "$env:USERPROFILE\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" plugins/diting-agent
codex debug prompt-input "probe" | Select-String 'trace-analysis'   # 技能是否进入模型上下文
```

`validate_plugin.py` 随 Codex 的 `plugin-creator` 技能发布，需要 `pyyaml`；它检查
`plugin.json` 字段白名单、semver、`interface` 必填项、资源路径以及每个技能的
frontmatter 与 `agents/openai.yaml`。`codex debug prompt-input` 输出模型可见的
prompt，用来确认技能清单里出现了本插件的七个技能。

## 卸载

```powershell
codex plugin remove diting-agent
codex plugin marketplace remove diting-agent-local
```

## 不想把插件放进仓库时

改用个人 marketplace：把插件放到 `~/plugins/diting-agent/`，条目写进
`~/.agents/plugins/marketplace.json`（`source.path` 仍为 `./plugins/diting-agent`）。
该文件被 Codex 隐式发现，不需要 `codex plugin marketplace add`。仓库级 marketplace
的好处是随代码一起评审和分发，团队克隆后即可安装，因此这里默认用仓库级。

## 已知边界与后续可选

- 方法论技能里出现的 `get_trace_overview`、`query_trace_sql`、
  `inspect_problem_window_candidates` 等是 DitingAgent **进程内**注册的只读工具，
  Codex 会话无法直接调用。插件里的 `diting-agent` 技能已显式声明这一点：
  在 Codex 中执行分析走 CLI，方法论技能用于理解、复核与写好 `--scenario` / `--symptom`。
- 若要让 Codex 交互式查询 Trace DB，需要新增一个 stdio MCP Server 暴露这些只读工具
  （沿用 `tools/registry.py` 的预算与能力门控），并在 `plugin.json` 里加
  `"mcpServers": "./.mcp.json"`；插件校验器已支持该字段。
- 反向集成（把 Codex 当作 `AnalysisAgent` 后端，即 `--agent codex`）按 README 的三步走：
  新增 `src/trace_agent/agent/adapters/codex/`、在 `pyproject.toml` 加 extra、
  在 `agent/providers.py` 注册；传输可用 `codex mcp-server`（stdio）或 `codex exec`。
  这条路由 `tests/test_sdk_pluggability.py` 守契约。
