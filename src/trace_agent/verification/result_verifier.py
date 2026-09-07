from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from trace_agent.contract.result import AnalysisContractResult
from trace_agent.contract.task import TaskContract
from trace_agent.models import StrictModel


class VerificationStatus(StrEnum):
    PASS = "pass"
    REPAIR = "repair"
    FAIL = "fail"


class VerificationIssue(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    severity: str


class VerificationResult(StrictModel):
    status: VerificationStatus
    issues: list[VerificationIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASS


class DeterministicResultVerifier:
    """Deterministic, rule-based gate over normalized model output.

    This is deliberately not an LLM judge: it only encodes what a program can
    prove about structural integrity and evidence linkage. A real LLM judge can
    be layered on top later without changing this boundary.
    """

    def verify(
        self,
        task: TaskContract,
        result: AnalysisContractResult,
    ) -> VerificationResult:
        del task  # reserved for future scenario-aware constraints
        issues = self._collect_issues(result)
        if not issues:
            return VerificationResult(status=VerificationStatus.PASS)

        status = VerificationStatus.REPAIR
        for issue in issues:
            if issue.severity == "error":
                status = VerificationStatus.FAIL
                break
        return VerificationResult(status=status, issues=issues)

    def _collect_issues(
        self,
        result: AnalysisContractResult,
    ) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []

        issues.extend(self._check_structure(result))
        issues.extend(self._check_evidence_integrity(result))
        issues.extend(self._check_reference_integrity(result))

        return issues

    @staticmethod
    def _check_structure(
        result: AnalysisContractResult,
    ) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []
        if not result.conclusion:
            issues.append(
                VerificationIssue(
                    code="missing_conclusion",
                    message="AnalysisResult 缺少 conclusion",
                    severity="error",
                )
            )
        if not result.findings:
            issues.append(
                VerificationIssue(
                    code="missing_findings",
                    message="AnalysisResult 缺少 findings",
                    severity="error",
                )
            )
        if not result.evidence:
            issues.append(
                VerificationIssue(
                    code="missing_evidence",
                    message="AnalysisResult 缺少 evidence",
                    severity="error",
                )
            )
        return issues

    @staticmethod
    def _check_evidence_integrity(
        result: AnalysisContractResult,
    ) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []
        seen: set[str] = set()
        for item in result.evidence:
            if not item.evidence_id:
                issues.append(
                    VerificationIssue(
                        code="evidence_missing_id",
                        message="Evidence 缺少 evidence_id",
                        severity="repair",
                    )
                )
                continue
            if item.evidence_id in seen:
                issues.append(
                    VerificationIssue(
                        code="evidence_duplicate_id",
                        message=(
                            f"Evidence {item.evidence_id} 出现重复"
                        ),
                        evidence_ids=[item.evidence_id],
                        severity="repair",
                    )
                )
            seen.add(item.evidence_id)
            if not item.source:
                issues.append(
                    VerificationIssue(
                        code="evidence_missing_source",
                        message=(
                            f"Evidence {item.evidence_id} 缺少 source"
                        ),
                        evidence_ids=[item.evidence_id],
                        severity="repair",
                    )
                )
            if not item.description:
                issues.append(
                    VerificationIssue(
                        code="evidence_missing_description",
                        message=(
                            f"Evidence {item.evidence_id} 缺少 description"
                        ),
                        evidence_ids=[item.evidence_id],
                        severity="repair",
                    )
                )
            if item.start is not None and item.end is not None:
                if item.start > item.end:
                    issues.append(
                        VerificationIssue(
                            code="evidence_invalid_range",
                            message=(
                                f"Evidence {item.evidence_id} start 大于 end"
                            ),
                            evidence_ids=[item.evidence_id],
                            severity="repair",
                        )
                    )
        return issues

    @staticmethod
    def _check_reference_integrity(
        result: AnalysisContractResult,
    ) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []
        known = {item.evidence_id for item in result.evidence}
        for finding in result.findings:
            if not finding.evidence_ids:
                issues.append(
                    VerificationIssue(
                        code="finding_without_evidence",
                        message=(
                            f"Finding {finding.category!r} 缺少 evidence_ids"
                        ),
                        severity="repair",
                    )
                )
                continue
            for evidence_id in finding.evidence_ids:
                if evidence_id not in known:
                    issues.append(
                        VerificationIssue(
                            code="finding_unknown_evidence",
                            message=(
                                f"Finding {finding.category!r} 引用了"
                                f"不存在的 Evidence {evidence_id}"
                            ),
                            evidence_ids=[evidence_id],
                            severity="repair",
                        )
                    )
        return issues
