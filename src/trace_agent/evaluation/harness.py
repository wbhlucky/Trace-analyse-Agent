from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import TypeAdapter

from trace_agent.contract.result import AnalysisContractResult
from trace_agent.contract.task import TaskContract
from trace_agent.evaluation.grader import FactGrader, Grader
from trace_agent.evaluation.models import (
    EvalCase,
    EvalResult,
    FactGradeReport,
    GradeResult,
)
from trace_agent.models import AnalysisResult
from trace_agent.verification import DeterministicResultVerifier


class EvalHarness:
    """Locates golden cases on disk and grades them deterministically.

    Real golden data is intentionally deferred; discovering an empty ``root``
    returns an empty report list so the CLI remains runnable.
    """

    def __init__(self, grader: FactGrader | None = None) -> None:
        self._grader = grader or FactGrader()

    def run(self, root: Path) -> list[FactGradeReport]:
        reports: list[FactGradeReport] = []
        for case_dir in _case_dirs(root):
            case_id = case_dir.name
            expected = _load_json(case_dir / "expected.json")
            result = _load_result(case_dir)
            reports.append(self._grader.grade(case_id, result, expected))
        return reports

    @staticmethod
    def summary(reports: list[FactGradeReport]) -> dict[str, Any]:
        total = len(reports)
        passed_cases = sum(1 for report in reports if report.passed)
        failed_cases = total - passed_cases
        pass_rate = (passed_cases / total) if total else 0.0
        return {
            "total_cases": total,
            "passed_cases": passed_cases,
            "failed_cases": failed_cases,
            "pass_rate": pass_rate,
        }


def _case_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        path
        for path in sorted(root.iterdir())
        if path.is_dir() and (path / "expected.json").is_file()
    ]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_result(case_dir: Path) -> AnalysisResult:
    payload = _load_json(case_dir / "result.json")
    try:
        return TypeAdapter(AnalysisResult).validate_python(payload)
    except Exception:
        return AnalysisResult(summary="", findings=[], limitations=[])


RunOutput = Awaitable[AnalysisContractResult]
CaseRunner = Callable[[TaskContract], RunOutput]


class EvaluationHarness:
    """Async single-case runner through runner, verifier, and grader.

    This is the provider-agnostic harness reserved for the next phase; it is
    kept here so the runtime boundary is already exercised by unit tests.
    """

    def __init__(
        self,
        *,
        runner: CaseRunner,
        verifier: DeterministicResultVerifier,
        grader: Grader | None = None,
    ) -> None:
        self._runner = runner
        self._verifier = verifier
        self._grader = grader

    async def run_case(self, case: EvalCase) -> EvalResult:
        if case.task is None:
            return EvalResult(
                case_id=case.case_id,
                status="error",
                error="missing task",
            )
        try:
            result = await self._runner(case.task)
        except Exception as exc:  # noqa: BLE001 - eval errors are data
            return EvalResult(
                case_id=case.case_id,
                status="error",
                error=str(exc),
            )

        verification = self._verifier.verify(case.task, result)
        if not verification.passed:
            return EvalResult(
                case_id=case.case_id,
                status=verification.status.value,
                error=(
                    verification.issues[0].code
                    if verification.issues
                    else "verification failed"
                ),
            )
        if self._grader is None:
            return EvalResult(case_id=case.case_id, status="pass")

        grade = self._grader.grade(case.expected, _result_payload(result))
        return EvalResult(
            case_id=case.case_id,
            status="pass",
            grade=grade,
        )


def _result_payload(result: AnalysisContractResult) -> dict[str, Any]:
    return result.model_dump()
