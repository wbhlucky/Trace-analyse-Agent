from __future__ import annotations

from pathlib import Path

from trace_agent.contract.result import (
    AnalysisContractResult,
    ContractEvidence,
    ContractFinding,
)
from trace_agent.contract.task import TaskContract
from trace_agent.models import ScenarioType
from trace_agent.verification import (
    DeterministicResultVerifier,
    VerificationStatus,
)


def _task() -> TaskContract:
    return TaskContract(
        task_id="verify",
        trace_id="verify",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="scenario",
        symptom="symptom",
        output_dir=Path("."),
    )


def _verifier() -> DeterministicResultVerifier:
    return DeterministicResultVerifier()


def test_pass_when_structure_and_evidence_are_complete() -> None:
    result = AnalysisContractResult(
        conclusion="conclusion",
        findings=[
            ContractFinding(
                category="cpu",
                claim="main thread ran long",
                evidence_ids=["ev-1"],
            )
        ],
        evidence=[
            ContractEvidence(
                evidence_id="ev-1",
                source="tool",
                description="schedule slice",
            )
        ],
    )
    verification = _verifier().verify(_task(), result)
    assert verification.status is VerificationStatus.PASS


def test_fail_when_conclusion_missing() -> None:
    result = AnalysisContractResult(conclusion="")
    verification = _verifier().verify(_task(), result)
    assert verification.status is VerificationStatus.FAIL
    assert any(issue.severity == "error" for issue in verification.issues)


def test_repair_when_finding_references_unknown_evidence() -> None:
    result = AnalysisContractResult(
        conclusion="conclusion",
        findings=[
            ContractFinding(
                category="cpu",
                claim="claim",
                evidence_ids=["ev-missing"],
            )
        ],
        evidence=[
            ContractEvidence(
                evidence_id="ev-1",
                source="tool",
                description="schedule slice",
            )
        ],
    )
    verification = _verifier().verify(_task(), result)
    assert verification.status is VerificationStatus.REPAIR
    assert any(
        issue.code == "finding_unknown_evidence"
        for issue in verification.issues
    )
