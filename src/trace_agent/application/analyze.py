from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Iterator
from uuid import uuid4

from trace_agent.agent import AnalysisAgent, AnalysisAgentFactory
from trace_agent.application.boundary_normalization import (
    ExplicitProblemIntervalNormalizer,
)
from trace_agent.application.completion_phase_evidence import (
    CompletionPhaseEvidenceEnsurer,
)
from trace_agent.application.evidence_normalization import (
    DeterministicEvidenceNormalizer,
)
from trace_agent.application.normalization import ColdStartMetricNormalizer
from trace_agent.application.preflight import DeterministicPreflight
from trace_agent.evidence import EvidenceStore
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
    RunManifest,
    RunResult,
    RunStatus,
    SkillSnapshot,
    utc_now,
)
from trace_agent.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStatus,
    emit_progress,
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


class AnalyzeApplication:
    """Deterministic lifecycle orchestration for one analysis job."""

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
    ) -> None:
        if (agent is None) == (agent_factory is None):
            raise ValueError(
                "必须且只能提供 agent 或 agent_factory 其中一个"
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
        self._deterministic_preflight = (
            deterministic_preflight or DeterministicPreflight()
        )

    async def run(self, request: AnalyzeRequest) -> RunResult:
        output_dir = request.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        with self._stage(1, "run.prepare", "准备分析任务"):
            manifest = RunManifest(
                run_id=f"run-{uuid4().hex[:12]}",
                trace_id=request.trace_id,
                status=RunStatus.RUNNING,
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
                skills=self._skills,
            )
            self._write_json(output_dir / "run.json", manifest)

        evidence = EvidenceStore(request.trace_id)
        try:
            with self._stage(2, "trace.prepare", "准备 Trace 分析数据库"):
                trace = await self._trace_adapter.prepare(
                    request,
                    workspace=output_dir / "work" / manifest.run_id,
                )
            self._emit_trace_conversion_progress(trace)

            with self._stage(
                3,
                "analysis.setup",
                "识别能力并加载 Tools 与 Skills",
            ):
                tools = self._tool_registry_factory.build(trace, evidence)
                tools.set_progress_callback(self._progress_callback)
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
                    selection = self._agent_factory.create(request, trace)
                    analysis_agent = selection.agent
                    manifest.skills = [
                        skill.snapshot() for skill in selection.skills
                    ]
                assert analysis_agent is not None
                self._write_json(output_dir / "run.json", manifest)

            with self._stage(
                4,
                "analysis.preflight",
                "执行确定性预分析与候选提取",
            ):
                if request.agent is AgentKind.QODER:
                    self._deterministic_preflight.run(request, tools)

            with self._stage(
                5,
                "agent.analyze",
                "Agent 正在取证、决策并形成结论",
            ):
                analysis = await analysis_agent.analyze(request, tools)
                self._write_json(
                    output_dir / "analysis-checkpoint.json",
                    analysis,
                )

            with self._stage(
                6,
                "analysis.normalize",
                "绑定确定性 Evidence 并归一化结果",
            ):
                analysis = self._problem_interval_normalizer.normalize(
                    analysis,
                    request,
                )
                analysis = self._metric_normalizer.normalize(
                    analysis,
                    evidence.evidence,
                )
                phase_record = self._completion_phase_evidence_ensurer.ensure(
                    analysis,
                    trace=trace,
                    evidence=evidence,
                )
                if phase_record is not None:
                    emit_progress(
                        self._progress_callback,
                        ProgressEvent(
                            stage="analysis.normalize.completion-phases",
                            status=ProgressStatus.INFO,
                            message="补齐最终边界的完成时延精确阶段取证",
                            details={
                                "evidence_id": phase_record.evidence_id,
                                "summary": phase_record.summary,
                            },
                        ),
                    )
                analysis = self._evidence_normalizer.normalize(
                    analysis,
                    evidence.evidence,
                )

            with self._stage(
                7,
                "analysis.validate",
                "执行确定性结果校验",
            ):
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
                    self._write_json(
                        output_dir / "rejected-findings.json",
                        analysis,
                    )
                    raise AnalysisValidationError(validation)

            validation_limitations = [
                f"验证警告 [{issue.code}] {issue.message}"
                for issue in validation.warnings
            ]
            for limitation in validation_limitations:
                if limitation not in analysis.limitations:
                    analysis.limitations.append(limitation)

            with self._stage(8, "report.render", "生成 HTML 分析报告"):
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
                self._write_json(output_dir / "run.json", manifest)

            return RunResult(
                run_id=manifest.run_id,
                output_dir=output_dir,
                report_path=report_path,
                analysis=analysis,
            )
        except Exception as exc:
            evidence.write(output_dir)
            manifest.status = RunStatus.FAILED
            manifest.completed_at = utc_now()
            manifest.error = str(exc)
            self._write_json(output_dir / "run.json", manifest)
            raise

    @contextmanager
    def _stage(
        self,
        step: int,
        stage: str,
        message: str,
    ) -> Iterator[None]:
        started = perf_counter()
        emit_progress(
            self._progress_callback,
            ProgressEvent(
                stage=stage,
                status=ProgressStatus.STARTED,
                message=message,
                step=step,
                total_steps=8,
            ),
        )
        try:
            yield
        except Exception as exc:
            emit_progress(
                self._progress_callback,
                ProgressEvent(
                    stage=stage,
                    status=ProgressStatus.FAILED,
                    message=message,
                    step=step,
                    total_steps=8,
                    elapsed_ms=(perf_counter() - started) * 1000,
                    details={"error": str(exc)},
                ),
            )
            raise
        emit_progress(
            self._progress_callback,
            ProgressEvent(
                stage=stage,
                status=ProgressStatus.COMPLETED,
                message=message,
                step=step,
                total_steps=8,
                elapsed_ms=(perf_counter() - started) * 1000,
            ),
        )

    def _emit_trace_conversion_progress(self, trace: object) -> None:
        conversions = getattr(trace, "conversions", [])
        for conversion in conversions:
            cache_hit = bool(getattr(conversion, "cache_hit", False))
            role = getattr(conversion, "role", "trace")
            message = (
                f"{role} Trace DB 命中缓存，已跳过 TraceStreamer 转换"
                if cache_hit
                else f"{role} Trace 已完成 TraceStreamer 转换"
            )
            emit_progress(
                self._progress_callback,
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
            raise TypeError("只允许持久化 Pydantic 模型")
        payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
