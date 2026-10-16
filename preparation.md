# 百度「AI 测试开发工程师」面试准备手册

> 目标岗位：北京 — AI 测试开发工程师（校招技术，J101055）
> 核心项目：DitingAgent（Trace 分析 Agent，GitHub: wbhlucky/Trace-analyse-Agent）
> 使用方式：先通读「岗位拆解」和「一句话主线」，再按「高频必问」逐题背诵推荐回答；算法和 Docker 部分单独突击。

---

## 0. 一句话主线（所有答案都往这句上靠）

> 我做的不只是「写 Agent」，而是把「对 Agent 做测试」这件事本身工程化：用**可插拔 SDK + 确定性工具边界 + Evidence 审计 + 三层 Eval**，让一个不可控的 LLM 变成可复现、可评分、可回归的性能诊断系统。简历上的 0.68→0.84、pass@1 65%→85%，本质是一个**把 AI Agent 当成被测对象来做质量度量**的闭环。

面试官最想验证的不是你会调 Claude API，而是三件事：

1. **风险意识**：你知道 LLM 会幻觉、会乱调工具、会自相矛盾，并且有工程手段挡住它。
2. **质量是可量化的**：你不是说「感觉 Agent 变好了」，而是有 pass@1、overall score、token/latency 这些硬指标。
3. **做完还能测**：你有 70+ Trace Eval 集、50+ 测试文件，能回答「你这个 Agent 自己怎么被测试」。

---

## 1. 岗位拆解：JD 每一句在面你什么

| JD 关键词 | 面试官真实考察点 | 你的项目对应资产 |
| --- | --- | --- |
| 智能化测试框架 / 平台 | 有没有「平台思维」而非脚本思维 | DitingAgent 的 CLI + 应用层 + Web 分层架构 |
| AI Agent 技术 / 任务编排 | 是否理解 Agent 运行时、工具、状态机 | Runtime：EventBus、StateMachine、Transport、Repair Loop |
| 脚本自愈 / 用例复用 | 是否做过失败可恢复、结果可复用 | Repair Loop、Resumable Run、内容寻址 Trace 缓存 |
| 大模型效果评测 | 是否懂 benchmark、golden、评估指标 | Eval Harness、DeterministicGrader、pass@1/k、pass^k |
| Prompt 工程 / 用例生成 | 是否把「分析逻辑」做成可复用的提示资产 | 6 类 Skill（.qoder/skills），分析方法与业务解耦 |
| 缺陷定位 / 根因分析 | 是否理解「定位」不等于「猜」 | 症状→边界→假设→证据→根因→建议的证据链 |
| 全链路质量保障 | 功能/接口/性能是否都覆盖 | pyproject + 50+ pytest 文件，覆盖 Runtime/Tool/Evidence/Report/Memory |
| 测试数据集搭建 | 数据从哪来、如何脱敏、如何评估 | 70+ 内部 Trace 私有维护 + 公开脱敏 demo |
| Docker / 企业级部署 | 环境一致性、镜像分层、CI | 项目仓库无 Dockerfile，需按第 8 节补救并诚实作答 |

---

## 2. 高频必问（按面经 + JD 交叉去重后排序）

1. 项目细节 + 技术栈选型（为什么）
2. 懂哪些测试框架，懂不懂百度的
3. 为什么投测试，而不是纯 AI/后端
4. 自己写过 MCP / Skill 吗，怎么写的
5. 数据指标怎么来的，数据集怎么来的
6. 企业级 Docker 用过吗，部署流程、Dockerfile 怎么写
7. 测试怎么测（这个 Agent 自己怎么保证质量）
8. Agent 最常见的失败是什么，怎么兜底（面经「跳槽快」背后是稳定性追问）
9. 算法题：删除链表第 N 个节点 + 复杂度
10. 自我介绍

---

## 3. 逐题精讲（考察意图 → 必须准备 → 推荐回答）

### 3.1 项目细节 + 技术栈选型

