# DitingAgent 保姆级教程（从零到会）

> 面向完全没有接触过本项目的初学者。本教程基于 Windows + PowerShell 编写，
> Linux/macOS 只需把 `.venv\Scripts\diting-agent.exe` 换成 `uv run diting-agent`，
> 把 PowerShell 换行符 `` ` `` 换成 `\` 即可。

---

## 0. 这个项目是干什么的？（两分钟看懂）

### 0.1 背景概念：Trace 是什么？

手机 App、车机系统在运行时，系统会记录一份**逐毫秒级的执行流水账**，包含：

- 哪个进程、哪个线程在什么时间执行了什么函数（Slice）
- 线程的调度（什么时候被唤醒、什么时候被抢占）
- 动画帧的提交情况（哪一帧耗时过长、丢帧）
- 事件输入（点击、滑动）打点、系统性能采样（Perf 调用栈）等

这份流水账就是 **Trace**。本项目的 Trace 文件格式是 **`.htrace`**（华为的 Trace 格式）。

**性能问题**（启动慢、点击没反应、卡顿）都能在 Trace 里找到对应的「证据」，
但问题是：Trace 文件动辄几十 MB、上百万条记录，人肉翻非常痛苦。

### 0.2 DitingAgent 是什么？

> 一个面向性能工程师的**任务型 Trace 分析 Agent**。

它接收三样东西，吐出一份带证据链的性能分析报告：

| 输入 | 含义 | 示例 |
| --- | --- | --- |
| Trace 文件 | 待分析的性能数据 | `case.htrace` |
| 场景（scenario） | 做了什么操作 | "点击进入详情并完成渲染" |
| 现象（symptom） | 观察到什么问题 | "点击后约 2 秒页面才稳定" |

### 0.3 它内部是怎么工作的？（一句话版）

```text
HTrace 文件
   ↓ ① 转成 SQLite 数据库（内置 Trace Streamer，有缓存）
   ↓ ② 探测数据库里有哪些数据能力（进程/线程/帧/Perf……）
   ↓ ③ 按问题类型加载对应的分析 Skill（方法论 + 证据标准）
   ↓ ④ 确定性工具先提取事实（时间线、候选区间、线程执行……）
   ↓ ⑤ Agent（大模型）结合场景做语义判断、根因推理
   ↓ ⑥ 生成 findings.json / evidence.json / report.html
```

**关键设计**：凡是能确定性计算的（统计、投影、聚合）绝不让大模型猜；
大模型只负责「有业务语义分歧」的部分（边界选择、根因判断、建议）。
所以结论有据可查，每条结论都能追溯到证据 ID。

---

## 1. 你需要准备什么

### 1.1 硬性要求（缺一不可）

| 项目 | 要求 | 你的电脑 |
| --- | --- | --- |
| 操作系统 | Windows / Linux / macOS | Windows 11 ✓ |
| Python | **3.12+** | 3.12.7 ✓ |
| uv | Python 包管理器（必装，见 3.1 节） | ❌ 还没装 |
| 一个 .htrace 文件 | 要分析的数据 | 需要自己准备 |

### 1.2 可选要求（做完整 AI 分析才需要）

- **DeepSeek API Key**（模型调用费用从这里出）
- **Qoder 账号**（`qodercli login`，用于建立 Agent 会话）

> 💡 没有 Key 也能跑！项目内置一个 **local 确定性 Agent**，
> 专门用于验证转换、工具和报告链路——先把整条流水线跑通，再上 AI。

### 1.3 特别提醒：你的项目路径是双层文件夹

你解压后得到的路径是：

```text
d:\Users\Administrator\Desktop\DitingAgent-main\        ← 外层（只是一个壳）
└── DitingAgent-main\                                    ← 真正的项目根目录！
    ├── src\
    ├── pyproject.toml
    └── ...
