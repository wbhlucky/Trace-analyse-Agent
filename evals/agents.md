### Grader 匹配原则

DeterministicGrader 必须优先基于结构化字段进行评判，不应依赖自然语言全文 substring 匹配。

* **诊断项（root cause / bottleneck）**：Gold 与 Agent 输出统一归一化为 canonical concept 后比较。优先匹配结构化字段；Agent 仅提供自然语言时，再使用 canonical ID + alias/normalization 作为 fallback。
* **uncertainty**：不比较 Gold 与 Agent 的原句，改为比较结构化 `topic` 是否命中。
* **原则**：`Structured exact match → Canonicalization/Alias → LLM semantic judge`。确定性规则能够判断的问题保持 deterministic，不提前引入 LLM。
* **本阶段范围**：只修正结构化匹配与 normalization，不修改现有 7 个评分维度、权重及 EvalHarness，不引入 embedding similarity 或 LLM Judge。

直接给你结论：

> **你现在的 `DeterministicGrader` 可以作为第一层，但不能作为最终的 Agent Grader。**
>
> 2026 年成熟 Agent 的实际落地方案已经不是“纯确定性评分”或者“全靠 LLM Judge”二选一，而是：
>
> **Outcome Deterministic + Trajectory/Trace Grader + LLM Judge + Human Calibration + 多次 Trial + 分层聚合。**
>
> Anthropic 在 2026 年公开的 Agent Eval 方法里明确写到：Agent 评测通常组合 **code-based、model-based、human graders**，一个 task 可以有多个 grader；并且推荐“能确定性判断就用 deterministic，需要语义理解再用 LLM，人工用于校准”。([Anthropic][1])

而且 OpenAI 当前的 Grader 体系也已经原生提供了 **Python grader、string check、similarity、score-model、label-model、multi grader**，本质上也是同样的组合式架构。([OpenAI平台][2])

---

# 一、先判断你现在这套：对，但还停在 V1

你现在：

```text
diagnosis   0.35
evidence    0.25
metrics     0.15
reasoning   0.10
uncertainty 0.05
report      0.05
efficiency  0.05
```

我认为：

**作为你项目第一版 grader，完全合理。**

尤其这几个：

* diagnosis
* evidence
* metrics

应该坚决保持 deterministic。

因为你的冷启动问题不是开放式创作，而是：

> 从 Trace 中判断真实性能问题。

其中很多结果是可以直接验证的：

```text
根因是什么？
瓶颈是什么？
证据有没有？
startup_duration 是多少？
presentation_duration 是多少？
```

这种东西让 LLM 来判断反而会降低评测可信度。

Anthropic 自己也明确强调：能确定性验证的东西，应该优先 deterministic，而且 deterministic 的优点就是便宜、客观、可复现、容易 debug。([Anthropic][1])

所以：

### 你现在不是“纯 deterministic 错了”。

真正的问题是：

> **你把 deterministic grader 当成了最终完整 grader。**

成熟 Agent 不这么做。

---

# 二、你真正应该做的架构

你的最终架构应该变成：

```text
                    Agent Run
                       │
                       ▼
                 ┌─────────────┐
                 │ Trace/Result│
                 └──────┬──────┘
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
 Deterministic      Trajectory        LLM Judge
   Graders           Graders             Graders
        │               │                │
        └───────────────┼────────────────┘
                        ▼
                 Composite Grader
                        │
                        ▼
                 Final Eval Result
                        │
             ┌──────────┼───────────┐
             ▼          ▼           ▼
          PASS/FAIL   Score       Failure
                                  Taxonomy
                        │
                        ▼
                 Human Calibration
```

这才是现在比较成熟的 Agent Eval 架构。

Anthropic 在 2026 年公开的评测方法里就是这个思路：一个 task 可以拥有多个 graders，每个 grader 又可以包含多个 assertions；grader 同时作用于 **outcome 和 transcript/trace**。([Anthropic][1])

---

