from __future__ import annotations

from uuid import uuid4

from trace_agent.models import AnalyzeRequest, AnalysisResult, Finding
from trace_agent.memory.models import EpisodicMemory


def build_episode(
    request: AnalyzeRequest,
    analysis: AnalysisResult,
    *,
    run_id: str,
    output_dir: str | None = None,
    status: str = "completed",
) -> EpisodicMemory:
    """Project one completed analysis into a searchable memory episode.

    Only structured fields are copied. Raw evidence remains in the result
    directory and is referenced through the stored output path.
    """
    findings: list[dict] = []
    for finding in analysis.findings:
        findings.append(
            {
                "title": finding.title,
                "severity": finding.severity.value,
                "status": finding.status.value,
                "confidence": finding.confidence,
                "analysis": finding.analysis,
                "recommendation": finding.recommendation,
                "evidence_ids": list(finding.evidence_ids),
            }
        )

    root_causes: list[str] = []
    recommendations: list[str] = []
    for finding in analysis.findings:
        if finding.status.value in {"confirmed", "observed"}:
            root_causes.append(f"{finding.title}: {finding.analysis}")
        if finding.recommendation:
            recommendations.append(finding.recommendation)

    tags: list[str] = []
    if request.device:
        tags.append(f"device:{request.device}")
    if request.build:
        tags.append(f"build:{request.build}")
    if request.target_process:
        tags.append(f"process:{request.target_process}")

    return EpisodicMemory(
        episode_id=f"ep-{uuid4().hex[:12]}",
        run_id=run_id,
        trace_id=request.trace_id,
        scenario_type=request.scenario_type.value,
        scenario=request.scenario,
        symptom=request.symptom,
        device=request.device,
        build=request.build,
        target_process=request.target_process,
        summary=analysis.summary,
        root_causes=root_causes,
        recommendations=recommendations,
        findings=findings,
        tags=tags,
        status=status,
        output_dir=output_dir,
    )
