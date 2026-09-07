样例：冷启动评测（Cold Start Demo）

这是按「Case / Gold Oracle / Trial / Grader / Result」分离方案写的结构化样例。

## 一句话关系

- **Case**：要测什么（题目本身）
- **Gold Oracle**：正确答案的结构化真值（属于 Case）
- **Trial**：Agent 实际跑的一次（同一 Case 跑多次）
- **Grader**：怎么打分
- **Result**：这个 Case 上 Agent 的综合表现

## 目录结构

```
cold-start-demo/
├── case.yaml              # Case + Gold Oracle（Gold 属于 Case）
├── grader.json            # 评分规则（阈值 / 权重 / 失败分类）
├── result.json            # 3 个 Trial 的汇总结果
├── fixtures/
│   ├── output.db          # 输入 trace 的最小化演示 SQLite（真实评测替换此文件）
│   └── README.md          # fixture 说明
└── trials/
    ├── trial-001/trial.json   # PASS   score=0.93
    ├── trial-002/trial.json   # FAIL   score=0.51 WRONG_ROOT_CAUSE
    └── trial-003/trial.json   # PARTIAL score=0.78 EVIDENCE_ERROR
```

## 关键设计点

1. **Gold 只写一次**：在 `case.yaml` 的 `gold:` 里，不在每个 Trial 里重复。
2. **Trial 保存完整轨迹**：`trajectory.tool_calls`、`final_answer`、`root_cause`、`metrics`，
   便于后续区分「选错工具 / 查错数据 / 正确证据但错误归因」等失败类型。
3. **双通道评分**：`grader.json` 里既有确定性评分（root_cause/evidence/metrics），
   也有 LLM 评分（reasoning/evidence_grounding/report_quality）。
4. **一个 Case 默认跑 3 个 Trial**；CI 回归可只跑 1 次。

## 推荐 V1 规模

```
40 Cases × 3 Trials = 120 次 Agent 执行
Gold Oracle 只有 40 份（Case 级）
```
