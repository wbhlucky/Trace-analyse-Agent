DitingAgent 架构

本项目采用模块化单体架构。基础设施层可以实现领域端口（port），但领域事实和应用程序策略不得依赖于某个具体的模型提供商或 HTML 模板。

依赖方向

CLI（命令行接口）
  -> AnalyzeApplication（分析应用层）
      -> ScenarioCatalog（场景目录）
      -> TraceAdapter / ToolRegistry（追踪适配器 / 工具注册表）
      -> AnalysisAgent port（分析代理端口）
      -> EvidenceStore / EvidenceIndex（证据存储 / 证据索引）
      -> Normalizers / Validators（规范化器 / 校验器）
      -> ReportProjectionBuilder（报告投影构建器）
          -> ReportRenderer（报告渲染器）

Qoder Agent SDK -> AnalysisAgent 端口的实现
SQLite / TraceStreamer -> Trace 和仓储层的实现
Jinja / Canvas -> ReportRenderer 的实现

职责规则

确定性核心（Deterministic core）

转换 Trace 文件并检测各项能力（capabilities）。
执行有界的只读查询和计算。
记录不可变的证据（Evidence），并通过 EvidenceIndex 进行选取。
填充确定性字段并校验数值一致性。
将经过校验的分析和 Trace 事实投影为报告视图数据。

代理（Agent）

筛选具有语义意义的候选项。
决定下一步调用哪个可用工具。
关联 Trace、调度、帧和性能（Perf）证据。
生成解释、根因判断、置信度和建议。

Agent 不得重新生成确定性的线程、调度或性能分析数据。Provider 适配器不得自行持有场景证据的需求定义，必须委托给 EvidenceSubmissionPolicy（证据提交策略）。

技能（Skills）

描述分析方法和证据标准。
不得将特定于应用的标记名称硬编码为全局事实。
不得替代确定性的工具输入校验或结果校验。

展示层（Presentation）

ReportProjectionBuilder 可以读取经过校验的分析数据、证据和 Trace 数据库事实，以构建报告视图。
ReportRenderer 仅负责渲染传入的投影数据，绝不执行 SQL、选择关联线程或推导诊断结论。
最终报告可以是自包含的，即使源模板和资源文件是拆分的。

扩展规则

新增场景时：

在 ScenarioCatalog 中注册其 Skill、报告标题、结果字段和预检调用。
在新增场景专属工具之前，优先复用跨场景通用的进程、标记、帧、调度、唤醒和性能能力。
新增场景校验逻辑，但不修改 Agent 的 provider 适配器。
通过投影数据来添加报告章节，而非在模板侧进行分析。
在修改指标语义之前，先添加契约测试和特征测试。

新增工具（Tool）时：

返回确定性事实和局限性说明，而非 LLM 风格的结论。
声明所需的 Trace 能力和有界的调用预算。
将完整结果存储为 Evidence，当数据量较大时，仅向 Agent 暴露精简视图。
当多个消费者需要使用该结果时，添加类型化的 Evidence 查找接口。

后续可继续拆分的方向

上述边界已在生效中。后续的进一步重构现在可以机械化地推进：

拆分时间线、性能和启动模块的报告投影器；
拆分 CSS、HTML 章节、时间线 JS 和火焰图 JS，同时保持最终 HTML 自包含；
按能力维度拆分工具定义；
按场景拆分校验器和领域模型；
集中管理 SQLite 表/列的内省逻辑。

这些变更必须保持当前 SQL 语义和真实 Trace 结果不变。