from __future__ import annotations

from typing import Any

from pydantic import Field

from trace_agent.models import (
    AnalysisResult,
    EvidenceRecord,
    Finding as DomainFinding,
    StrictModel,
)


class ContractFinding(StrictModel):
    """Verifier-facing finding with an explicit evidence reference list."""

    category: str
    claim: str
    severity: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_domain(cls, finding: DomainFinding) -> "ContractFinding":
        return cls(
            category=finding.title,
            claim=finding.analysis,
            severity=finding.severity.value,
            evidence_ids=list(finding.evidence_ids),
        )


class ContractEvidence(StrictModel):
    """Verifier-facing evidence record."""

    evidence_id: str
    source: str
    description: str
    start: float | None = None
    end: float | None = None

    @classmethod
    def from_domain(cls, evidence: EvidenceRecord) -> "ContractEvidence":
        start = evidence.data.get("start_timestamp_ms")
        end = evidence.data.get("end_timestamp_ms")
        return cls(
            evidence_id=evidence.evidence_id,
            source=evidence.tool,
            description=evidence.summary,
            start=float(start) if isinstance(start, (int, float)) else None,
            end=float(end) if isinstance(end, (int, float)) else None,
        )


class AnalysisContractResult(StrictModel):
    """Normalized model-agent output accepted by :class:`ResultVerifier`.

    It is intentionally smaller than the domain ``AnalysisResult``: the
    verifier only needs conclusions, claims, and the evidence that backs them.
    """

    conclusion: str
    findings: list[ContractFinding] = Field(default_factory=list)
    evidence: list[ContractEvidence] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_domain(
        cls,
        result: AnalysisResult,
        evidence: list[EvidenceRecord] | None = None,
    ) -> "AnalysisContractResult":
        return cls(
            conclusion=result.summary,
            findings=[
                ContractFinding.from_domain(finding)
                for finding in result.findings
            ],
            evidence=[
                ContractEvidence.from_domain(item)
                for item in (evidence or [])
            ],
        )
