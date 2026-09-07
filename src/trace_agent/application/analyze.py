from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Iterator
from uuid import uuid4

from trace_agent.agent import AnalysisAgent, AnalysisAgentFactory
from trace_agent.agent.registry import metadata_for_kind
from trace_agent.application.boundary_normalization import (
    ExplicitProblemIntervalNormalizer,
)
from trace_agent.application.checkpoint import StepCheckpointStore
from trace_agent.application.completion_phase_evidence import (
    CompletionPhaseEvidenceEnsurer,
)
from trace_agent.application.evidence_normalization import (
    DeterministicEvidenceNormalizer,
)
from trace_agent.application.normalization import ColdStartMetricNormalizer
from trace_agent.application.preflight import DeterministicPreflight
from trace_agent.errors import (
    AgentFailure,
    RunInterrupted,
    classify_exception,
)
from trace_agent.evidence import EvidenceStore
from trace_agent.memory import (
    MEMORY_ENABLED_DEFAULT,
    MemoryRuntime,
    build_episode,
    default_memory_runtime,
    memory_enabled_explicit,
)
from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
    RunManifest,
    RunResult,
    RunStatus,
    RunStepState,
    SkillSnapshot,
    StepStatus,
    ValidationReport,
    utc_now,
)
from trace_agent.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStatus,
    ProgressToAgentBridge,
    emit_progress,
)
from trace_agent.runtime import (
    Deadline,
    EventType,
    LatencyPolicy,
    RunContext,
    StageLatencyRecorder,
    TraceAgentEventBus,
    get_event_bus,
    write_performance_report,
)
from trace_agent.report import ReportRenderer
from trace_agent.tools import (
    DefaultToolRegistryFactory,
    ToolRegistryFactory,
)
from trace_agent.trace import TraceAdapter
from trace_agent.validation import (
    AnalysisResultValidator,
    AnalysisValidationError,
)

_TOTAL_STEPS = 8
_REUSABLE_STEP_STAGES = {
    "run.prepare",
    "agent.analyze",
}
_RESUMABLE_STATUSES = {
    RunStatus.RUNNING,
    RunStatus.INTERRUPTED,
    RunStatus.RESUMABLE,
    RunStatus.RUNNING_RECOVERY,
}


def _memory_query(request: AnalyzeRequest) -> str:
    """Build a compact recall query for current scenario context."""
    parts = [
        request.scenario_type.value,
        request.scenario,
        request.symptom,
    ]
    if request.target_process:
        parts.append(request.target_process)
    if request.device:
        parts.append(request.device)
    if request.build:
        parts.append(request.build)
    return " ".join(parts)



@dataclass(frozen=True, slots=True)
class StepScope:
    run: bool
    input_hash: str | None = None
    state: RunStepState | None = None