```

**所有命令都要在「真正的项目根目录」里执行。** 教程里记作 `项目根目录`。

---

## 2. 项目目录速览（先混个脸熟）

```text
DitingAgent-main/
├── pyproject.toml           # 项目定义：依赖、CLI 入口、打包配置
├── uv.lock                  # 依赖锁文件（uv 自动维护，别手动改）
├── .env.example             # 环境变量模板（复制为 .env 后填 Key）
├── README.md                # 官方文档（本教程的权威版本）
├── docs/architecture.md     # 架构设计文档
├── src/trace_agent/         # 全部核心源码（Python）
│   ├── cli.py               # CLI 入口（analyze / configure 命令）
│   ├── application/         # 任务编排层（AnalyzeApplication 等）
│   ├── agent/               # Agent 实现：local（本地）/ qoder（大模型）
│   ├── database/            # 确定性查询：窗口、线程、帧、Perf……
│   ├── evidence/            # 证据存储与索引（追加式、可审计）
│   ├── report/              # 报告投影 + HTML 渲染
│   ├── scenarios/           # 四类场景的目录定义
│   ├── skills/              # Skill 目录与路由
│   ├── tools/               # 只读 Trace 工具注册表
│   ├── trace/               # Trace 适配器、转换、缓存
│   └── models.py            # Pydantic 数据模型（输入/证据/结论）
├── .qoder/skills/           # Agent 的分析方法论（Skill 文件）
│   ├── trace-analysis/      # 总控 Skill
│   ├── cold-start-analysis/ # 冷启动场景 Skill
│   ├── response-latency-analysis/
│   ├── completion-latency-analysis/
│   ├── frame-jank-analysis/
│   └── perf-sample-analysis/ # Perf 采样分析 Skill
├── vendor/trace_streamer/   # 内置的 Trace 转换二进制（三平台）
├── tests/                   # pytest 测试（28 个文件）
└── evals/                   # 评估说明
```

---

## 3. 环境搭建（一步步来）

### 3.1 安装 uv

uv 是新一代 Python 包管理器（Astral 出品），项目用它创建虚拟环境、装依赖。
你有两种安装方式，任选其一：

**方式 A：用 pip 装（最简单，因为你有 Python 3.12）**

```powershell
python -m pip install uv
```

**方式 B：官方独立安装器**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

装完验证（需要**新开一个终端**让 PATH 生效）：

```powershell
uv --version
```

### 3.2 进入项目根目录并安装依赖

```powershell
# 注意：要进到"里面那层"DitingAgent-main
Set-Location "d:\Users\Administrator\Desktop\DitingAgent-main\DitingAgent-main"

# 只装基础依赖（本地 Agent 模式够用，不联网、不要 Key）
uv sync
```

如果之后要用 Qoder + DeepSeek 做完整 AI 分析，改成：

```powershell
uv sync --extra qoder
```

> 第一次会下载依赖并创建 `.venv` 虚拟环境，稍等几分钟属正常。
> 装完可以二选一执行一次：只跑本地链路就 `uv sync`，
> 后面决定上 AI 了再补 `uv sync --extra qoder` 也可以，不会重复装基础包。

### 3.3 验证安装

```powershell
.\.venv\Scripts\diting-agent.exe --version
.\.venv\Scripts\diting-agent.exe --help
```

能打印出版本号和一屏帮助信息，说明环境 OK。

> 小技巧：之后每次执行都写 `.\.venv\Scripts\diting-agent.exe` 太累，
> 可以在 PowerShell 里先 `.\\.venv\\Scripts\\diting-agent.exe` 建个别名：
> `Set-Alias diting .\.venv\Scripts\diting-agent.exe`（仅当前窗口有效），
> 或直接用 Linux 风格命令 `uv run diting-agent ...`（自动复用 .venv，推荐）。

---

## 4. 配置凭证（两种模式）

### 4.1 模式 A：本地模式（零配置，先跑通再说）

什么都不用配。CLI 的 `--agent` 参数**默认就是 local**，
不访问网络、不需要任何 Key。适合：

- 验证工具链是否正常
- 看确定性分析能产出什么
- 没有 API Key 时先熟悉流程

⚠️ 注意：本地 Agent 不会猜性能根因（README 明说：
「本地 Agent 不会猜测性能根因」），它主要是验证链路、生成确定性的候选证据。

### 4.2 模式 B：Qoder + DeepSeek（完整 AI 分析）

这套体系有两套独立凭证，**缺一不可，且职责不同**：

| 凭证 | 用途 | 获取方式 |
| --- | --- | --- |
| Qoder 登录 | 建立 Agent 会话 | `qodercli login`（浏览器授权） |
| DeepSeek API Key | 支付实际模型调用（BYOK） | [platform.deepseek.com](https://platform.deepseek.com) 申请 |

**第一步：登录 Qoder CLI**

```powershell
qodercli login
qodercli --list-models
```

如果你没全局装 qodercli（很可能没装），可以用项目 SDK 自带的 CLI：

```powershell
$qoderCli = & .\.venv\Scripts\python.exe -c `
  "from pathlib import Path; import qoder_agent_sdk; print(Path(qoder_agent_sdk.__file__).parent / '_bundled' / 'qodercli.exe')"
& $qoderCli login
& $qoderCli --list-models
```