# 三、你的 `7 个维度` 不应该被删除，而应该拆层

我建议你直接这样改。

## Layer 1：Outcome Grader

这是你的核心确定性评分。

### diagnosis

```text
root_causes
bottlenecks
```

### evidence

```text
expected evidence
```

### metrics

```text
startup_duration
presentation_duration
...
```

### report schema

```text
summary
findings
limitations
```

这一层：

> **100% deterministic。**

不要上 LLM。

---

# 四、Layer 2：Trajectory / Trace Grader

这是你现在明显缺失的一层。

你的系统本身就是 **Trace Analysis Agent**，所以不能只看最终报告。

要额外检查 Agent 的行动过程：

```text
tool call
tool args
tool result
turn
failed tool
repeated tool
wrong tool
evidence retrieval
```

例如：

```yaml
trajectory:
  required_tools:
    - get_trace_overview
    - get_startup_slices

  forbidden_tools:
    - modify_trace

  max_tool_errors: 2

  max_turns: 20
```

但是有一个非常重要的原则：

> **不要把“必须按照某条路径走”作为硬性正确标准。**

Anthropic 特别提醒过，过度要求固定 tool-call 顺序会导致 grader 很脆弱，因为 Agent 可能通过另一条完全合法的路径得到正确结果。更合理的是优先评价“产生了什么结果”，而不是强制规定“必须怎么走”。([Anthropic][1])

所以你可以：

```text
❌ 必须：
tool A → tool B → tool C

✅ 推荐：
至少获得 startup slice
获得 presentation event
证据能够支撑 diagnosis
```

这很重要。

---

# 五、Layer 3：LLM Judge

这个你现在没有，但**我建议一定加**。

不过不是把你现在的：

```text
reasoning = LLM
```

直接塞进去。

而是单独成为：

```text
SemanticGrader
```

专门处理确定性规则难以解决的东西。

比如：

### 1. Reasoning quality

Agent：

> RenderService VSync 生成正常，但 Flutter VSync 接收频率明显下降，因此瓶颈位于 VSync 分发链路，而非 Flutter raster 执行阶段。

这个东西很难靠字符串完全判断。

LLM Judge 更适合判断：

```text
是否建立了：
现象 → 证据 → 推理 → 结论
```

---

### 2. Evidence grounding

例如：

Agent 说：

> UI thread 被 Binder 阻塞 80ms。

但 Trace 里根本没有这个证据。

deterministic 可以检测一部分，但语义层面的：

> “这个结论是否真的由这些 evidence 支撑？”

非常适合 LLM Judge。

---

### 3. Recommendation quality

例如：

```text
建议优化 Flutter raster thread
```

但证据实际上说明问题发生在：

```text
RenderService → VSync distribution
```

这种错误不能简单靠 presence check 解决。

---

### 4. Report quality

比如：

```text
summary
finding
analysis
recommendation
limitations
```

字段存在不代表写得对。

你现在：

```python
if finding.analysis:
    score += ...
```

这种只是：

> **结构完整性检查**

不是：

> **推理质量评测**

这两个必须区分。

---

# 六、所以你现在的 `reasoning` 其实应该拆成两个

现在：

```text
reasoning 0.10
```

我建议以后变成：

```text
reasoning_structure
reasoning_quality
```

例如：

```text
reasoning_structure      deterministic
reasoning_quality        LLM judge
```

这样非常干净。

---

# 七、还有一个你现在设计里我认为必须改的问题：efficiency

你现在：

```text
efficiency = 工具调用越少越高
```

**这个不要这么做。**

这是典型的“看起来合理，实际上会把 Agent 优化歪”的指标。

比如：

### Agent A

```text
10 次工具调用
最终找到了真正根因
```

### Agent B

```text
3 次工具调用
直接猜错
```

你不能因为 B 更省工具调用，就给它更高 efficiency 分。

所以成熟做法是：

## 正确关系

