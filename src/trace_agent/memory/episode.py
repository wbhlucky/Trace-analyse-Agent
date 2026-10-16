from __future__ import annotations

from uuid import uuid4

from trace_agent.models import AnalyzeRequest, AnalysisResult, Finding
from trace_agent.memory.flags import (
    eval_run_id as env_eval_run_id,
    memory_project_id,
    memory_session_id,
)
from trace_agent.memory.models import (
    CORROBORATED_FINDING_STATUSES,
    EpisodicMemory,
)


def build_episode(
    request: AnalyzeRequest,
    analysis: AnalysisResult,
    *,
    run_id: str,
    output_dir: str | None = None,
    status: str = "completed",
    eval_run_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
) -> EpisodicMemory:
    """Project one completed analysis into a searchable memory episode.

    Only structured fields are copied. Raw evidence remains in the result
    directory and is referenced through the stored output path.

    ``eval_run_id`` defaults to :func:`~trace_agent.memory.flags.eval_run_id`
    rather than to ``None``: the eval harness publishes its run id through the
    environment because that is the one channel reaching the application
    without adding an eval-only field to the production ``AnalyzeRequest``.
    Stamping it here is what makes "was this memory minted inside the same
    experiment that is now scoring it" answerable afterwards.

    ``session_id`` and ``project_id`` follow the same rule. The dashboard knows
    which conversation triggered a run and passes it explicitly; a bare CLI
    analysis has neither, and then the environment-derived fallbacks decide -
    ``memory_project_id`` reads the namespace, so the ordinary deployment still
    gets a project. Section 4 keeps both as ``episodes`` columns precisely so
    that "what did this session conclude" is a filter instead of a scan.
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
        if finding.status.value in CORROBORATED_FINDING_STATUSES:
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
        session_id=(
            session_id if session_id is not None else memory_session_id()
        ),
        project_id=(
            project_id if project_id is not None else memory_project_id()
        ),
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
        eval_run_id=(
            eval_run_id if eval_run_id is not None else env_eval_run_id()
        ),
    )


def apply_grade(
    episode: EpisodicMemory,
    *,
    graded_pass: bool,
    graded_score: float | None = None,
    eval_run_id: str | None = None,
) -> EpisodicMemory:
    """Return a copy of ``episode`` carrying one deterministic grade.

    Shared by both store adapters so the file and SQL layouts cannot drift on
    what a graded episode looks like. ``eval_run_id`` is only overwritten when
    supplied: a run projected during an eval already carries the id from
    :func:`build_episode`, and a later manual grading must not erase it.
    """
    return episode.model_copy(
        update={
            "graded_pass": bool(graded_pass),
            "graded_score": graded_score,
            "eval_run_id": eval_run_id or episode.eval_run_id,
        }
    )