class AnalyzeApplication:
    """Deterministic lifecycle orchestration for one durable analysis job."""

    def __init__(
        self,
        *,
        trace_adapter: TraceAdapter,
        agent: AnalysisAgent | None = None,
        agent_factory: AnalysisAgentFactory | None = None,
        skills: list[SkillSnapshot] | None = None,
        tool_registry_factory: ToolRegistryFactory | None = None,
        report_renderer: ReportRenderer | None = None,
        result_validator: AnalysisResultValidator | None = None,
        problem_interval_normalizer: ExplicitProblemIntervalNormalizer
        | None = None,
        metric_normalizer: ColdStartMetricNormalizer | None = None,
        evidence_normalizer: DeterministicEvidenceNormalizer | None = None,
        completion_phase_evidence_ensurer: CompletionPhaseEvidenceEnsurer
        | None = None,
        progress_callback: ProgressCallback | None = None,
        deterministic_preflight: DeterministicPreflight | None = None,
        event_bus: TraceAgentEventBus | None = None,
        memory_runtime: MemoryRuntime | None = None,
        memory_enabled: bool | None = None,
    ) -> None:
        if (agent is None) == (agent_factory is None):
            raise ValueError(
                "agent 与 agent_factory 必须且只能提供其中一个"
            )
        self._trace_adapter = trace_adapter
        self._agent = agent
        self._agent_factory = agent_factory
        self._skills = list(skills or [])
        self._tool_registry_factory = (
            tool_registry_factory or DefaultToolRegistryFactory()
        )
        self._report_renderer = report_renderer or ReportRenderer()
        self._result_validator = (
            result_validator or AnalysisResultValidator()
        )
        self._problem_interval_normalizer = (
            problem_interval_normalizer
            or ExplicitProblemIntervalNormalizer()
        )
        self._metric_normalizer = (
            metric_normalizer or ColdStartMetricNormalizer()
        )
        self._evidence_normalizer = (
            evidence_normalizer or DeterministicEvidenceNormalizer()
        )
        self._completion_phase_evidence_ensurer = (
            completion_phase_evidence_ensurer
            or CompletionPhaseEvidenceEnsurer()
        )
        self._progress_callback = progress_callback
        self._event_bus = event_bus or get_event_bus()
        self._run_context: RunContext | None = None
        self._progress_bridge: ProgressToAgentBridge | None = None
        self._deterministic_preflight = (
            deterministic_preflight or DeterministicPreflight()
        )
        self._latency_policy = LatencyPolicy()
        self._stage_recorder = StageLatencyRecorder(self._latency_policy)
        self._run_started_monotonic: float | None = None
        self._cancel_event: threading.Event | None = None
        self._job_deadline: Deadline | None = None
        self._memory_runtime = memory_runtime
        explicit_memory = memory_enabled_explicit()
        self._memory_enabled = (
            memory_enabled
            if memory_enabled is not None
            else (
                explicit_memory
                if explicit_memory is not None
                else MEMORY_ENABLED_DEFAULT
            )
        )

    async def run(
        self,
        request: AnalyzeRequest,
        *,
        resume_from: str | None = None,
        run_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RunResult:
        output_dir = request.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        self._current_run_path = output_dir / "run.json"
        checkpoint = StepCheckpointStore(output_dir)
        request_payload = request.model_dump(mode="json")

        manifest = self._load_or_create_manifest(
            request,
            output_dir,
            resume_from=resume_from,
            run_id=run_id,
        )
        resuming = manifest.status in _RESUMABLE_STATUSES
        if resuming:
            manifest.status = RunStatus.RUNNING_RECOVERY
            manifest.attempt_id = f"attempt-{uuid4().hex[:8]}"
            manifest.resume_from = resume_from or self._first_pending_step(
                manifest
            )
        manifest.last_heartbeat_at = utc_now()
        self._write_json(self._current_run_path, manifest)

        self._run_started_monotonic = perf_counter()
        self._stage_recorder = StageLatencyRecorder(self._latency_policy)

        self._cancel_event = cancel_event or threading.Event()
        self._job_deadline = Deadline.after_seconds(
            self._latency_policy.job_deadline_seconds
        )
        self._run_context = RunContext(
            run_id=manifest.run_id,
            event_bus=self._event_bus,
            cancel_event=self._cancel_event,
            deadline=self._job_deadline,
        )
        self._progress_bridge = ProgressToAgentBridge(
            manifest.run_id,
            self._event_bus,
        )
        self._run_context.publish(
            EventType.RUN_STARTED,
            data={
                "trace_id": request.trace_id,
                "scenario_type": request.scenario_type.value,
                "agent": request.agent.value,
                "resuming": resuming,
            },
        )

        evidence = EvidenceStore(request.trace_id)
        analysis: AnalysisResult | None = None
        trace: object | None = None
        tools: object | None = None
        report_path: Path | None = None
        self._memory_context: str | None = None
        if self._memory_runtime is None:
            self._memory_runtime = default_memory_runtime(Path.cwd())
        self._resume_snapshot = None
        if self._memory_enabled:
            try:
                if resuming:
                    self._resume_snapshot = self._memory_runtime.resume_plan(
                        output_dir
                    )
                recall = self._memory_runtime.recall(
                    _memory_query(request),
                    scenario_type=request.scenario_type.value,
                )
                self._memory_context = recall.context_prompt or None
            except Exception:
                self._memory_context = None

        with self._step(
            1,
            "run.prepare",
            "初始化运行",
            manifest=manifest,
            checkpoint=checkpoint,
            request_payload=request_payload,
            resuming=resuming,
        ) as scope:
            if scope.run:
                self._write_json(self._current_run_path, manifest)

        try:
            with self._step(
                2,
                "trace.prepare",
                "准备 Trace 数据",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
            ) as scope:
                if scope.run:
                    trace = await self._trace_adapter.prepare(
                        request,
                        workspace=output_dir / "work" / manifest.run_id,
                    )
            self._emit_trace_conversion_progress(trace)

            with self._step(
                3,
                "analysis.setup",
                "初始化 Tools 与 Skills",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
            ) as scope:
                if scope.run:
                    tools = self._tool_registry_factory.build(
                        trace,
                        evidence,
                    )
                    tools.set_progress_callback(self._progress)
                    manifest.trace_capabilities = trace.capabilities
                    manifest.available_tools = tools.names()
                    manifest.database_path = (
                        str(trace.database_path.resolve())
                        if trace.database_path
                        else None
                    )
                    manifest.baseline_database_path = (
                        str(trace.baseline_database_path.resolve())
                        if trace.baseline_database_path
                        else None
                    )
                    manifest.trace_conversions = trace.conversions

                    analysis_agent = self._agent
                    if self._agent_factory is not None:
                        selection = self._agent_factory.create(
                            request,
                            trace,
                            memory_context=self._memory_context,
                        )
                        analysis_agent = selection.agent
                        manifest.skills = [
                            skill.snapshot() for skill in selection.skills
                        ]
                    assert analysis_agent is not None
                    self._write_json(self._current_run_path, manifest)

            with self._step(
                4,
                "analysis.preflight",
                "执行前置检查",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
                artifact_paths=[output_dir / "preflight-report.json"],
            ) as scope:
                if scope.run:
                    if metadata_for_kind(request.agent).requires_preflight:
                        preflight_report = (
                            self._deterministic_preflight.assess(
                                request,
                                tools,
                            )
                        )
                        self._write_json(
                            output_dir / "preflight-report.json",
                            preflight_report,
                        )
                        if self._run_context is not None:
                            self._run_context.publish(
                                EventType.PHASE_COMPLETED,
                                phase="analysis.preflight",
                                data={
                                    "ready": preflight_report.ready,
                                    "tool_available": (
                                        preflight_report.tool_available
                                    ),
                                    "gap_count": len(preflight_report.gaps),
                                },
                            )
                        if not preflight_report.ready:
                            raise AgentFailure(
                                "\n".join(
                                    f"{gap.field}: {gap.message}"
                                    for gap in preflight_report.blocking
                                )
                            )

            with self._step(
                5,
                "agent.analyze",
                "正在执行 Agent 分析",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
                artifact_paths=[output_dir / "analysis-checkpoint.json"],
            ) as scope:
                if scope.run:
                    analysis = await analysis_agent.analyze(
                        request,
                        tools,
                        checkpoint=lambda payload: (
                            self._flush_checkpoint(
                                checkpoint,
                                "agent.analyze",
                                request_payload,
                                payload,
                                output_dir,
                            )
                        ),
                        run_context=self._run_context,
                    )
                    self._write_json(
                        output_dir / "analysis-checkpoint.json",
                        analysis,
                    )
                else:
                    analysis = self._load_analysis(output_dir)
                    if analysis is None:
                        raise AgentFailure(
                            "未找到 analysis-checkpoint.json 文件",
                            category=classify_exception(
                                RuntimeError("missing checkpoint")
                            ),
                        )

            with self._step(
                6,
                "analysis.normalize",
                "规范化 Evidence 结果",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
                artifact_paths=[output_dir / "analysis-checkpoint.json"],
            ) as scope:
                if scope.run:
                    analysis = self._problem_interval_normalizer.normalize(
                        analysis,
                        request,
                    )
                    analysis = self._metric_normalizer.normalize(
                        analysis,
                        evidence.evidence,
                    )
                    phase_record = (
                        self._completion_phase_evidence_ensurer.ensure(
                            analysis,
                            trace=trace,
                            evidence=evidence,
                        )
                    )
                    if phase_record is not None:
                        self._progress(
                            ProgressEvent(
                                stage=(
                                    "analysis.normalize."
                                    "completion-phases"
                                ),
                                status=ProgressStatus.INFO,
                                message=(
                                    "已补充完成阶段证据"
                                ),
                                details={
                                    "evidence_id": (
                                        phase_record.evidence_id
                                    ),
                                    "summary": phase_record.summary,
                                },
                            ),
                        )
                    analysis = self._evidence_normalizer.normalize(
                        analysis,
                        evidence.evidence,
                    )

            with self._step(
                7,
                "analysis.validate",
                "校验分析结果",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
                artifact_paths=[output_dir / "validation.json"],
            ) as scope:
                if scope.run:
                    validation = self._result_validator.validate(
                        request=request,
                        analysis=analysis,
                        evidence=evidence.evidence,
                        trace=trace,
                    )
                    self._write_json(
                        output_dir / "validation.json",
                        validation,
                    )
                    if not validation.valid:
                        rejected_payload = {
                            "status": "incomplete",
                            "analysis": analysis.model_dump(mode="json"),
                        }
                        (output_dir / "rejected-findings.json").write_text(
                            json.dumps(
                                rejected_payload,
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        raise AnalysisValidationError(validation)

            validation: object = self._load_validation(output_dir)
            validation_limitations = [
                f"警告 [{issue.code}] {issue.message}"
                for issue in getattr(validation, "warnings", [])
            ]
            for limitation in validation_limitations:
                if limitation not in analysis.limitations:
                    analysis.limitations.append(limitation)

            with self._step(
                8,
                "report.render",
                "渲染 HTML 报告",
                manifest=manifest,
                checkpoint=checkpoint,
                request_payload=request_payload,
                resuming=resuming,
                artifact_paths=[
                    output_dir / "findings.json",
                    output_dir / "report.html",
                ],
            ) as scope:
                if scope.run:
                    self._write_json(output_dir / "findings.json", analysis)
                    evidence.write(output_dir)
                    report_path = output_dir / "report.html"
                    self._report_renderer.render(
                        request=request,
                        analysis=analysis,
                        evidence=evidence.evidence,
                        output_path=report_path,
                        database_path=trace.database_path,
                    )

                    manifest.status = RunStatus.COMPLETED
                    manifest.completed_at = utc_now()
                    manifest.last_heartbeat_at = utc_now()
                    self._write_json(self._current_run_path, manifest)

            if report_path is None:
                report_path = output_dir / "report.html"
            if self._run_context is not None:
                self._run_context.publish(
                    EventType.RUN_COMPLETED,
                    data={
                        "report_path": str(report_path),
                        "output_dir": str(output_dir),
                    },
                )
            if self._memory_enabled and analysis is not None:
                self._remember_episode(request, analysis, manifest, output_dir)
            self._write_performance_report(
                request,
                output_dir,
                manifest,
            )
            return RunResult(
                run_id=manifest.run_id,
                output_dir=output_dir,
                report_path=report_path,
                analysis=analysis,
            )
        except Exception as exc:
            interrupted = isinstance(exc, RunInterrupted)
            evidence.write(output_dir)
            manifest.status = (
                RunStatus.INTERRUPTED if interrupted else RunStatus.FAILED
            )
            manifest.completed_at = utc_now()
            manifest.last_heartbeat_at = utc_now()
            manifest.error = f"{classify_exception(exc).value}: {exc}"
            self._write_json(self._current_run_path, manifest)

            if self._run_context is not None:
                self._run_context.publish(
                    (
                        EventType.RUN_INTERRUPTED
                        if interrupted
                        else EventType.RUN_FAILED
                    ),
                    data={"error": str(exc)},
                )
            self._write_performance_report(
                request,
                output_dir,
                manifest,
            )

            run_path = output_dir / "run.json"
            if isinstance(exc, AgentFailure):
                if run_path not in exc.diagnostic_paths:
                    exc.diagnostic_paths.append(run_path)
                raise

            exc.diagnostic_paths = [run_path]  # type: ignore[attr-defined]
            raise

    @contextmanager
    def _step(
        self,
        step: int,
        stage: str,
        message: str,
        *,
        manifest: RunManifest,
        checkpoint: StepCheckpointStore,
        request_payload: dict,
        resuming: bool,
        artifact_paths: list[Path] | None = None,
    ) -> Iterator[StepScope]:
        input_hash = checkpoint.input_hash(stage, request_payload)
        artifacts = [str(path.resolve()) for path in (artifact_paths or [])]
        existing = checkpoint.load(stage)
        can_reuse = (
            resuming
            and stage in _REUSABLE_STEP_STAGES
            and existing is not None
            and existing.status is StepStatus.DONE
            and existing.input_hash == input_hash
        )
        if can_reuse:
            self._progress(
                ProgressEvent(
                    stage=stage,
                    status=ProgressStatus.INFO,
                    message=f"{message}·已复用",
                    step=step,
                    total_steps=_TOTAL_STEPS,
                    details={"reused": True},
                ),
            )
            manifest.last_heartbeat_at = utc_now()
            self._write_json(self._current_run_path, manifest)
            yield StepScope(
                run=False,
                input_hash=input_hash,
                state=existing,
            )
            return

        started = perf_counter()
        self._raise_if_job_deadline_exceeded()
        self._stage_recorder.start(stage)
        running_state = checkpoint.record(
            stage,
            status=StepStatus.RUNNING,
            input_hash=input_hash,
            started_at=utc_now(),
        )
        manifest.step_states = self._upsert_step_state(
            manifest.step_states,
            running_state,
        )
        manifest.last_heartbeat_at = utc_now()
        self._write_json(self._current_run_path, manifest)
        self._progress(
            ProgressEvent(
                stage=stage,
                status=ProgressStatus.STARTED,
                message=message,
                step=step,
                total_steps=_TOTAL_STEPS,
            ),
        )
        try:
            yield StepScope(
                run=True,
                input_hash=input_hash,
                state=running_state,
            )
        except Exception as exc:
            failed_state = checkpoint.record(
                stage,
                status=StepStatus.FAILED,
                input_hash=input_hash,
                artifact_paths=artifacts,
                error=str(exc),
                started_at=running_state.started_at,
            )
            manifest.step_states = self._upsert_step_state(
                manifest.step_states,
                failed_state,
            )
            self._stage_recorder.finish(stage)
            manifest.last_heartbeat_at = utc_now()
            self._progress(
                ProgressEvent(
                    stage=stage,
                    status=ProgressStatus.FAILED,
                    message=message,
                    step=step,
                    total_steps=_TOTAL_STEPS,
                    elapsed_ms=(perf_counter() - started) * 1000,
                    details={"error": str(exc)},
                ),
            )
            self._write_json(self._current_run_path, manifest)
            raise
        done_state = checkpoint.record(
            stage,
            status=StepStatus.DONE,
            input_hash=input_hash,
            artifact_paths=artifacts,
            started_at=running_state.started_at,
        )
        manifest.step_states = self._upsert_step_state(
            manifest.step_states,
            done_state,
        )
        self._stage_recorder.finish(stage)
        manifest.last_heartbeat_at = utc_now()
        self._progress(
            ProgressEvent(
                stage=stage,
                status=ProgressStatus.COMPLETED,
                message=message,
                step=step,
                total_steps=_TOTAL_STEPS,
                elapsed_ms=(perf_counter() - started) * 1000,
            ),
        )
        self._write_json(self._current_run_path, manifest)

    @staticmethod
    def _upsert_step_state(
        states: list[RunStepState],
        incoming: RunStepState,
    ) -> list[RunStepState]:
        result = [
            state for state in states if state.step != incoming.step
        ]
        result.append(incoming)
        return result

    @staticmethod
    def _first_pending_step(manifest: RunManifest) -> str:
        done = {
            state.step
            for state in manifest.step_states
            if state.status is StepStatus.DONE
        }
        for stage in (
            "run.prepare",
            "trace.prepare",
            "analysis.setup",
            "analysis.preflight",
            "agent.analyze",
            "analysis.normalize",
            "analysis.validate",
            "report.render",
        ):
            if stage not in done:
                return stage
        return "report.render"

    @staticmethod
    def _load_analysis(output_dir: Path) -> AnalysisResult | None:
        path = output_dir / "analysis-checkpoint.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return AnalysisResult.model_validate(payload)
        except ValueError:
            return None

    @staticmethod
    def _load_validation(output_dir: Path):
        path = output_dir / "validation.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return ValidationReport.model_validate(payload)
        except ValueError:
            return None

    def _load_or_create_manifest(
        self,
        request: AnalyzeRequest,
        output_dir: Path,
        *,
        resume_from: str | None,
        run_id: str | None = None,
    ) -> RunManifest:
        existing = self._load_existing_manifest(output_dir)
        if resume_from is not None or (
            existing is not None and existing.status in _RESUMABLE_STATUSES
        ):
            return existing if existing is not None else self._new_manifest(
                request,
                resume_from=resume_from,
                run_id=run_id,
            )
        return self._new_manifest(request, run_id=run_id)

    @staticmethod
    def _load_existing_manifest(output_dir: Path) -> RunManifest | None:
        path = output_dir / "run.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return RunManifest.model_validate(payload)
        except ValueError:
            return None

    @staticmethod
    def _new_manifest(
        request: AnalyzeRequest,
        *,
        resume_from: str | None = None,
        run_id: str | None = None,
    ) -> RunManifest:
        return RunManifest(
            run_id=run_id or f"run-{uuid4().hex[:12]}",
            trace_id=request.trace_id,
            status=(
                RunStatus.RESUMABLE
                if resume_from is not None
                else RunStatus.RUNNING
            ),
            agent=request.agent,
            provider=request.provider,
            model=request.model,
            scenario_type=request.scenario_type,
            scenario=request.scenario,
            symptom=request.symptom,
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
            trace_path=str(request.trace_path.resolve()),
            baseline_trace_path=(
                str(request.baseline_trace_path.resolve())
                if request.baseline_trace_path
                else None
            ),
            skills=[],
            resume_from=resume_from,
            attempt_id=f"attempt-{uuid4().hex[:8]}",
        )

    def _flush_checkpoint(
        self,
        checkpoint: StepCheckpointStore,
        stage: str,
        request_payload: dict,
        payload: dict,
        output_dir: Path,
    ) -> None:
        input_hash = checkpoint.input_hash(stage, request_payload)
        checkpoint.record(
            stage,
            status=StepStatus.RUNNING,
            input_hash=input_hash,
            error=payload.get("error"),
            artifact_paths=[
                str((output_dir / "analysis-checkpoint.json").resolve())
            ],
        )

    def _progress(self, event: ProgressEvent) -> None:
        """Forward legacy progress to the CLI callback and the event bus."""
        emit_progress(self._progress_callback, event)
        if self._progress_bridge is not None:
            try:
                self._progress_bridge(event)
            except Exception:
                return

    def _raise_if_job_deadline_exceeded(self) -> None:
        """Enforce the job deadline by cancelling in-flight work.

        This is the "deadline -> cancellation -> cleanup" boundary: once the
        budget is exhausted we mark the shared token so the agent and its LLM
        stream stop at their next cooperation point instead of the job merely
        noticing the overrun after the fact.
        """
        if self._job_deadline is None or not self._job_deadline.expired():
            return
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._run_context is not None:
            self._run_context.raise_if_cancelled()
        raise TimeoutError(
            "analysis job exceeded its deadline"
        )  # pragma: no cover - defensive fallback without RunContext

    def _write_performance_report(
        self,
        request: AnalyzeRequest,
        output_dir: Path,
        manifest: RunManifest,
    ) -> Path | None:
        """Persist machine-readable latency telemetry for regression checks.

        The report is best-effort: analysis correctness must never depend on
        telemetry IO. LLM metrics are read from ``agent-result.json`` when the
        concrete SDK adapter produced them, so TTFT/TTFE stay in one
        durable place.
        """
        started = self._run_started_monotonic
        total_duration_ms = (
            (perf_counter() - started) * 1000
            if started is not None
            else None
        )
        llm_metrics: dict[str, Any] = {}
        agent_result_path = output_dir / "agent-result.json"
        if agent_result_path.is_file():
            try:
                agent_result = json.loads(
                    agent_result_path.read_text(encoding="utf-8")
                )
                candidate = agent_result.get("llm_metrics")
                if isinstance(candidate, dict):
                    llm_metrics = candidate
            except (OSError, ValueError, json.JSONDecodeError):
                llm_metrics = {}
        tool_metrics = self._load_tool_latency_records(output_dir)
        try:
            return write_performance_report(
                output_dir / "performance.json",
                run_id=manifest.run_id,
                trace_id=request.trace_id,
                agent=request.agent.value,
                scenario_type=request.scenario_type.value,
                total_duration_ms=total_duration_ms,
                stages=self._stage_recorder.snapshot(),
                llm_metrics=llm_metrics,
                tool_metrics=tool_metrics,
                job_deadline_seconds=(
                    self._latency_policy.job_deadline_seconds
                ),
            )
        except Exception:
            return None

    @staticmethod
    def _load_tool_latency_records(
        output_dir: Path,
    ) -> list[dict[str, Any]]:
        """Read persisted tool audit rows into compact latency line items."""
        path = output_dir / "agent-log.jsonl"
        if not path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                payload = json.loads(raw_line)
                records.append(
                    {
                        "tool": payload.get("tool"),
                        "status": payload.get("status"),
                        "duration_ms": payload.get("duration_ms"),
                        "started_at": payload.get("started_at"),
                    }
                )
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        return records


    def _remember_episode(
        self,
        request: AnalyzeRequest,
        analysis: AnalysisResult,
        manifest: RunManifest,
        output_dir: Path,
    ) -> None:
        """Best-effort capture plus consolidation after a completed run."""
        try:
            episode = build_episode(
                request,
                analysis,
                run_id=manifest.run_id,
                output_dir=str(output_dir),
            )
            self._memory_runtime.remember_episode(
                episode,
                consolidate=True,
            )
            self._memory_runtime.dreaming.enqueue_episode(
                episode.episode_id
            )
        except Exception:
            return



    def _emit_trace_conversion_progress(self, trace: object) -> None:
        conversions = getattr(trace, "conversions", [])
        for conversion in conversions:
            cache_hit = bool(getattr(conversion, "cache_hit", False))
            role = getattr(conversion, "role", "trace")
            message = (
                f"{role} Trace 数据库已命中 TraceStreamer 缓存"
                if cache_hit
                else f"{role} Trace 未命中 TraceStreamer 缓存"
            )
            self._progress(
                ProgressEvent(
                    stage="trace.cache",
                    status=ProgressStatus.INFO,
                    message=message,
                    details={
                        "cache_hit": cache_hit,
                        "cache_key": getattr(
                            conversion,
                            "cache_key",
                            None,
                        ),
                    },
                ),
            )

    @staticmethod
    def _write_json(path: Path, model: object) -> None:
        if not hasattr(model, "model_dump"):
            raise TypeError("仅支持 Pydantic 模型")
        payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )




