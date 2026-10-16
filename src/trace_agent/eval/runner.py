from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Callable
from uuid import uuid4

from trace_agent.application import AnalyzeApplication
from trace_agent.errors import (
    AgentFailure,
    AgentRetryExhausted,
    RunInterrupted,
)
from trace_agent.eval.models import (
    AgentOutput,
    Case,
    Trial,
    TrialFailure,
    TrialStatus,
    TrialMemory,
)
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
    RunResult,
    utc_now,
)

ApplicationFactory = Callable[[], AnalyzeApplication]


class TrialConfigError(ValueError):
    """Raised when a trial cannot be constructed deterministically."""


class TrialRunner:
    """Runs one case through the production analysis entry point.

    The runner intentionally does not reimplement agent logic: it adapts the
    case into an :class:`AnalyzeRequest` and calls the exact same
    ``AnalyzeApplication`` used by the CLI/Web code paths.  Each trial writes
    its artifacts into a fresh, isolated output directory.
    """

    def __init__(
        self,
        *,
        application_factory: ApplicationFactory,
        output_root: Path,
        timeout_seconds: float = 3600.0,
        agent: AgentKind = AgentKind.LOCAL,
    ) -> None:
        if application_factory is None:
            raise TrialConfigError("application_factory is required")
        self._factory = application_factory
        self._output_root = output_root.resolve()
        self._timeout_seconds = timeout_seconds
        self._agent = agent

    async def run(
        self,
        case: Case,
        *,
        index: int = 0,
    ) -> Trial:
        trial_id = f"trial-{uuid4().hex[:12]}"
        output_dir = self._output_root / case.id / f"trial-{index}"
        output_dir.mkdir(parents=True, exist_ok=True)

        request = self._to_request(case, output_dir)
        trial = Trial(
            trial_id=trial_id,
            case_id=case.id,
            index=index,
        )

        started = perf_counter()
        try:
            application = self._factory()
            run_result = await asyncio.wait_for(
                application.run(request),
                timeout=self._timeout_seconds,
            )
            elapsed_ms = (perf_counter() - started) * 1000
            output = self._capture(output_dir, run_result)
            trial.status = TrialStatus.COMPLETED
            trial.failure = TrialFailure.SUCCESS
            trial.output = output
            trial.elapsed_ms = round(elapsed_ms, 3)
            trial.tool_calls = self._read_tool_calls(output_dir)
            trial.transcript = self._read_transcript(output_dir)
        except asyncio.TimeoutError as exc:
            trial.status = TrialStatus.TIMEOUT
            trial.failure = TrialFailure.TIMEOUT
            trial.error = f"trial exceeded {self._timeout_seconds}s"
            trial.elapsed_ms = round((perf_counter() - started) * 1000, 3)
            self._capture_error(output_dir, exc)
        except Exception as exc:  # noqa: BLE001 - eval captures failures
            trial.status = TrialStatus.ERROR
            trial.failure = self._classify_error(exc)
            trial.error = f"{type(exc).__name__}: {exc}"
            trial.elapsed_ms = round((perf_counter() - started) * 1000, 3)
            self._capture_error(output_dir, exc)
        finally:
            trial.completed_at = utc_now()
            # Read on every path, not just the successful one. A run that died at
            # validation still *recalled*, and its manifest still carries the
            # memory_trace: skipping it here made the report claim "memory off"
            # for a run where memory was on, which hides precisely the injection
            # the section 8.4 contamination audit exists to catch.
            trial.memory = self._read_memory(output_dir)

        return trial

    def _to_request(self, case: Case, output_dir: Path) -> AnalyzeRequest:
        case_input = case.input
        return AnalyzeRequest(
            trace_id=case.id,
            trace_path=case_input.trace,
            scenario_type=case_input.type,
            scenario=case_input.scenario,
            symptom=case_input.symptom,
            output_dir=output_dir,
            device=case_input.device,
            build=case_input.build,
            time_range=case_input.time_range,
            target_process=case_input.target_process,
            operation_marker=case_input.operation_marker,
            start_marker=case_input.start_marker,
            end_marker=case_input.end_marker,
            response_marker=case_input.response_marker,
            completion_marker=case_input.completion_marker,
            problem_duration_ms=case_input.problem_duration_ms,
            refresh_rate_hz=case_input.refresh_rate_hz,
            baseline_trace_path=case_input.baseline_trace_path,
            agent=self._agent,
        )

    @staticmethod
    def _capture(
        output_dir: Path,
        run_result: RunResult,
    ) -> AgentOutput:
        raw = TrialRunner._read_json(output_dir / "agent-result.json")
        return AgentOutput(result=run_result.analysis, raw=raw)

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_tool_calls(output_dir: Path) -> list[dict]:
        path = output_dir / "agent-log.jsonl"
        if not path.is_file():
            return []
        calls: list[dict] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    calls.append(payload)
        except OSError:
            return []
        return calls

    @staticmethod
    def _read_transcript(output_dir: Path) -> list[dict]:
        calls = TrialRunner._read_tool_calls(output_dir)
        # The transport-level transcript is captured indirectly through the
        # tool log for the deterministic first version; it remains complete
        # and replayable from the persisted artifacts.
        return list(calls)

    @staticmethod
    def _read_memory(output_dir: Path) -> TrialMemory | None:
        """Read this trial's ``memory_trace`` out of the run manifest.

        The trace lives in ``run.json`` rather than ``agent-result.json`` because
        it spans both halves of a run: recall is written before the agent starts
        and capture is folded in afterwards, and only the manifest covers both.
        ``None`` means memory was off or the run died before writing one, which
        the report keeps distinguishable from "recall found nothing".
        """
        manifest = TrialRunner._read_json(output_dir / "run.json")
        return TrialMemory.from_trace(manifest.get("memory_trace"))

    @staticmethod
    def _capture_error(output_dir: Path, exc: Exception) -> None:
        try:
            (output_dir / "trial-error.txt").write_text(
                f"{type(exc).__name__}: {exc}",
                encoding="utf-8",
            )
        except OSError:
            return

    @staticmethod
    def _classify_error(exc: Exception) -> TrialFailure:
        """Map a runner-level exception onto the trial failure taxonomy.

        Ordering matters and reflects where the failure came from, not what
        happened incidentally:

        * ``agent_failure``: explicit production agent/provider boundary
          failures (including retry exhaustion and cooperative cancellation).
        * ``infra_failure``: environment, OS, runtime, import, or resource
          failures outside the agent's control.
        * ``invalid``: malformed data or contract violations (bad schema,
          bad JSON, bad key/type) that mean the trial was not a valid input.
        * ``eval_failure``: anything else, which is by elimination a bug in
          the harness/grader/evaluator rather than a real agent regression.
        """
        if isinstance(
            exc,
            (AgentFailure, AgentRetryExhausted, RunInterrupted),
        ):
            return TrialFailure.AGENT_FAILURE
        if isinstance(
            exc,
            (
                FileNotFoundError,
                PermissionError,
                OSError,
                ImportError,
                ModuleNotFoundError,
                ConnectionError,
            ),
        ):
            return TrialFailure.INFRA_FAILURE
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            return TrialFailure.INVALID
        return TrialFailure.EVAL_FAILURE