```text
Correctness
    ↓
先保证任务成功
    ↓
再比较成本/效率
```

也就是：

```text
primary:
  correctness

secondary:
  latency
  token
  cost
  tool_calls
```

而不是：

```text
correctness + fewer_tool_calls
```

混成同一个“质量”。

Anthropic 的 Agent Eval 也把 transcript metrics，例如 turn 数、tool calls、tokens、latency 单独作为 tracked metrics，而不是简单把它们等同于 task correctness。([Anthropic][1])

所以我建议你：

```text
efficiency
```

从：

```text
0.05 final correctness score
```

改成：

```text
performance metrics
```

单独统计：

```text
tool_calls
turns
tokens
latency
cost
```

最终报告：

```text
Correctness: 91%
Latency: 13.2s
Tokens: 38k
Tool calls: 17
Cost: $0.08
```

而不是：

```text
Final score = 0.93
```

把这些东西揉一起。

---

# 八、你真正应该采用的最终评分模型

我建议你的 Agent Eval V2 直接定成：

```text
                 ┌───────────────┐
                 │  Outcome      │
                 │  Deterministic│
                 └──────┬────────┘
                        │
                        │
                 ┌──────▼────────┐
                 │ Trajectory    │
                 │ Deterministic │
                 └──────┬────────┘
                        │
                        │
                 ┌──────▼────────┐
                 │ Semantic      │
                 │ LLM Judge     │
                 └──────┬────────┘
                        │
                        ▼
                ┌─────────────────┐
                │ Hard Gates      │
                │ + Partial Score │
                └────────┬────────┘
                         │
                         ▼
                  PASS / SCORE
```

---

# 九、最关键的是：不要把所有东西简单加权

你现在：

```text
0.35
+0.25
+0.15
+...
```

可以保留作为内部 score。

但**最终 PASS 不应该只看一个总分。**

例如：

```text
diagnosis = 0
evidence = 1
metrics = 1
reasoning = 1
```

加起来可能：

```text
0.65
```

这时候如果阈值：

```text
>= 0.6 PASS
```

就出现：

> **根因完全错了，但系统判定 PASS。**

这是你的业务绝对不能接受的。

所以成熟设计更适合：

## Hard Gate + Weighted Score

例如：

```text
必须满足：

diagnosis >= 0.5
evidence >= 0.5
metrics >= 0.5

AND

total_score >= 0.75
```

这样：

```text
根因完全错误
```

即使：

```text
报告写得再漂亮
reasoning 再漂亮
```

也必须：

```text
FAIL
```

这对于你的“性能根因分析”尤其重要。

---

# 十、你的冷启动 grader，我建议直接升级成这样

## Cold Start Grader V2

```text
A. Outcome Correctness
    diagnosis
    evidence
    metrics

B. Reasoning Quality
    reasoning_structure
    causal_reasoning
    recommendation

C. Trace Quality
    evidence_grounding
    tool_usage
    unnecessary_actions
    tool_errors

D. Report Quality
    completeness
    clarity
    consistency

E. Reliability
    pass@k
    pass^k

F. Efficiency Metrics
    latency
    tokens
    tool_calls
    cost
```

注意：

**真正决定任务有没有做对的是 A。**

B/C/D 用来告诉你：

> 为什么这个 Agent 做得好/不好。

F 是性能指标。

这比：

```text
7 个东西加权
```

更符合成熟 Agent Eval 的思路。

---

# 十一、你现有的 `Gold` 结构也要升级

你现在：

```yaml
root_causes:
bottlenecks:
evidence:
metrics:
uncertainty:
```

这个方向是对的。

但以后不要只保存：

```yaml
expected:
```

还应该保存：

```yaml
acceptable_variants:
constraints:
must_have:
must_not_have:
rubric:
```

例如：

