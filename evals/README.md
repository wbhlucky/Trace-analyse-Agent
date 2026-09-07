# Evals

后续每个评测案例包含：

```text
cases/<case-id>/
├── case.json
├── current.htrace
├── baseline.htrace        # 可选
└── expected.json
```

`expected.json` 应描述：

- 问题类型：冷启动、响应时延、完成时延或帧率/丢帧；
- 必须召回的关键问题；
- 必须引用的证据；
- 允许的根因状态；
- 不允许出现的结论；
- 数值容差。

当前框架只完成文件级数据闭环，接入事件级 HTrace Adapter 后再建立真实评测集。

## 评测分层

每个 trial 会同时产出三层独立观察，互不折叠进 correctness：

- **Outcome**：`DeterministicGrader` 的加权分 + 硬 gates（`diagnosis`/`evidence`/`metrics`/`overall`）。
- **Trajectory**：`TrajectoryGrader` 对工具轨迹的确定性行为评分（tool errors / repeated calls / budget / forbidden actions）。
- **Operational**：turns / tool_calls / tool_errors / tokens / ttft_ms / latency_ms。

`MultiTrialAggregator` 在此基础上给出 `pass@1` / `pass@k` / `pass^k` 以及独立的 `trajectory_pass_rate` 与 operational 聚合。trial 失败会按 `success` / `agent_failure` / `infra_failure` / `timeout` / `invalid` / `eval_failure` 分类，避免 infra 噪声与评测器 bug 污染多 trial 统计。