> ⚠️ 判断登录成功的标准：浏览器授权成功**并且** `--list-models` 能返回模型列表。
> 只打开网页不算数。

**第二步：配置 DeepSeek Key**

```powershell
.\.venv\Scripts\diting-agent.exe configure --provider deepseek
```

按提示粘贴 Key（输入是隐藏的）。命令会把它写进项目根目录的 `.env` 文件
（已被 Git 忽略，不会误提交）。

`.env` 内容大概是（可对照 `.env.example`）：

```env
TRACE_AGENT_LLM_PROVIDER=deepseek
TRACE_AGENT_LLM_MODEL=deepseek-v4-pro-pg
TRACE_AGENT_LLM_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_API_KEY=sk-你的密钥
QODER_PERSONAL_ACCESS_TOKEN=        # 可选：没有本机 qodercli 登录时用
```

> 想换模型？改 `.env` 里的 `TRACE_AGENT_LLM_MODEL`，或用 `--model` 参数临时覆盖。

---

## 5. 你的第一次分析（本地模式）

### 5.1 准备一个 Trace 文件

`.htrace` 文件需要你自己准备，来源通常是：

- 公司/团队的性能测试平台下载
- 手机系统抓取（如华为设备的 htrace 抓取工具）
- 已有的性能分析案例

随便放一个路径即可，例如 `D:\traces\case.htrace`。
（本仓库不附带示例 Trace 文件。）

### 5.2 跑一次冷启动分析（本地 Agent）

```powershell
# 在项目根目录执行
.\.venv\Scripts\diting-agent.exe analyze `
  "D:\traces\case.htrace" `
  --type cold-start `
  --scenario "应用冷启动" `
  --symptom "启动耗时从1.2秒增加到2.1秒" `
  --output ".\results\case-001"