**考察意图**
- 判断项目是真做过还是简历包装。
- 判断你有没有「根据约束做技术决策」的能力，而不是堆名词。
- 面试官会顺着一个词一路深挖（为什么 Python 3.12、为什么 SQLite 不用 PG、为什么 Claude SDK 而不是手写 OpenAI）。

**必须准备的内容**
- 项目一句话：面向 HarmonyOS APP（微信/抖音/快手）的性能 Trace，做「数据抽取 → 瓶颈定位 → 根因归因 → 报告生成」的 Task 型 Agent。
- 四类问题：cold-start / response-latency / completion-latency / frame-jank。
- 核心分层：CLI → AnalyzeApplication → TraceAdapter → AgentFactory → AnalysisAgent（可插拔）→ SubmissionPolicy → EvidenceStore → ReportProjection → ReportRenderer。
- 关键设计：**LLM 只做语义决策，不做大数据序列化**。线程状态、CPU 分布、唤醒链、Perf Profile 这些确定性大对象由应用层从 Evidence 合并，模型只提交语义 Draft。
- 为什么 Python 3.12 + uv + hatchling：依赖隔离、跨平台分发、依赖组管理（optional extra）。
- 为什么 SQLite：Trace 转出的查询封闭、单机只读、易缓存、易序列化；不需要高并发写。
- 为什么 Claude Agent SDK 为主 SDK：Claude Code CLI + 内建 MCP Server，工具调用和运行态成熟。

**推荐回答（可直接说）**

> 这个项目我定位成「任务型 Trace 分析 Agent」，而不是通用聊天机器人。业务输入是一个 .htrace 文件加一段用户症状描述，输出是一份带证据链的 HTML 报告。目标是把性能工程师人工排查卡顿、冷启动、IPC 阻塞这几个小时的工作，收敛成 Agent 一轮可复现的分析。
>
> 技术栈我做了几个明确取舍：第一，Python 3.12 用 uv 管理依赖，核心链路在异步子进程、JSON 和 SQLite 之间做，Pydantic、Typer、pytest 最适合快速把评测链路搭起来；uv 用 optional extra 支持 claude/qoder/openai 三套 SDK 依赖隔离，装最小依赖不会拉全家桶。第二，SDK 抽象成 AnalysisAgent 协议，业务层只依赖 Protocol。Claude 是主 SDK 因为 Claude Code CLI 自带 MCP Server；qoder 是 BYOK 路由到 DeepSeek；openai 是契约示例，换起来只改 --agent。第三，存储选 SQLite 而不是 PG，因为 Trace 查询是只读、封闭、单机的，要的是内容寻址缓存 + 确定性查询 + 结果审计，PG 的高并发写是过度设计。最后，LLM 不碰原始大数据：线程状态、CPU 分布、唤醒链、Perf Profile 这些确定性大对象放在应用层，模型只提交语义 Draft，我再按 Evidence ID 合并，既省 token 也压缩模型编造空间。

---

### 3.2 懂哪些测试框架，懂不懂百度的

**考察意图**
- 校招会问主流框架，JD 明确写了 Pytest、Selenium、JMeter。
- 「懂不懂百度的」是压力题/防背题，不要假装会；重点展示「底层原理相通」。

**必须准备的内容**
- Pytest：fixture、parametrize、conftest、marker、mock、monkeypatch、插件体系。
- Selenium：定位策略、显式等待、Page Object、CI 集成；可主动引出现代化的 Playwright。
- JMeter：线程组、吞吐量、响应时间、P99、压力场景；与 Locust 的差异。
- 接口测试：FastAPI + httpx + pytest；契约测试。
- 百度框架：知道 AppBuilder、QAT、智测平台等概念即可，不确定就不硬说，落到「执行器 + 断言 + 报告 + 调度」。

**推荐回答**

