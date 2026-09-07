from __future__ import annotations

from typing import Any

from pydantic import Field

from trace_agent.contract.task import TaskContract
from trace_agent.models import StrictModel


class CheckResult(StrictModel):
    code: str = Field(min_length=1)
    passed: bool
    message: str = ""


class FactGradeReport(StrictModel):
    case_id: str
    passed: bool
    total: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    checks: list[CheckResult] = Field(default_factory=list)


class EvalCase(StrictModel):
    """Minimal golden case shape consumed by :class:`EvalHarness`."""

    case_id: str = Field(min_length=1)
    task: TaskContract | None = None
    expected: dict[str, Any] = Field(default_factory=dict)


class GradeResult(StrictModel):
    score: float = Field(ge=0, le=1)
    passed: bool
    notes: list[str] = Field(default_factory=list)


class EvalResult(StrictModel):
    case_id: str
    status: str
    grade: GradeResult | None = None
    error: str | None = None