```

（`--agent` 缺省就是 local，所以这次不写。）

你会看到 8 个阶段依次推进 + spinner + 每阶段的真实耗时：

```text
Trace 转换 → 能力探测 → Skill 装载 → 确定性预分析 → Agent 分析 → 证据固化 → 报告渲染
```

完成后提示：

```text
分析完成
Run ID: run-xxxxxxxxxxxx
报告: results\case-001\report.html
```

用浏览器打开 `results\case-001\report.html`，这就是第一份产出！

### 5.3 跑一次 Qoder 完整分析

先确保按 4.2 节配好了 Qoder 登录和 DeepSeek Key：

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

此时进度条会显示 Agent 正在调用哪个取证工具（如
`get_trace_overview`、`inspect_problem_window_candidates`、`query_trace_sql`）。

---

## 6. analyze 命令参数详解（超全对照表）

**必填参数**（4 个）：

| 参数 | 含义 | 示例 |
| --- | --- | --- |
| `trace_path`（位置参数） | 待分析的 Trace 文件 | `"D:\traces\case.htrace"` |
| `--type` | 问题类型，四选一 | `cold-start` |
| `--scenario` | 业务场景描述 | `"点击进入详情"` |
| `--symptom` | 观察到的性能现象 + 分析目标 | `"点击后约 2 秒页面才稳定"` |
| `--output` | 结果输出目录 | `".\results\case-001"` |

**问题区间相关**（重要，见第 8 节优先级规则）：

| 参数 | 含义 | 使用场景 |
| --- | --- | --- |
| `--time-range` | 已知问题区间 | `"788297.52s-788298.37s"` |
| `--problem-duration-ms` | 只知道持续时间 | `--problem-duration-ms 2000` |
| `--start-marker` / `--end-marker` | 应用自定义唯一打点/Slice 边界 | `--start-marker APP_START_BEGIN` |
| `--operation-marker` | 一个正 duration 的业务 Slice，直接定义完整操作区间 | `--operation-marker OPEN_DETAIL_OPERATION` |
| `--response-marker` | 第一帧有效响应打点 | `--response-marker DETAIL_FIRST_FEEDBACK` |
| `--completion-marker` | 动作完成打点 | `--completion-marker` |

**Agent 相关**：

| 参数 | 含义 |
| --- | --- |
| `--agent` | `local`（默认，无需 Key）或 `qoder`（AI 分析） |
| `--provider` | `deepseek`（默认读 .env） |
| `--model` | 覆盖模型名 |

**Trace 转换相关**：

| 参数 | 含义 |
| --- | --- |
| `--trace-streamer` | 覆盖转换二进制路径（开发用） |
| `--trace-streamer-timeout` | 转换超时秒数（默认 600） |
| `--trace-cache` / `--no-trace-cache` | 是否用内容寻址 DB 缓存（默认开） |
| `--refresh-trace-cache` | 忽略缓存强制重新转换 |
| `--trace-cache-dir` | 指定缓存目录（默认 `.trace-agent\cache`） |

**其他**：

| 参数 | 含义 |
| --- | --- |
| `--target-process` | 目标应用进程提示（省略时由工具+Agent 联合判断） |
| `--baseline` | 可选基线 Trace，用于对比 |
| `--refresh-rate` | 显示刷新率（帧率/卡顿场景），如 60 |
| `--device` / `--build` | 设备型号 / 构建版本（写进报告上下文） |
| `--trace-id` | 自定义 Trace ID（默认用文件名） |
| `--progress` / `--no-progress` | 关闭交互式进度（适合 CI / 日志重定向） |

---

## 7. 四种问题类型详解

| `--type` | 问题 | 判断核心 | 推荐补充参数 |
| --- | --- | --- | --- |
| `cold-start` | 应用启动到首帧 | 启动阶段耗时、各阶段边界 | `--start-marker` / `--end-marker` 或稳定首页帧语义 |
| `response-latency` | 点击/输入 → 第一帧有效反馈 | 输入到反馈的时延 | `--problem-duration-ms`、`--response-marker` |
| `completion-latency` | 点击/输入 → 动作完全完成 | 三段式：输入→反馈、反馈→完成、总时长 | `--operation-marker`、`--response-marker`、`--completion-marker` |
| `frame-jank` | 帧率下降、慢帧、丢帧、卡顿 | 问题区间内 FPS、丢帧原因 | `--time-range`、`--refresh-rate 60` |

### 7.1 各场景示例命令

**冷启动（用应用自定义边界）**：

```powershell
.\.venv\Scripts\diting-agent.exe analyze app-start.htrace `
  --type cold-start `
  --scenario "进入首页并稳定" `
  --symptom "首页稳定耗时偏长" `
  --start-marker "APP_START_BEGIN" `
  --end-marker "HOME_PAGE_STABLE" `
  --agent qoder --provider deepseek `
  --output .\results\case-001
```

**响应时延（只知道持续时间）**：

```powershell
.\.venv\Scripts\diting-agent.exe analyze interaction.htrace `
  --type response-latency `
  --scenario "点击进入详情" `
  --symptom "响应约持续1800ms" `
  --problem-duration-ms 1800 `
  --agent qoder --provider deepseek `
  --output .\results\case-002
```

**完成时延（应用定义了业务操作 Slice）**：

```powershell
.\.venv\Scripts\diting-agent.exe analyze interaction.htrace `
  --type completion-latency `
  --scenario "点击进入详情并完成渲染" `
  --symptom "点击后约 2 秒页面才稳定" `
  --operation-marker "OPEN_DETAIL_OPERATION" `
  --response-marker "DETAIL_FIRST_FEEDBACK" `
  --agent qoder --provider deepseek `
  --output .\results\completion-001
```

> 注意：`--operation-marker` 对应 Trace 里一个「唯一且带 duration 的业务 Slice」，
> 它的 `[ts, ts+dur]` 就是应用自己定义的完整操作区间。
> 没有应用完成语义时，动画结束/帧静止只能当「候选」，不能把
> `completion_proven` 判为真——这是项目的严谨之处。

**帧率/卡顿**：

```powershell
.\.venv\Scripts\diting-agent.exe analyze scroll-jank.htrace `
  --type frame-jank `
  --scenario "滑动聊天列表" `
  --symptom "连续两次滑动明显卡顿，需要计算问题区间 FPS 并定位丢帧原因" `
  --time-range "2441.036097969s-2446.036097969s" `
  --refresh-rate 60 `
  --agent qoder --provider deepseek `
  --output .\results\scroll-jank
```

---

## 8. 问题区间是怎么确定的？（核心知识点）

「问题区间」（problem interval）是分析的锚点，它的确定**有严格的优先级**，
并且每条都会记录「哪条规则赢了」：