> 我日常用得最熟的是 Pytest：DitingAgent 里 50 多个测试文件全是 pytest，覆盖 Runtime、Tool、Evidence、Report、Memory、Transport 六个模块，用 fixture 做临时 DB 隔离，parametrize 做多场景矩阵，monkeypatch 隔离子进程和时间。它对我的价值不是「跑用例」，而是把 Agent 的不确定性拆成一层层可断言的确定性组件。
>
> Selenium 和 JMeter 我都用过，但在这个岗位上我更想强调接口和 Agent 测试：接口层用 FastAPI + httpx + pytest 做契约测试，性能层关注 P50/P90/P99、并发和吞吐。Selenium 的核心是 Page Object、定位策略和等待策略；JMeter 的线程组 + 断言 + 报告和「执行器 + 断言 + 报告」是同一套骨架，只是驱动对象从 HTTP 换成 UI 或模型。
>
> 百度的框架我确实没在实习里直接接触过，但如果类比，核心大概率是「平台化 + 用例资产 + 调度 + 报告 + 度量」。百度要的是能把「为什么这么做」讲清楚的人。任何平台翻到底层，都是执行器、断言、可观测性、调度和资产管理这五件事。

---### 3.3 为什么投测试，而不是纯 AI/后端

**考察意图**
- 校招高频「动机题」，也是面经原题。
- 面试官担心你拿测试当跳板，入职后发现「测试没有开发体面」就跑。

**推荐回答（重点：把自己定位成「AI 测试平台建设者」，不是「点点点测试」）**

> 我投这个岗位不是因为退而求其次，而是因为我在实习里做的 Agent 项目自己就面临一个绕不开的问题：怎么保证这个 Agent 是对的、稳定的、效果可度量，这正好就是 AI 测试的核心。我想做的是「测试 AI 的 AI」，而不是重复性人工回归。
>
> 我在华为做 Trace Agent 时，最花时间的是做 Eval Harness、70+ 条 trace 测试集、pass@1 这些指标。我发现如果只把 Agent 当功能开发，上线后它输出错了你根本不知道，因为大模型的错是概率性的、分布式的、会漂移的。这让我对「质量保障」产生了很强的认同：传统软件错了是 exception，大模型错了是「看起来对但其实是编的」，后者的测试难度和工程价值更高。
>
> 所以这个岗位对我不是转行，是同一件事的正面：把测试框架平台化、测试用例智能化、大模型效果评测工程化，我做的就是这三件事的交叉。

---

### 3.4 自己写过 MCP / Skill 吗，怎么写的

**考察意图**
- 面经原题。JD 明确要「AI Agent、MCP、Prompt 工程、Skill」。
- 面试官会问协议细节，判断是真懂 MCP 还是只会说名词。

**必须准备的 MCP 知识（背牢）**
- MCP = Model Context Protocol，Anthropic 提出的开放协议，用 **JSON-RPC 2.0**。
- 三个角色：主机（Host）、客户端（Client）、服务端（Server）。
- Server 暴露三类能力：resources（数据）、tools（可执行动作）、prompts（模板）。
- 传输方式：stdio（本地子进程）、SSE / streamable HTTP（远程）。
- 你的 DitingAgent 里是什么：**Claude Code 内建一个 MCP Server**，把只读 Trace 工具（get_trace_overview、query_trace_sql、inspect_*）以 MCP tool 形式暴露给 Claude；同时用 PreToolUse Hook 限制 Read 只能访问本次启用的 Skill 文件。

**必须准备的 Skill 知识**
- Skill 是「可复用的方法论/提示资产」，不是一段硬编码 prompt。
- 项目里 6 类 Skill 放在 `.qoder/skills/`：trace-analysis（总控）、cold-start-analysis、response-latency-analysis、completion-latency-analysis、frame-jank-analysis、perf-sample-analysis。
- 每个 SKILL.md 有 name/description + 工作流 + 路由 + 证据契约 + 防幻觉护栏。
- 用法：`ScenarioSkillRouter` 根据 `scenario_type` + Trace capabilities 动态选 Skill；Skill 内容作为上下文给 Agent，分析逻辑不写死在 Python system prompt 里。

**推荐回答**