```yaml
root_causes:
  - id: vsync_distribution
    type: render_vsync_distribution
    required: true
    acceptable_aliases:
      - vsync_distribution
      - render_vsync_delivery

bottlenecks:
  - type: flutter_vsync_delivery
    required: true

metrics:
  startup_duration_ms:
    value: 842
    tolerance: 30

evidence:
  - event: SendVsyncTo(flutterSyncName)
    required: true

semantic_rubric:
  reasoning:
    - "结论必须由 trace evidence 支撑"
    - "必须区分 VSync generation 与 VSync delivery"
    - "不得将问题归因到缺乏证据支持的 Flutter raster execution"

constraints:
  forbidden_claims:
    - "GPU utilization is the root cause"
```

这才开始接近真正成熟的 Agent eval case。

---

# 十二、还必须增加一个你现在很容易忽略的东西：多次运行

Agent 是 stochastic 的。

Anthropic 现在公开的 eval 方法明确强调：

> 一个 task 不是只跑一次，而是多个 trial，因为模型每次运行可能得到不同结果。([Anthropic][1])

所以：

```text
cold_start_001
```

不要只跑：

```text
1 次
```

而应该：

```text
trial 1
trial 2
trial 3
trial 4
trial 5
```

然后报告：

```text
pass@1
pass@3
pass^3
mean score
score variance
```

其中：

### pass@k

> k 次里成功至少一次。

### pass^k

> k 次全部成功。

Anthropic 目前也明确建议根据产品要求选择这两个指标；对于你这种性能诊断 Agent，我会更加关注 **pass^k**，因为你不是“碰巧找到一次正确答案”，而是需要“稳定分析正确”。([Anthropic][1])

例如：

```text
pass@1   = 82%
pass^3   = 55%
```

这比一个：

```text
overall_score = 0.87
```

信息量大得多。

---

# 十三、你到底要不要 LLM Judge？

**答案：要。**

但不是替代你现在的 DeterministicGrader。

而是：

```text
DeterministicGrader
        +
LLMJudge
        +
TrajectoryGrader
```

这三层一起。

Anthropic 2026 年公开的 eval 方法明确就是这么做的，而且还特别强调 LLM judge 必须通过人工专家进行 calibration；复杂任务可以把不同维度拆成独立 judge，而不是一个 LLM 包打天下。([Anthropic][1])

OpenAI 当前公开的 grader API 也已经直接支持：

```text
Python
String check
Similarity
Score model
Label model
Multi grader
```

这本身就说明工业方案已经不是“一个 grader”。([OpenAI平台][3])

---

# 十四、还有一个非常重要的业界变化

最近的 Agent Eval 已经开始从：

```text
只看最终答案
```

转向：

```text
Outcome + Trajectory
```

原因很简单：

一个 Agent 完全可能：

```text
最终答案正确
```

但是：

```text
中间工具调用错误
错误地修改状态
错误地访问数据
走了危险路径
碰巧得到正确答案
```

最新的 Agent trajectory evaluation 研究也在直接指出这个问题：**outcome-only judge 会漏掉“结果看起来正确、过程实际上错误”的 silent failures**。([arXiv][4])

所以你这个项目尤其应该做 Trace Grader。

因为：

> **你的核心产品本身就是 Trace Analysis。**

别人做 Agent Eval 还得额外收集 trajectory。

你天然就有。

这是你的优势。

---

# 十五、对于你这个项目，我建议最终代码结构直接这样设计

```text
src/trace_agent/eval/

├── graders/
│   ├── base.py
│   ├── deterministic.py
│   ├── trajectory.py
│   ├── semantic.py
│   ├── efficiency.py
│   └── calibration.py
│
├── aggregate.py
├── harness.py
├── models.py
├── metrics.py
└── registry.py
```

然后：

```python
grader = CompositeGrader(
    graders=[
        DeterministicOutcomeGrader(),
        TrajectoryGrader(),
        SemanticLLMGrader(),
    ],
    gates=[
        "diagnosis",
        "evidence",
        "metrics",
    ],
)
```

---

# 十六、你的 `DeterministicGrader` 现在该不该推翻？

