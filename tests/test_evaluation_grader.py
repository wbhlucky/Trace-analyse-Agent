from __future__ import annotations

from trace_agent.evaluation import FactGrader
from trace_agent.models import AnalysisResult, Finding


def _result() -> AnalysisResult:
    return AnalysisResult(
        summary="CPU bound on main thread in slice 1000",
        findings=[
            Finding(
                title="CPU 竞争导致卡顿",
                severity="medium",
                status="confirmed",
                confidence=0.9,
                evidence_ids=["ev-1"],
                analysis="主线程在 1000ms 内持续运行且存在 CPU 竞争",
                recommendation="隔离关键线程",
                verification="通过 schedule slice 验证",
            )
        ],
        limitations=[],
    )


def test_grader_passes_when_facts_and_evidence_match() -> None:
    report = FactGrader().grade(
        "case-001",
        _result(),
        {
            "facts": ["CPU", "1000ms"],
            "required_evidence": ["ev-1"],
            "allowed": ["confirmed"],
            "forbidden": ["GPU 显存耗尽"],
        },
    )
    assert report.passed is True
    assert report.total == 5
    assert report.passed_count == 5


def test_grader_fails_on_missing_fact() -> None:
    report = FactGrader().grade(
        "case-002",
        _result(),
        {"facts": ["都不应该出现的事实"]},
    )
    assert report.passed is False
    assert any(
        check.code == "facts.0" and not check.passed
        for check in report.checks
    )


def test_grader_fails_on_forbidden_conclusion() -> None:
    report = FactGrader().grade(
        "case-003",
        _result(),
        {"forbidden": ["GPU 显存耗尽"]},
    )
    assert report.passed is True


def test_grader_checks_numeric_tolerance() -> None:
    result = _result()
    report = FactGrader().grade(
        "case-004",
        result,
        {
            "numeric_tolerance": [
                {"path": "missing_field", "value": 0, "tolerance": 0}
            ]
        },
    )
    assert any(
        check.code == "numeric_tolerance.0" and not check.passed
        for check in report.checks
    )
