from __future__ import annotations

from pathlib import Path

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import ExecutionCheckpointSnapshot

# Ordered stage names used by AnalyzeApplication. Keeping this list in one
# place makes resume planning auditable without coupling to the application.
EXECUTION_STAGES = (
    "run.prepare",
    "trace.prepare",
    "analysis.setup",
    "analysis.preflight",
    "agent.analyze",
    "analysis.normalize",
    "analysis.validate",
    "report.render",
)


class CheckpointResumeService:
    """Read-only resume projection over durable execution state.

    The service does not mutate checkpoints; it only turns ``run.json`` and
    ``steps/*.state.json`` into a structured ``ExecutionCheckpointSnapshot``
    that callers can use for resume decisions and observability.
    """

    def __init__(self, backend: JsonMemoryBackend) -> None:
        self._backend = backend

    def snapshot(self, output_dir: str | Path) -> ExecutionCheckpointSnapshot:
        snapshot = self._backend.execution_snapshot(output_dir)
        if not snapshot.resumable:
            snapshot.resumable = bool(
                snapshot.run_id and (
                    snapshot.failed_steps or snapshot.completed_steps
                )
            )
        pending = [
            stage for stage in EXECUTION_STAGES
            if stage not in snapshot.completed_steps
            and stage not in snapshot.failed_steps
        ]
        snapshot = snapshot.model_copy(update={"pending_steps": pending})
        if not snapshot.resume_from and (snapshot.resumable and pending):
            snapshot = snapshot.model_copy(update={"resume_from": pending[0]})
        return snapshot