> MCP 我写过，而且不是只「用过现成的」。在 DitingAgent 里，Claude 适配器会起一个内建 MCP Server，把我们自己实现的只读工具注册进去。工具边界是我自己定义的：get_trace_overview 必须先调，query_trace_sql 只允许 SELECT / WITH...SELECT / EXPLAIN，禁止写操作、ATTACH、PRAGMA，参数必须是位置参数，max_rows 限 500。所以 MCP 对我不只是通道，是给 LLM 做权限收口的手段。
>
> MCP 协议层基于 JSON-RPC，Server 暴露 resources、tools、prompts 三类能力，我项目里主要是 tools。为了防止 Agent 乱读文件，我还在 Claude Code 上加了一个 PreToolUse Hook：Read 工具只允许访问本次启用的 Skill 目录，别的路径直接 deny。这就是把能力边界同时做在工具层和文件系统层。
>
> Skill 我理解成「方法论资产」，不是一句 prompt。我把 trace 分析拆成 6 个 Skill，一个总控 trace-analysis 负责路由和证据契约，下面挂冷启动、响应时延、完成时延、帧率卡顿、Perf 采样五个专项。每个 SKILL.md 里都写了工作流、假设生成、防护规则，比如「只分析性能，不分析内存」「不要把墙钟时间说成 CPU 时间」「没有证据别声称已确认根因」。这些让 Agent 的行为可预期、可回归。

---

### 3.5 数据指标怎么来的，数据集怎么来的

**考察意图**
- 面经原题，也是 JD 里「测试资产库、评测数据集、基准指标」的直接映射。
- 面试官要区分「抄了个 pass@1 名词」和「真做过数据飞轮」。

**必须准备的指标定义（逐条积累）**
- **Overall Score**：DeterministicGrader 加权分，默认权重 diagnosis 0.35 / evidence 0.25 / metrics 0.15 / reasoning 0.10 / report 0.10 / uncertainty 0.05，机器判定不靠人打分。
- **Diagnosis Score**：根因召回，命中的根因数 / gold 根因数；命中 forbidden conclusion 直接归零。
- **Evidence Score**：命中的证据项 / gold 证据项。
- **Metrics Score**：输出数值与 gold 数值误差在 tolerance 内算对。
- **pass@1 / pass@k / pass^k**：MultiTrialAggregator 输出，三个指标彼此独立，不折进 correctness。
- **Trajectory 指标**：tool errors、重复调用、budget、forbidden actions，单独评「工具用得干不干净」。
- **Operational 指标**：turns、tool_calls、tool_errors、tokens、ttft、latency。
- 简历数字：Overall 0.68→0.84，pass@1 65%→85%，Diagnosis Accuracy 最高 96%，Token/Latency 降 18%/24%。

**数据集来源（诚实且有说服力）**
- 内部真实 HarmonyOS 性能 Trace 私有维护，涉密不能公开上传。
- 公开仓库只放脱敏 demo（用例结构 + gold oracle + grader + trials 完整保留）。
- gold oracle 构建：每个 case 写清楚问题类型、必须召回的关键根因、必须引用的证据、**允许的根因状态**、**不允许的结论**、数值容差。
- 「允许/不允许」是让 eval 有区分度的关键——不是让模型复述答案，而是验证它「说对」和「没说错」。

**推荐回答**

> 指标不是我拍脑袋给的。我早期人工拍板，后来把评审判定做成 DeterministicGrader：把每个 case 的 gold 标准结构化，根因召回、证据引用、数值误差、禁止结论全部用规则打分。diagnosis 0.35、evidence 0.25、metrics 0.15、reasoning 0.10、report 0.10、uncertainty 0.05，加总 1.0。这样同一个 Agent 跑多次也能用 pass@1 稳定描述真实能力，而不是「这轮看着还行」。
>
> 我特别强调三层观察分开看：结果正确性（outcome）、工具轨迹（trajectory）、运行成本（operational）。很多人 eval 只看结果对不对，但一个 Agent 可能结果对却调错工具、撞了三轮、超预算，这种上生产会炸。所以我把 tool errors、重复调用、budget 单独做一层评分，turns/tokens/ttft/latency 再单独做一层，不折进正确率。
>
> 数据集来处是华为内部真实性能 Trace，涉密，我保留私有用例集但不上传；公开仓库用脱敏版把每个 case 的题面、gold oracle、grader 权重和 trials 都暴露出来，证明评测链路是同一套。Gold oracle 里我最看重三个字段：允许的根因状态、不允许的结论、数值容差。它决定评测是在考「会不会造正确答案」，还是在考「有没有证据、会不会乱下结论」。

