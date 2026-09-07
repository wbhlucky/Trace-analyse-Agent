from __future__ import annotations

from typing import Any, Protocol

from trace_agent.evaluation.models import (
    CheckResult,
    FactGradeReport,
    GradeResult,
)
from trace_agent.models import AnalysisResult


class Grader(Protocol):
    """Deterministic scoring boundary between expected and actual outputs."""

    def grade(self, expected: Any, actual: Any) -> GradeResult:
        ...


class PassFailGrader:
    """Skeleton equality grader; kept minimal for interface parity."""

    def grade(self, expected: Any, actual: Any) -> GradeResult:
        passed = expected == actual
        return GradeResult(
            score=1.0 if passed else 0.0,
            passed=passed,
            notes=[] if passed else ["expected and actual differ"],
        )


class FactGrader:
    """Rule-based, fact-level grader compatible with golden cases.

    Checks are intentionally deterministic and judgment-free: fact presence,
    evidence linkage, allowed/finding status, forbidden terms, and numeric
    tolerance.  No LLM is involved.
    """

    def grade(
        self,
        case_id: str,
        result: AnalysisResult,
        expected: dict[str, Any],
    ) -> FactGradeReport:
        checks: list[CheckResult] = []

        checks.extend(self._check_facts(expected.get("facts", []), result))
        checks.extend(
            self._check_evidence(expected.get("required_evidence", []), result)
        )
        checks.extend(self._check_allowed(expected.get("allowed", []), result))
        checks.extend(
            self._check_forbidden(expected.get("forbidden", []), result)
        )
        checks.extend(
            self._check_numeric(expected.get("numeric_tolerance", []), result)
        )

        passed_count = sum(1 for check in checks if check.passed)
        return FactGradeReport(
            case_id=case_id,
            passed=passed_count == len(checks),
            total=len(checks),
            passed_count=passed_count,
            checks=checks,
        )

    @staticmethod
    def _check_facts(
        facts: list[Any],
        result: AnalysisResult,
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []
        haystack = _searchable_text(result)
        for index, fact in enumerate(facts):
            value = str(fact)
            passed = value in haystack
            checks.append(
                CheckResult(
                    code=f"facts.{index}",
                    passed=passed,
                    message="" if passed else f"missing fact {value!r}",
                )
            )
        return checks

    @staticmethod
    def _check_evidence(
        required: list[Any],
        result: AnalysisResult,
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []
        known = {
            evidence_id
            for finding in result.findings
            for evidence_id in finding.evidence_ids
        }
        for index, evidence_id in enumerate(required):
            passed = str(evidence_id) in known
            checks.append(
                CheckResult(
                    code=f"required_evidence.{index}",
                    passed=passed,
                    message=(
                        "" if passed else f"missing evidence {evidence_id!r}"
                    ),
                )
            )
        return checks

    @staticmethod
    def _check_allowed(
        allowed: list[Any],
        result: AnalysisResult,
    ) -> list[CheckResult]:
        if not allowed:
            return []
        statuses = {finding.status.value for finding in result.findings}
        passed = any(str(item) in statuses for item in allowed)
        return [
            CheckResult(
                code="allowed",
                passed=passed,
                message=(
                    "" if passed else "no finding matches allowed statuses"
                ),
            )
        ]

    @staticmethod
    def _check_forbidden(
        forbidden: list[Any],
        result: AnalysisResult,
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []
        haystack = _searchable_text(result)
        for index, term in enumerate(forbidden):
            value = str(term)
            passed = value not in haystack
            checks.append(
                CheckResult(
                    code=f"forbidden.{index}",
                    passed=passed,
                    message="" if passed else f"forbidden term {value!r}",
                )
            )
        return checks

    @staticmethod
    def _check_numeric(
        specs: list[Any],
        result: AnalysisResult,
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []
        for index, spec in enumerate(specs):
            if not isinstance(spec, dict):
                checks.append(
                    CheckResult(
                        code=f"numeric_tolerance.{index}",
                        passed=False,
                        message="invalid spec",
                    )
                )
                continue
            path = spec.get("path", "")
            expected_value = spec.get("value", 0)
            tolerance = spec.get("tolerance", 0)
            actual = _navigate(result, str(path))
            if isinstance(actual, (int, float)):
                passed = abs(actual - expected_value) <= tolerance
                message = (
                    ""
                    if passed
                    else (
                        f"{path} value {actual} outside "
                        f"{expected_value}+/-{tolerance}"
                    )
                )
            else:
                passed = False
                message = f"{path} missing or non-numeric"
            checks.append(
                CheckResult(
                    code=f"numeric_tolerance.{index}",
                    passed=passed,
                    message=message,
                )
            )
        return checks


def _searchable_text(result: AnalysisResult) -> str:
    parts = [result.summary] if result.summary else []
    parts.extend(
        f"{finding.title} {finding.analysis}"
        for finding in result.findings
    )
    return " ".join(parts)


def _navigate(result: AnalysisResult, path: str) -> Any:
    current: Any = result
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return None
    return current
