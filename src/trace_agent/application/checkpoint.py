from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from trace_agent.models import RunStepState, StepStatus, utc_now


_SAFE_STEP_RE = re.compile(r"[^A-Za-z0-9._-]+")


class StepCheckpointStore:
    """Durable per-step state files under ``<output_dir>/steps``.

    File layout::

        <output_dir>/steps/<step>.state.json

    Each file holds the same subset as :class:`RunStepState`. The in-memory
    manifest mirrors ``step_states`` and remains the authoritative resume
    index, while these files are the crash-safe event log consumed during
    recovery.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir.resolve()
        self.steps_dir = self.output_dir / "steps"

    def uri(self, step: str) -> str:
        return str(self.path(step))

    def path(self, step: str) -> Path:
        safe = _SAFE_STEP_RE.sub("-", step).strip("-") or "step"
        return self.steps_dir / f"{safe}.state.json"

    def load(self, step: str) -> RunStepState | None:
        path = self.path(step)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return RunStepState.model_validate(payload)
        except ValueError:
            return None

    def done(self, step: str, *, input_hash: str) -> bool:
        state = self.load(step)
        return (
            state is not None
            and state.status is StepStatus.DONE
            and state.input_hash == input_hash
        )

    def record(
        self,
        step: str,
        *,
        status: StepStatus,
        input_hash: str | None = None,
        artifact_paths: list[str] | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> RunStepState:
        state = RunStepState(
            step=step,
            status=status,
            input_hash=input_hash,
            artifact_paths=list(artifact_paths or []),
            error=error,
            started_at=started_at or utc_now(),
            completed_at=(
                completed_at
                if status is StepStatus.RUNNING
                else (completed_at or utc_now())
            ),
        )
        path = self.path(step)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                state.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return state

    @staticmethod
    def input_hash(step: str, request_payload: dict) -> str:
        canonical = json.dumps(
            {"step": step, "request": request_payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