---### 3.6 企业 Docker 用过吗，部署流程、Dockerfile 怎么写

**考察意图**
- 面经原题。简历技术栈写了 Docker，面试官大概率会顺藤摸瓜。
- **诚实风险提示**：你这个仓库目前没有 Dockerfile / compose 文件。不要硬说「项目里写了 Dockerfile」，会被当场抓包。正确策略：承认「当前主流程用 uv + 本地子进程，容器化还在 CI 规划里」，然后展示你懂 Docker 的工程原理，面试前可补一个最小 Dockerfile。

**必须准备的 Docker 知识**
- 分层缓存：基础层 → 依赖层 → 源码层，依赖只随 lock 文件变化而变化。
- 多阶段构建：build 阶段装依赖，runtime 阶段只放产物，减小镜像。
- 非 root 用户、`.dockerignore`、healthcheck、信号处理（SIGTERM）。
- 项目难点：Claude Code CLI 二进制、MCP 跨进程、Trace 大文件、Python 3.12 + uv。
- 部署流程：git → CI 构建镜像 → 推镜像仓 → K8s/容器运行时拉起 → 挂载 Trace/结果卷 → 环境变量注入密钥 → 日志/监控。

**推荐回答（含补救话术）**

> 我的项目当前主流程是 uv + 本地 Claude Code 子进程，Dockerfile 还不在仓库主分支——我会坦诚这一点。但我理解怎么正确容器化，也做过企业级容器部署的基本链路。
>
> 如果给 DitingAgent 写 Dockerfile，会这么分层：第一，python:3.12-slim 做基础镜像；第二，先 COPY pyproject.toml 和 uv.lock 再 uv sync --extra claude，把依赖层单独做一层，锁文件不变就不重建；第三，最后才 COPY 源码，源码改动不触发依赖重装；第四，用多阶段构建，build 阶段装编译依赖，runtime 阶段只保留 .venv 和源码。
>
> 生产还要做几件事：Claude Code CLI 要打进镜像或从固定版本拉取；运行时用非 root 用户；.dockerignore 排除 .venv、results、.env；启动命令正确处理 SIGTERM。部署链路就是 CI 构建 → 推镜像仓库 → 容器编排拉起 → Trace 和结果目录挂 volume → 密钥走环境变量或 Secret 不写进镜像。这个项目没做到主分支是我自己知道的明确 gap，不是我不懂。

---

### 3.7 测试怎么测（这个 Agent 自己怎么保证质量）

**考察意图**
- JD 核心：全链路质量保障 + 大模型专项测试。
- 面试官想知道你「用 AI 做测试」之外，还懂不懂「测 AI」。

**推荐回答（分四层讲）**