```text
1. 显式的 --time-range                          ← 最高优先
2. 唯一成对的应用自定义 Slice/Marker
   （--start-marker + --end-marker）
3. 场景语义边界（如冷启动结束 = 首页首个稳定帧，
   而不是 Trace 里最早出现的帧）
4. 兜底假设：假设 Trace 只含一次用户操作，
   取最后一个有效 TouchEvent/PointerEvent/点击打点为起点
   + --problem-duration-ms 推算出终点
```

⚠️ 重要：第 4 条是**可审计的假设**，不是 Trace 事实。如果 Trace 里明显有
多次操作、或输入打点不全，Agent 会降低置信度或拒绝该假设。

---

## 9. 读懂输出（results 目录）

每次分析生成一个输出目录：

```text
results/case-001/
├── report.html              # ★ 最终报告（自包含 HTML，直接双击打开）
├── findings.json            # 结构化结论（根因判断、置信度、建议）
├── evidence.json            # 全部证据（追加式、不可变、可审计）
├── run.json                 # 本次运行的元信息
├── agent-result.json        # Qoder 最终消息、解析来源、修复诊断
├── agent-log.jsonl          # Agent 每轮工具调用的 JSONL 审计日志
└── work/
    └── run-xxxxxxxxxxxx/
        ├── current.db       # 本次分析的 SQLite 数据库（Trace 转换产物）
        ├── baseline.db      # 提供 --baseline 时生成
        ├── current-trace-streamer.stdout.log
        └── current-trace-streamer.stderr.log   # 转换日志（报错先看这里）
```

**四类报告的对应关系**：

- `findings.json` → 结论：边界、阶段指标、根因、置信度、建议、回归检查
- `evidence.json` → 依据：每个证据 ID 与 Agent 结论一一对应
- `report.html` → 给人和演示看的最终形态

> 想自己验证 SQL？`work/run-*/current.db` 就是一个标准 SQLite 库，
> 可以用任何 SQLite 工具（如 DB Browser）打开查。

---

## 10. 缓存机制（省时间的法宝）

Trace 转换是最耗时的环节（大文件可能几分钟）。
默认开启**内容寻址缓存**：缓存键 = Trace 的 SHA-256 + TraceStreamer 二进制
SHA-256 + 转换格式版本。命中后直接用硬链接生成 `current.db`，秒开。

常用控制：

```powershell
# 临时关掉缓存
.\.venv\Scripts\diting-agent.exe analyze ... --no-trace-cache

# 强制重新转换并刷新缓存（改了转换二进制后用它）
.\.venv\Scripts\diting-agent.exe analyze ... --refresh-trace-cache

# 换个共享缓存目录（多项目共用）
.\.venv\Scripts\diting-agent.exe analyze ... --trace-cache-dir D:\trace-agent-cache
```

> 如果觉得「每次重跑都慢」，先确认没有误加 `--no-trace-cache`。

---

## 11. 常见问题 FAQ

| 现象 | 解决办法 |
| --- | --- |
| `Not logged in` | 执行 `qodercli login`，并用 `qodercli --list-models` 验证（SDK 内置 CLI 用法见 4.2 节） |
| `Failed to generate custom pool` | 通常是 BYOK 模型 ID 不在 Qoder 当前目录。项目已兼容旧的 `deepseek-v4-pro[1m]` 和 `deepseek-v4-flash`，确认 .env 里模型名正确 |
| Trace 转换重复耗时 | 确认没有误用 `--no-trace-cache` / `--refresh-trace-cache` |
| 分析失败但不知原因 | 看 `work/run-*/current-trace-streamer.stderr.log` |
| 首次转换大 Trace 很慢 | 正常，第二次同 Trace 会命中缓存 |
| 自动化/CI 环境 | 在 Secret 管理里同时提供 `QODER_PERSONAL_ACCESS_TOKEN` 和 `DEEPSEEK_API_KEY`，并加 `--no-progress`；**不要把 Key 提交进仓库** |
| 找不到 `.env` | `configure` 命令会自动创建；也可以手动复制 `.env.example` 为 `.env` 填写 |
| `trace-agent` 是什么 | 兼容旧命令名，`diting-agent` 与 `trace-agent` 指向同一个程序 |

---

## 12. 源码地图（想改代码从这里开始）

### 12.1 一条主链路走一遍

