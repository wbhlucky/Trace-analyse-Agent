from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from trace_agent.eval.grader import DeterministicGrader
from trace_agent.eval.loader import iter_cases
from trace_agent.eval.models import (
    Case,
    EvalRun,
    GateThresholds,
    OperationalMetrics,
    TrialResult,
)
from trace_agent.eval.multi_trial import MultiTrialAggregator
from trace_agent.eval.runner import ApplicationFactory, TrialRunner
from trace_agent.eval.trajectory import TrajectoryConfig, TrajectoryGrader
from trace_agent.models import AgentKind, utc_now


class EvalHarness:
    """Provider-agnostic eval harness over the production agent boundary.

    It loads cases, runs isolated trials through the same analysis application
    as production, grades each outcome deterministically, and aggregates a
    durable ``EvalRun`` with case and suite scores plus per-case multi-trial
    results (``pass@k`` / ``pass^k``).

    Each trial records raw tool/transcript telemetry; the harness also grades
    the trajectory layer deterministically and keeps it separate from outcome
    correctness.  Trajectory quality never contributes to ``case_scores`` or
    ``suite_scores`` and never enters the outcome hard gates.
    """

    def __init__(
        self,
        *,
        application_factory: ApplicationFactory,
        output_root: Path | None = None,
        grader: DeterministicGrader | None = None,
        gate_thresholds: GateThresholds | None = None,
        aggregator: MultiTrialAggregator | None = None,
        trajectory_grader: TrajectoryGrader | None = None,
        trajectory_config: TrajectoryConfig | None = None,
        timeout_seconds: float = 3600.0,
        trials: int = 1,
        agent: AgentKind = AgentKind.LOCAL,
    ) -> None:
        self._application_factory = application_factory
        self._output_root = (
            output_root.resolve()
            if output_root is not None
            else Path(".trace-agent") / "eval"
        )
        self._grader = grader or DeterministicGrader()
        self._gate_thresholds = gate_thresholds or GateThresholds()
        self._timeout_seconds = timeout_seconds
        self._trials = max(1, trials)
        self._agent = agent
        self._aggregator = aggregator or MultiTrialAggregator(
            thresholds=self._gate_thresholds,
            k=self._trials,
        )
        self._trajectory_grader = trajectory_grader or TrajectoryGrader(
            trajectory_config
        )

    async def run(
        self,
        cases: list[Case],
        *,
        run_id: str | None = None,
    ) -> EvalRun:
        resolved_run_id = run_id or f"eval-{uuid4().hex[:12]}"
        run = EvalRun(
            run_id=resolved_run_id,
            artifact_dir=self._output_root / resolved_run_id,
        )

        for case in cases:
            values: list[float] = []
            trial_results: list[TrialResult] = []
            for index in range(self._trials):
                runner = TrialRunner(
                    application_factory=self._application_factory,
                    output_root=self._output_root / resolved_run_id,
                    timeout_seconds=self._timeout_seconds,
                    agent=self._agent,
                )
                trial = await runner.run(case, index=index)
                run.trials.append(trial)

                if trial.output is None:
                    values.append(0.0)
                    trial_results.append(
                        TrialResult(
                            case_id=case.id,
                            trial_id=trial.trial_id,
                            index=index,
                            status=trial.status,
                            failure=trial.failure,
                            error=trial.error,
                        )
                    )
                    continue

                grade = self._grader.grade(
                    case.id,
                    trial.trial_id,
                    case.gold,
                    trial.output,
                )
                run.grader_results.append(grade)
                outcome = self._aggregator.trial_outcome(
                    grade,
                    case_id=case.id,
                    trial_id=trial.trial_id,
                )
                trajectory = self._trajectory_grader.grade(
                    case.id,
                    trial.trial_id,
                    trial.tool_calls,
                )
                llm_metrics = self._extract_llm_metrics(trial.output.raw or {})
                tokens = llm_metrics.get("tokens")
                operational = OperationalMetrics(
                    case_id=case.id,
                    trial_id=trial.trial_id,
                    turns=len(trial.tool_calls),
                    tool_calls=len(trial.tool_calls),
                    tool_errors=self._count_tool_errors(trial.tool_calls),
                    tokens=tokens,
                    ttft_ms=llm_metrics.get("ttft_ms"),
                    latency_ms=trial.elapsed_ms,
                )
                values.append(grade.overall)
                trial_results.append(
                    TrialResult(
                        case_id=case.id,
                        trial_id=trial.trial_id,
                        index=index,
                        status=trial.status,
                        failure=trial.failure,
                        outcome=outcome,
                        trajectory=trajectory,
                        operational=operational,
                        error=trial.error,
                    )
                )

            run.case_scores[case.id] = round(
                sum(values) / len(values), 4
            ) if values else 0.0
            run.multi_trial_results[case.id] = self._aggregator.aggregate(
                case.id,
                trial_results,
            )

        suites: dict[str, list[str]] = {}
        for case in cases:
            suites.setdefault(case.suite, []).append(case.id)
        for suite, case_ids in suites.items():
            scores = [
                run.case_scores[case_id]
                for case_id in case_ids
                if case_id in run.case_scores
            ]
            run.suite_scores[suite] = round(
                sum(scores) / len(scores), 4
            ) if scores else 0.0

        run.completed_at = utc_now()
        return run

    @staticmethod
    def _extract_llm_metrics(raw: dict) -> dict:
        """Read ``tokens`` / ``ttft_ms`` from the persisted agent telemetry.

        ``agent-result.json`` keeps LLM timing under ``llm_metrics`` plus an
        optional normalized token count.  We tolerate absent/malformed fields
        because this is observational data, not an outcome gate.
        """
        metrics: dict = {}
        llm = raw.get("llm_metrics")
        if not isinstance(llm, dict):
            return metrics
        ttft_ms = llm.get("ttft_ms")
        if isinstance(ttft_ms, (int, float)):
            metrics["ttft_ms"] = float(ttft_ms)
        tokens = llm.get("total_tokens") or llm.get("tokens")
        if tokens is None:
            usage = llm.get("usage")
            if isinstance(usage, dict):
                tokens = (
                    usage.get("total_tokens")
                    or usage.get("input_tokens")
                    or usage.get("output_tokens")
                )
        if isinstance(tokens, (int, float)):
            metrics["tokens"] = int(tokens)
        return metrics

    @staticmethod
    def _count_tool_errors(tool_calls: list[dict]) -> int:
        errors = 0
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            if call.get("status") == "error" or bool(call.get("error")):
                errors += 1
        return errors

    @classmethod
    async def run_from_disk(
        cls,
        *,
        root: Path,
        application_factory: ApplicationFactory,
        output_root: Path | None = None,
        case_id: str | None = None,
        suite: str | None = None,
        trials: int = 1,
        gate_thresholds: GateThresholds | None = None,
        timeout_seconds: float = 3600.0,
        agent: AgentKind = AgentKind.LOCAL,
    ) -> EvalRun:
        cases = iter_cases(root, suite=suite)
        if case_id is not None:
            cases = [case for case in cases if case.id == case_id]
        harness = cls(
            application_factory=application_factory,
            output_root=output_root,
            gate_thresholds=gate_thresholds,
            timeout_seconds=timeout_seconds,
            trials=trials,
            agent=agent,
        )
        return await harness.run(cases)


def save_run(run: EvalRun, path: Path) -> Path:
    """Persist the complete EvalRun, including every trial and grade."""
    payload = run.model_dump(mode="json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_run(path: Path) -> EvalRun:
    if not path.is_file():
        raise FileNotFoundError(f"eval run not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return EvalRun.model_validate(payload)