> 我把 DitingAgent 的质量保证分成四层。
>
> 第一层是确定性代码的单元测试。Trace 转换、边界归一化、Repository、Evidence、报告渲染这些没有模型参与的逻辑，全部用 pytest 覆盖，50 多个文件，核心是 test_sdk_pluggability 和 test_contract 这类契约测试。
>
> 第二层是 Agent 契约测试。不管底层是 Claude 还是 Qoder，统一走 AnalysisAgent 协议；换 SDK 只允许触碰到 adapter、extra 依赖和 registry 三处，业务层零改动。用 local 确定性 Agent 做端到端冒烟，不依赖任何模型密钥。
>
> 第三层是大模型效果评测，这是最关键的专项测试：不是看一次输出，而是 multi-trial，从正确性、工具轨迹、运行成本三个维度跑 pass@1/pass@k/pass^k。这一步能抓出 LLM 的常见病——幻觉、漏证据、乱下结论、超 budget、重复调工具。
>
> 第四层是结果自检 + 可恢复性。模型提交前有 AnalysisResultValidator 和 SubmissionPolicy 做 Schema 和证据硬校验；提交后还有 ResultVerifier。运行中外部 Agent 失败，runtime 有 Repair Loop 和 Resumable Run，中断能接着跑，不让环境噪声污染统计。
>
> 这样从「没有 LLM 的代码」到「有 LLM 的决策」每一层都有可断言的边界。

---

### 3.8 Agent 最常见的失败是什么，怎么兜底

**考察意图**
- 面经「跳槽快」表面是稳定性，本质是「你知不知道 LLM 生产上会怎么坏」。
- 这是拉开校招生差距的题。

**推荐回答**

> 我把 Agent 的失败分成四类，对应四种兜底。
>
> 第一类是工具错误，比如 SQL 写错、参数越界。做法是工具只读、白名单限制，出错记录到 tool audit，TrajectoryGrader 专门扣这种分。
>
> 第二类是幻觉/过度自信，模型说一个很确定的根因但没有证据。我用根因状态三层强制区分：observed 是事实，suspected 是有证据但有替代解释，confirmed 必须独立证据或基线排除替代方案。配合 SubmissionPolicy 的硬 gate：证据没收集够就不允许提交。
>
> 第三类是循环或超预算。阶段工具设成只允许调一次，重复调用判 forbidden，也有 job deadline 和 token/tool budget，超了就终止，不能无限烧钱。
>
> 第四类是运行故障而非模型错误，比如进程挂了、超时、SDK 抽风。用 Repair Loop 重试，Run 可以中断后恢复；同时把 trial 失败分类成 agent_failure / infra_failure / timeout / invalid / eval_failure，环境故障不污染正确率。
>
> 一句话：我不追求让 LLM 100% 不犯错，而是让每种错都能被检测、被归类、被回滚或重试。

---### 3.9 算法题：删除链表第 N 个节点

**考察意图**
- 面经原题，校招必考。大概率是 LeetCode 19「删除链表的倒数第 N 个结点」。
- 面试官会追问：为什么用哑节点？边界条件？时间/空间复杂度？

**标准解法（快慢指针 + 虚拟头节点）**

```python
from typing import Optional

class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

def remove_nth_from_end(head: Optional[ListNode], n: int) -> Optional[ListNode]:
    dummy = ListNode(0, head)   # 处理「删头节点」的边界
    fast = slow = dummy

    # 让 fast 先走 n+1 步，slow 最终停在「待删节点的前一个」
    for _ in range(n + 1):
        fast = fast.next

    while fast:
        fast = fast.next
        slow = slow.next

    slow.next = slow.next.next
    return dummy.next
```

**口述复杂度**
- 时间：O(n)，fast 先走 n 步，再和 slow 一起遍历剩余节点，整体只遍历一遍。
- 空间：O(1)，只用 dummy/fast/slow 三个指针，无额外数据结构。

**加分追问处理**
- 为什么 dummy 节点：当 n 等于链表长度时要删头节点，没 dummy 就得单独写 if。
- n 非法怎么办：n<=0 或 n>链表长度，先遍历一遍数长度或用哨兵判断；面试先说「题目隐含 n 合法，但防御式我会先校验」。
- 删除正数第 N 个：直接指到第 N-1 个节点再改 next，同样 O(n)/O(1)。

---

### 3.10 自我介绍（60–90 秒版）

