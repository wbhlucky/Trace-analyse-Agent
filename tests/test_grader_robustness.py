"""Grader robustness / adversarial tests for ``cold_start_001``.

These tests validate that the deterministic outcome grader judges concepts
(canonical IDs + aliases) rather than literal Gold wording, and that its
diagnosis/evidence/metrics/uncertainty dimensions stay independent.

Known boundary: the Level-2 alias matcher is substring-based, so it can still
report a false positive when a Gold keyword appears inside an otherwise wrong
classification.  That case is marked ``xfail`` until structured output
(Level 1) or an LLM judge (Level 3) replaces the fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trace_agent.eval import DeterministicGrader, load_case
from trace_agent.eval.models import (
    AgentOutput,
    BottleneckGold,
    EvidenceGold,
    Gold,
    MetricGold,
    RootCauseGold,
)
from trace_agent.models import (
    AnalysisResult,
    ColdStartAnalysis,
    Finding,
    FindingSeverity,
    FindingStatus,
    ResolvedProcess,
    TraceBoundary,
)


def _finding(
    title: str,
    analysis: str,
    *,
    evidence_ids: list[str] | None = None,
    recommendation: str = "recommend",
    verification: str = "verify",
) -> Finding:
    return Finding(
        title=title,
        severity=FindingSeverity.HIGH,
        status=FindingStatus.CONFIRMED,
        confidence=0.9,
        evidence_ids=evidence_ids or [],
        analysis=analysis,
        recommendation=recommendation,
        verification=verification,
    )


def _analysis(
    summary: str,
    findings: list[Finding],
    limitations: list[str] | None = None,
    cold_start: ColdStartAnalysis | None = None,
) -> AnalysisResult:
    return AnalysisResult(
        summary=summary,
        cold_start=cold_start,
        completion_latency=None,
        perf=None,
        findings=findings,
        limitations=limitations or [],
    )


def _graded(gold: Gold, result: AnalysisResult):
    output = AgentOutput(result=result, raw={"tool_calls": []})
    grade = DeterministicGrader().grade("cold_start_001", "trial", gold, output)
    return {item.component: item.score for item in grade.components}


def _cold_start(
    total_duration_ms: float,
    presentation_duration_ms: float | None = None,
) -> ColdStartAnalysis:
    return ColdStartAnalysis(
        resolved_process=ResolvedProcess(
            name=".tencent.wechat",
            pid=11550,
            ipid=236,
            selection_reason="test",
            confidence=0.9,
        ),
        cold_start_proven=True,
        classification_reason="cold start",
        start_boundary=TraceBoundary(
            name="start",
            timestamp_ns=0,
            source="callstack",
            confidence=0.9,
        ),
        end_boundary=TraceBoundary(
            name="end",
            timestamp_ns=int(total_duration_ms * 1_000_000),
            source="callstack",
            confidence=0.9,
        ),
        total_duration_ms=total_duration_ms,
        presentation_duration_ms=presentation_duration_ms,
        critical_path_summary="critical path",
    )


def _loaded_gold() -> Gold:
    return load_case(Path("evals/cases/cold_start_001/case.yaml")).gold


def test_root_cause_correct_with_different_wording() -> None:
    # 1) root cause correct with different wording, no canonical literal.
    gold = _loaded_gold()
    result = _analysis(
        summary="微信启动慢，主线程在 LaunchAbility 阶段串行加载并求值了大量业务模块",
        findings=[
            _finding(
                "启动阶段主线程串行求值业务模块导致耗时增加",
                "主线程在 LaunchAbility 阶段同步执行模块求值，占用 CPU",
            )
        ],
    )
    assert _graded(gold, result)["diagnosis"] >= 0.5


def test_bottleneck_correct_without_canonical_literal() -> None:
    # 2) bottleneck correct without canonical id appearing verbatim.
    gold = _loaded_gold()
    result = _analysis(
        summary="首帧前 LaunchAbility 阶段是主要瓶颈",
        findings=[
            _finding(
                "LaunchAbility 阶段占最大比重",
                "LaunchAbility 阶段主线程持续 Running，属于同步执行而非等待",
            )
        ],
    )
    assert _graded(gold, result)["diagnosis"] >= 0.5


@pytest.mark.xfail(
    reason=(
        "Level-2 substring matcher cannot distinguish a Gold keyword "
        "inside an otherwise wrong classification; needs Level 1 structured "
        "output or Level 3 LLM judge"
    ),
    strict=True,
)
def test_wrong_classification_with_keyword_does_not_pass() -> None:
    # 3) wrong classification containing a Gold keyword must not PASS.
    gold = _loaded_gold()
    result = _analysis(
        summary="根因是 GPU 饱和。虽然也观察到 EntryAbility.abc 同步求值，但只是伴随现象。",
        findings=[
            _finding(
                "GPU 饱和是主要根因",
                "真正瓶颈是 GPU 资源耗尽；EntryAbility.abc 的同步求值并非首因",
            )
        ],
    )
    assert _graded(gold, result)["diagnosis"] < 0.5


def test_partial_root_cause_scores_partial() -> None:
    # 4) partial credit when only one of several root causes is found.
    gold = Gold(
        root_causes=[
            RootCauseGold(id="RC1", canonical="cause_a", aliases=["线索 A"]),
            RootCauseGold(id="RC2", canonical="cause_b", aliases=["线索 B"]),
        ]
    )
    result = _analysis(
        summary="只找到了线索 A",
        findings=[_finding("线索 A", "命中了第一个根因")],
    )
    score = _graded(gold, result)["diagnosis"]
    assert 0.0 < score < 1.0


def test_uncertainty_topic_different_wording() -> None:
    # 5) uncertainty expressed differently still matches the topic.
    gold = _loaded_gold()
    result = _analysis(
        summary="冷启动慢",
        findings=[_finding("主线程同步求值", "EntryAbility.abc 求值")],
        limitations=[
            "Trace 内无用户点击 Marker，起点只能取进程创建近似",
            "本次无 baseline，无法判断相对回归",
        ],
    )
    assert _graded(gold, result)["uncertainty"] >= 0.5


def test_missing_evidence_lowers_score() -> None:
    # 6) correct diagnosis but missing required evidence lowers evidence.
    gold = _loaded_gold()
    result = _analysis(
        summary="主线程 LaunchAbility 同步求值导致启动慢",
        findings=[
            _finding(
                "LaunchAbility 同步求值",
                "EntryAbility.abc 求值占用主线程",
                evidence_ids=["ev-other"],
            )
        ],
    )
    scores = _graded(gold, result)
    assert scores["diagnosis"] >= 0.5
    assert scores["evidence"] < 0.5


def test_metric_correct_but_diagnosis_wrong() -> None:
    # 7) metric values correct while diagnosis is wrong: dimensions independent.
    gold = _loaded_gold()
    result = _analysis(
        summary="启动慢",
        findings=[_finding("GPU 饱和", "根因是 GPU")],
        cold_start=_cold_start(1533.675, 1549.428),
    )
    scores = _graded(gold, result)
    assert scores["metrics"] >= 0.99
    assert scores["diagnosis"] < 0.5


def test_diagnosis_correct_but_evidence_wrong() -> None:
    # 8) diagnosis correct while evidence is wrong: dimensions independent.
    gold = _loaded_gold()
    result = _analysis(
        summary="主线程 LaunchAbility 同步求值导致启动慢",
        findings=[
            _finding(
                "LaunchAbility 同步求值",
                "EntryAbility.abc 求值占用主线程",
                evidence_ids=[],
            )
        ],
        cold_start=_cold_start(1533.675, 1549.428),
    )
    scores = _graded(gold, result)
    assert scores["diagnosis"] >= 0.5
    assert scores["evidence"] < 0.5