**不要推翻。**

我反而建议：

### V1 保持

```text
DeterministicGrader
```

继续作为：

> **Golden correctness layer**

然后：

### V2 新增

```text
TrajectoryGrader
SemanticGrader
CompositeGrader
```

最后：

```text
DeterministicGrader
        ↓
判断“有没有做对”
        
LLM Judge
        ↓
判断“做得好不好”
        
Trajectory
        ↓
判断“怎么做的”
        
Metrics
        ↓
判断“花了多少代价”
```

---

# 十七、你现在最应该做什么

我直接给你排序：

### 第一优先级

**先把当前 DeterministicGrader 做扎实。**

但做三件事：

```text
1. 从“文本 substring 匹配”
   升级为结构化字段匹配

2. 引入 hard gate

3. efficiency 从 correctness score 中拿出来
```

---

### 第二优先级

增加：

```text
TrajectoryGrader
```

检查：

```text
tool calls
tool errors
evidence retrieval
turn count
invalid actions
```

---

### 第三优先级

增加：

```text
LLM SemanticGrader
```

只负责：

```text
reasoning quality
causal validity
evidence grounding
recommendation quality
report quality
```

不是让 LLM 决定：

```text
startup_duration = 832ms
```

这种机器可以直接判断的东西。

---

### 第四优先级

增加：

```text
multi-trial
pass@k
pass^k
```

---

### 第五优先级

做：

```text
human calibration
```

抽样人工评：

```text
Agent score
vs
Human score
```

检查 LLM Judge 的一致性。

这也是成熟 eval 系统长期维护的关键。Anthropic 明确建议持续读取 transcripts 和 grades，并用人工校准模型 grader；他们甚至强调，在没检查 transcript 前，不应直接相信 eval 分数。([Anthropic][1])

---

# 十八、最终给你一个非常明确的判断

你现在这个：

```text
DeterministicGrader
7 dimensions
fixed weights
no LLM
```

### 对于 V1：

**正确。**

### 对于最终工业级 Agent Eval：

**不够。**

你应该把它升级成：

```text
                 Agent Evaluation
                        │
        ┌───────────────┼────────────────┐
        │               │                │
        ▼               ▼                ▼
   Deterministic   Trajectory       LLM Judge
      Outcome         Trace          Semantic
        │               │                │
        └───────────────┼────────────────┘
                        ▼
                 Hard Gates
                        +
                 Weighted Score
                        │
                        ▼
                Multi-Trial Eval
                        │
                        ▼
             Human Calibration Loop
```

而且：

> **不要把 LLM Judge 加到现在的 `7 项加权`里就算完事。**

那样只是“从 V1.0 变成 V1.1”，不是成熟 Agent Eval。

你现在这个项目真正值得做的是：

> **把 `DeterministicGrader` 定义成“事实正确性层”，再往上增加“语义质量层”和“轨迹行为层”，最后用 hard gates + partial credit + multi-trial 聚合。**

这套架构已经非常接近现在 Anthropic/OpenAI 公开出来的成熟 Agent Eval 思路。([Anthropic][1])

**所以我给你的最终决策是：现在不要再纠结 `grader.json` 要不要保存那 7 个权重。直接把当前 `DeterministicGrader` 封住，作为 V1 correctness grader；下一步优先新增 `TrajectoryGrader + LLM SemanticGrader + CompositeGrader`，而不是继续给 DeterministicGrader 堆规则。**

[1]: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents "Demystifying evals for AI agents \ Anthropic"
[2]: https://platform.openai.com/docs/api-reference/graders?api-mode=chat "Graders | OpenAI API Reference"
[3]: https://platform.openai.com/docs/api-reference/graders?api-mode=chat&utm_source=chatgpt.com "Graders | OpenAI API Reference"
[4]: https://arxiv.org/abs/2609.00038?utm_source=chatgpt.com "trajectory-judge: What Outcome-Only LLM Judges Miss on Agent Trajectories"