> 面试官好，我叫王博涵，杭州电子科技大学控制工程硕士，2027 届。
>
> 我的履历用一个关键词概括：把 AI Agent 做成可测试、可度量的工程系统。我在华为做过手机 APP 性能 Trace 分析 Agent，从数据抽取到根因归因到报告生成全链路打通，同时自己搭了一套 Eval Harness——70 多条真实 Trace 测试集、多层指标，把 Overall Score 从 0.68 提到 0.84、pass@1 从 65% 提到 85%。在三花做过法务诉讼 Agent，重点是 MCP 多步检索和 RAG，把资料处理耗时降低 98.3%。这些项目给我的最大体会是：大模型的错是概率性的、会漂移的，传统「跑一遍看结果」的测试方式不管用，必须把评测数据集、指标体系、Prompt/Skill 资产和回滚机制都工程化。
>
> 这也是我投百度 AI 测试开发的原因——我想做的不是点点点测试，而是「测试 AI 的 AI」，把大模型用例生成、智能测试分析、效果评测这些能力真正落到平台里。谢谢。

---

## 4. 必须背熟的十六个关键词（防止被追问卡壳）

1. **Provider-agnostic Runtime**：业务只依赖 Protocol，不绑定具体 SDK。
2. **AnalysisAgent Protocol**：统一 analyze 入口，checkpoint 可选扩展。
3. **MCP + JSON-RPC 2.0**：resources / tools / prompts；stdio / SSE。
4. **PreToolUse Hook**：Read 只允许读本次 Skill 目录，文件级权限收口。
5. **只读工具 + 位置参数 + max_rows 500**：禁止写、ATTACH、PRAGMA。
6. **Evidence ID 引用制**：模型只引用证据 ID，不重复序列化大对象。
7. **确定性/LLM 边界**：确定性流程交给 code，语义分歧才给模型。
8. **Skill Router**：scenario_type + capabilities → 动态加载方法论。
9. **根因三层状态**：observed / suspected / confirmed。
10. **forbidden conclusions**：说错命中黄金标准直接 diagnosis 归零。
11. **三层 Eval**：Outcome / Trajectory / Operational，互不折叠。
12. **pass@1 / pass@k / pass^k**：分别衡量单次、多次尝试、全对概率。
13. **trial 失败 taxonomy**：success / agent_failure / infra_failure / timeout / invalid / eval_failure。
14. **Repair Loop + Resumable Run**：失败重试与中断恢复。
15. **内容寻址 Trace 缓存**：相同 Trace 二次运行命中 SQLite 缓存。
16. **Python 3.12 + uv + hatchling**：依赖隔离、跨平台构建、optional extra。

---

## 5. 面试前必做的三件实事（不是背，是补缺口）

1. **补 Dockerfile**：在仓库加一个最小 Dockerfile + .dockerignore，能本地 build 最好。哪怕面试不用，写进「项目复盘」也有底气。结构见 3.6。
2. **跑一遍 eval demo**：用 local（无模型）跑通 `eval run --root evals/cases --agent local`，确保能脱口而出「我复现过 pass@1 怎么算出来」。
3. **补算法模板**：LeetCode 19 + 24（两两交换）+ 206（反转链表）+ 141（环形链表），后面三个是链表高频连锁追问。

---

## 6. 最后一张自检清单

- [ ] 能把项目一句话讲清楚，不打开代码。
- [ ] 能说出「为什么 Python / 为什么 SQLite / 为什么 Claude SDK」三条选型理由。
- [ ] 能画出 DitingAgent 的分层图并解释确定性/LLM 边界。
- [ ] 能背出 pass@1 / pass@k / pass^k 的差异。
- [ ] 能说清 70+ 测试集为什么公开版要脱敏。
- [ ] 能写出 LeetCode 19 的 dummy 节点解法并说 O(n)/O(1)。
- [ ] 能诚实回答 Docker 的现状，同时讲清正确做法。
- [ ] 能把「为什么投测试」说到对方觉得你不是来过渡的。

> 注：以上推荐回答均可在面试中直接展开；数字、指标、模块名与 DitingAgent 仓库实现一致。Docker 部分属于简历与仓库的已知差异，建议面试前补文件或如实说明，切勿现场编造「仓库里有 Dockerfile」。