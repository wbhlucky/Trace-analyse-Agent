# 冷启动回归 case：cold_start_001

这是一个「Case / Gold Oracle / Grader / Trial」分离的完整可测试 case，
金标已与仓库内一次实际分析结果对齐：

```text
results/analyze-cold-start-hiprofiler_data_new-431d6a/findings.json
```

输入 trace 使用 `evals/fixtures/current.db`（同一份 htrace 经 `trace_streamer` 转换后的 SQLite）。

## 文件说明

- `case.yaml`：题面（Prompt + 输入环境）与金标真值（Gold Oracle）。
- `grader.json`：确定性评分权重、hard gates、轨迹约束与 LLM rubric。
- `trials/trial-001/trial.json`：标准 trial 记录（示例，不参与确定性校验）。
- 本 README：数据来源、关键证据与可直接测试的运行方式。

## 权威金标数据（来自 findings.json）

| 指标 | 值 | 说明 |
| --- | --- | --- |
| `total_duration_ms` | 4038.739 | spawn(68.6708s) → 稳定帧(72.7095s) |
| `presentation_duration_ms` | 1549.428 | spawn → mapped render-service frame end(70.2202s) |
| `launch_ability_duration_ms` | 959.151 | LaunchAbility 根切片 |
| `entry_ability_eval_duration_ms` | 858.8 | ExecuteModuleBufferSecure EntryAbility.abc |
| `wx_ability_stage_eval_duration_ms` | 90.7 | WXAbilityStage.abc |
| `ability_transaction_duration_ms` | 324.8 | OnStart/OnForeground 至窗口首帧 |
| `app_component_load_duration_ms` | 2564.102 | 70.1406 → 72.7047s |
| `app_component_load_running_ms` | 1969.077 | 该阶段主线程 Running |

### 边界（Gold）

- 起点：`AppSpawnExecuteClearEnvHook` `ts=68670797385ns` = **68.6708s**。
- 呈现终点：`mapped render-service frame end` `ts=70220224989ns` = **70.2202s**。
  事件：微信首帧 `frame_slice:198`(src) 经 `frame_maps` 映射到
  `render_service`(`dst_row=200`, `dst_ts=70218201030ns`，实际呈现结束时间按
  `presentation_boundary` 取 70220224989ns)。
- 稳定帧终点：`frame_slice:328` `ts=72709535926ns` = **72.7095s**。

### 主导根因（Gold）

- RC001 `sync_module_evaluation_on_main_thread`：LaunchAbility 阶段主线程同步求值
  EntryAbility.abc(858.8ms) / WXAbilityStage.abc(90.7ms)，cpu-bound（Running 85.0%）。
- RC002 `app_component_load_cpu_bound_on_main_thread`：APP_COMPONENT_LOAD 区域
  2564.1ms，主线程 Running 76.8%，cpu-bound；期间 RS 呈现中断约 1.19s。

### uncertainty

- `app_startup` 表存在但 0 行，无 SmartPerf 启动阶段标签，阶段由 callstack/帧序列重建。
- 无启动点击/输入事件与业务冷启动完成 Marker，完成语义为 `suspected`。
- 无 perf-samples/baseline，不做 Perf 关联与基线对比。

## 「4926ms」与「6.475s」的说明

你提供的两个数值我放在 `case.yaml` 的 `gold.regression_target` 中，**默认不参与确定性评分**：

- `cold_start_start_ms: 4926`：与 findings 的 `total_duration_ms=4038.7ms` 不一致，
  可能是你的回归目标口径，等你确认后再启用。
- `cold_start_human_duration_ms: 6475`：你写的“t0 后 6.475s”。本库 t0=68.6708s，
  终点 70.2202s，实际 `presentation_duration_ms=1549.4ms`；6.475s 对应的是另一份
  t0 口径（t0≈63.745s），需重跑后对齐。

## 如何直接测试

1. 确认输入存在：

   ```powershell
   Test-Path ".\evals\fixtures\current.db"
   ```

2. 跑一次真实分析（aiquest 同源数据）：

   ```powershell
   .\.venv\Scripts\diting-agent.exe analyze `
     ".\evals\fixtures\current.db" `
     --type cold-start `
     --scenario "微信应用冷启动过慢" `
     --symptom "点开后响应半天才完全启动" `
     --target-process ".tencent.wechat" `
     --agent qoder --provider deepseek `
     --output ".\results\cold_start_001"
   ```

3. 分析完成后读取 `results/cold_start_001/findings.json`，将
   `resolved_process / boundaries / metrics / root_causes` 归一化为 trial 后，
   按 `grader.json` 评分。

如果你希望我把「4926ms / 6475ms」直接作为确定性金标，请告诉我这两个值的
t0 定义；当前版本先保留为待确认的回归目标，避免把正确结果误判为错误。

## Trials（本 case 已内置 3 个）

- `trials/trial-001/trial.json`：PASS（score 0.93）
- `trials/trial-002/trial.json`：PARTIAL（score 0.72，METRIC_ERROR）
- `trials/trial-003/trial.json`：FAIL（score 0.46，WRONG_ROOT_CAUSE）

聚合见 `result.json`：pass@1 = 1/3，mean_score ≈ 0.703。
