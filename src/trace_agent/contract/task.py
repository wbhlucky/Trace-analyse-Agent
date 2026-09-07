from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from trace_agent.models import (
    AnalyzeRequest,
    ScenarioType,
    StrictModel,
)


class TaskContract(StrictModel):
    """Provider-agnostic task boundary shared by CLI, Web, and future APIs.

    This mirrors the "task contract" typed boundary in mature Agent runtimes:
    transport-facing code never receives raw CLI/Web fields, so a Qoder,
    Claude, OpenAI, or local transport can be swapped without touching input
    parsing.
    """

    task_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    trace_path: Path
    scenario_type: ScenarioType
    scenario: str = Field(min_length=1)
    symptom: str = Field(min_length=1)
    output_dir: Path
    device: str | None = None
    build: str | None = None
    time_range: str | None = None
    target_process: str | None = None
    operation_marker: str | None = None
    start_marker: str | None = None
    end_marker: str | None = None
    response_marker: str | None = None
    completion_marker: str | None = None
    problem_duration_ms: float | None = Field(default=None, gt=0)
    refresh_rate_hz: float | None = Field(default=None, gt=0)
    baseline_trace_path: Path | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_request(
        cls,
        request: AnalyzeRequest,
        *,
        task_id: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> "TaskContract":
        """Adapt the existing CLI/Web input model into the task contract."""

        return cls(
            task_id=task_id or request.trace_id,
            trace_id=request.trace_id,
            trace_path=request.trace_path,
            scenario_type=request.scenario_type,
            scenario=request.scenario,
            symptom=request.symptom,
            output_dir=request.output_dir,
            device=request.device,
            build=request.build,
            time_range=request.time_range,
            target_process=request.target_process,
            operation_marker=request.operation_marker,
            start_marker=request.start_marker,
            end_marker=request.end_marker,
            response_marker=request.response_marker,
            completion_marker=request.completion_marker,
            problem_duration_ms=request.problem_duration_ms,
            refresh_rate_hz=request.refresh_rate_hz,
            baseline_trace_path=request.baseline_trace_path,
            constraints=dict(constraints or {}),
        )