```text
cli.py analyze 命令
  → application/analyze.py 的 AnalyzeApplication.run()   ← 任务编排总控
      → trace/htrace.py      转换 Trace → SQLite
      → trace/cache.py       内容寻址缓存
      → application/preflight.py  确定性预分析（概览、候选区间）
      → skills/router.py     按 Trace 能力路由 Skill
      → tools/registry.py    按能力动态开放只读工具
      → agent/factory.py     创建 Agent（local / qoder）
      → agent/qoder.py       大模型对话 + 工具调用
      → evidence/store.py    固化证据
      → report/projection.py 证据 → 报告视图
      → report/renderer.py   HTML 渲染
```

### 12.2 核心目录速查

| 目录/文件 | 干什么的 | 想改什么看这里 |
| --- | --- | --- |
| `cli.py` | CLI 参数定义 | 加新参数 |
| `models.py` | 所有 Pydantic 模型 | 改数据结构 |
| `application/analyze.py` | 任务编排 | 改整体流程 |
| `scenarios/catalog.py` | 场景注册表 | 新增场景（见 12.3） |
| `skills/router.py` | Skill 选择 | 改路由逻辑 |
| `.qoder/skills/` | 分析方法论（Markdown） | 改 Agent 的"工作手册" |
| `database/` | 确定性 SQL 查询 | 改统计逻辑 |
| `tools/registry.py` | 工具注册 | 新增工具 |
| `evidence/` | 证据存储/索引 | 改证据模型 |
| `report/` | 报告生成 | 改报告样式/内容 |
| `trace/` | 转换与缓存 | 改缓存策略 |
| `vendor/trace_streamer/` | 转换二进制（含三平台） | 一般不动 |

### 12.3 如何新增一个问题类型（官方扩展规则）

1. 在 `ScenarioCatalog` 注册：Skill、报告标题、结果字段、预分析调用
2. 优先复用跨场景能力（进程/标记/帧/调度/唤醒链/Perf），再加场景专用工具
3. 场景校验加在 validation 层，**不要**改 Agent 适配器
4. 报告新章节通过 projection 数据加，不要在模板里做分析
5. 改指标语义前，先补契约测试和特征测试

### 12.4 新增工具的规则

1. 工具返回**确定性事实和限制**，不做 LLM 式结论
2. 声明所需 Trace 能力和有界调用预算
3. 大结果全量存 Evidence，只给模型紧凑视图
4. 多个消费方需要时，加类型化 Evidence 查询

---

## 13. 测试

```powershell
# 在项目根目录
uv run pytest
```

28 个测试文件覆盖：端到端、边界归一化、各场景工具、证据索引、
报告投影/渲染、Trace 缓存、Qoder Skill 访问限制等。

---

## 14. 学习路线建议（按你的目标选）

**只想用它分析 Trace**（使用者的路径）：
1. ✅ 第 3 章装环境 → 2. 第 5.2 节用 local 模式跑通一次 →
3. 第 4.2 节配好 Qoder + DeepSeek → 4. 第 7 章按你的问题类型挑参数 →
5. 第 9 章学会读报告

**想二次开发/做贡献**（开发者的路径）：
1. ✅ 第 3 章装环境 → 2. 先读 `README.md` 和 `docs/architecture.md` →
3. 按 12.1 的主链路把 `application/analyze.py` 走一遍 →
4. 读一个简单场景（`cold-start`）的 Skill + 工具 + 测试，体会
   「确定性事实 / Agent 语义判断」的分工 →
5. 跑 `uv run pytest` 保证测试全绿后再动手改

**几个值得记住的设计原则**（面试/写代码都能用上）：
- 确定性大对象（线程状态、CPU 分布、唤醒链、Perf Profile）由应用层自动合并进结果，模型只提交语义 Draft，不做重复序列化
- 分析方法写在 Skill 文件里，而不是硬编码在 Python system prompt 里
- 证据是追加式不可变的；结论必须引用证据 ID
- 工具调用有硬预算；SQL 只能走受限的 `query_trace_sql`
- 模型只能读选中 Skill 的目录，Shell/写文件/网络能力全部禁用

---

## 附录：PowerShell 速查

```powershell
# 进入项目
Set-Location "d:\Users\Administrator\Desktop\DitingAgent-main\DitingAgent-main"

# 等价命令写法（二选一，推荐后者）
.\.venv\Scripts\diting-agent.exe --version
uv run diting-agent --version

# 查看所有参数
uv run diting-agent analyze --help

# 查看版本
uv run diting-agent --version
```
