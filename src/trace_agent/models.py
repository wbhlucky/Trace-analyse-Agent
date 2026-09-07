from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentKind(StrEnum):
    LOCAL = "local"
    QODER = "qoder"
    CLAUDE = "claude"


class LlmProvider(StrEnum):
    DEEPSEEK = "deepseek"
    BAILIAN = "bailian"


class ScenarioType(StrEnum):
    COLD_START = "cold-start"
    RESPONSE_LATENCY = "response-latency"
    COMPLETION_LATENCY = "completion-latency"
    FRAME_JANK = "frame-jank"


class TraceCapability(StrEnum):
    FILE_METADATA = "file-metadata"
    BASELINE_METADATA = "baseline-metadata"
    TRACE_DATABASE = "trace-database"
    PROCESSES = "processes"
    THREADS = "threads"
    SLICES = "slices"
    CPU_SCHEDULING = "cpu-scheduling"
    IO_EVENTS = "io-events"
    FRAME_EVENTS = "frame-events"
    MARKERS = "markers"
    APP_STARTUP_STAGES = "app-startup-stages"
    PERF_SAMPLES = "perf-samples"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RESUMABLE = "resumable"
    RUNNING_RECOVERY = "running-recovery"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class FindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OBSERVED = "observed"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"


class SchedulingDiagnosis(StrEnum):
    CPU_BOUND = "cpu-bound"
    CPU_CONTENTION = "cpu-contention"
    BLOCKED_WAIT = "blocked-wait"
    IO_WAIT = "io-wait"
    MIXED = "mixed"
    INCONCLUSIVE = "inconclusive"


class AnalyzeRequest(StrictModel):
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
    problem_duration_ms: float | None = Field(
        default=None,
        gt=0,
        le=3_600_000,
    )
    refresh_rate_hz: float | None = Field(default=None, gt=0)
    baseline_trace_path: Path | None = None
    agent: AgentKind = AgentKind.LOCAL
    provider: LlmProvider | None = None
    model: str | None = None


class TraceConversionRecord(StrictModel):
    role: str
    executable: str
    version: str | None = None
    command: list[str]
    database_path: str
    stdout_path: str
    stderr_path: str
    duration_ms: float = Field(ge=0)
    return_code: int
    cache_hit: bool = False
    cache_key: str | None = None
    source_database_path: str | None = None
    materialization: str | None = None


class TraceHandle(StrictModel):
    trace_id: str
    trace_path: Path
    format: str
    size_bytes: int = Field(ge=0)
    database_path: Path | None = None
    baseline_database_path: Path | None = None
    capabilities: list[TraceCapability] = Field(default_factory=list)
    baseline_capabilities: list[TraceCapability] = Field(
        default_factory=list
    )
    baseline_trace_path: Path | None = None
    baseline_size_bytes: int | None = Field(default=None, ge=0)
    conversions: list[TraceConversionRecord] = Field(default_factory=list)


class EvidenceRecord(StrictModel):
    evidence_id: str
    trace_id: str
    tool: str
    summary: str
    data: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)


class ToolAuditRecord(StrictModel):
    tool: str
    arguments: dict[str, Any]
    status: str
    started_at: datetime
    duration_ms: float = Field(ge=0)
    evidence_id: str | None = None
    error: str | None = None


class Finding(StrictModel):
    title: str
    severity: FindingSeverity
    status: FindingStatus
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    analysis: str
    recommendation: str
    verification: str


class ResolvedProcess(StrictModel):
    name: str = Field(min_length=1)
    pid: int = Field(ge=0)
    ipid: int = Field(ge=0)
    main_tid: int | None = Field(default=None, ge=0)
    main_itid: int | None = Field(default=None, ge=0)
    selection_reason: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    alternative_candidates: list[str] = Field(default_factory=list)


class TraceBoundary(StrictModel):
    name: str = Field(min_length=1)
    timestamp_ns: int = Field(ge=0)
    source: str = Field(min_length=1)
    source_id: str | None = None
    kind: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ProblemInterval(StrictModel):
    metric_definition: str = Field(min_length=1)
    start_boundary: TraceBoundary
    end_boundary: TraceBoundary
    duration_ms: float = Field(ge=0)
    selection_rule: str = Field(min_length=1)
    single_operation_assumption: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class ThreadStateBreakdown(StrictModel):
    running_ms: float = Field(ge=0)
    runnable_ms: float = Field(ge=0)
    sleeping_ms: float = Field(ge=0)
    uninterruptible_io_ms: float = Field(ge=0)
    uninterruptible_other_ms: float = Field(ge=0)
    other_ms: float = Field(ge=0)


class CpuExecutionShare(StrictModel):
    cpu: int = Field(ge=0)
    running_ms: float = Field(ge=0)
    share: float = Field(ge=0, le=1)
    schedule_slices: int = Field(ge=0)


class ThreadPriorityProfile(StrictModel):
    observed_values: list[int] = Field(default_factory=list)
    dominant_value: int | None = None
    interpretation: str


class SchedulingContentionInterval(StrictModel):
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    cpu: int | None = Field(default=None, ge=0)
    target_priority: int | None = None
    competing_process: str | None = None
    competing_thread: str | None = None
    competing_priority: int | None = None
    assessment: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)


class WakeupHop(StrictModel):
    depth: int = Field(ge=1, le=5)
    sleep_start_ns: int = Field(ge=0)
    sleep_end_ns: int = Field(ge=0)
    waiting_itid: int = Field(ge=0)
    waiting_thread: str | None = None
    enclosing_slice: str | None = None
    wakeup_ns: int | None = Field(default=None, ge=0)
    waker_itid: int | None = Field(default=None, ge=0)
    waker_process: str | None = None
    waker_thread: str | None = None
    waker_slice: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ThreadExecutionAnalysis(StrictModel):
    process_name: str = Field(min_length=1)
    thread_name: str | None = None
    pid: int = Field(ge=0)
    ipid: int = Field(ge=0)
    tid: int = Field(ge=0)
    itid: int = Field(ge=0)
    state_breakdown: ThreadStateBreakdown
    cpu_distribution: list[CpuExecutionShare] = Field(
        default_factory=list
    )
    cpu_migrations: int = Field(ge=0)
    schedule_slices: int = Field(ge=0)
    longest_running_ms: float = Field(ge=0)
    longest_runnable_ms: float = Field(ge=0)
    longest_sleep_ms: float = Field(ge=0)
    priority: ThreadPriorityProfile
    contention_intervals: list[SchedulingContentionInterval] = Field(
        default_factory=list
    )
    wakeup_chain: list[WakeupHop] = Field(default_factory=list)
    diagnosis: SchedulingDiagnosis
    assessment: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ColdStartStage(StrictModel):
    name: str = Field(min_length=1)
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    critical_threads: list[ThreadExecutionAnalysis] = Field(
        default_factory=list
    )
    assessment: str
    evidence_ids: list[str] = Field(default_factory=list)


class ColdStartAnalysis(StrictModel):
    resolved_process: ResolvedProcess
    cold_start_proven: bool
    classification_reason: str = Field(min_length=1)
    start_boundary: TraceBoundary
    end_boundary: TraceBoundary
    total_duration_ms: float = Field(
        ge=0,
        description=(
            "Exact milliseconds from start_boundary to end_boundary "
            "using the application/scenario metric definition selected "
            "for this run."
        ),
    )
    presentation_boundary: TraceBoundary | None = Field(
        default=None,
        description=(
            "Mapped render-service completion for the standardized technical "
            "first frame. It normally follows an application-frame boundary, "
            "but may precede an application-defined stable-home completion."
        ),
    )
    presentation_duration_ms: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Milliseconds from start_boundary to presentation_boundary; "
            "not the render-stage duration."
        ),
    )
    stages: list[ColdStartStage] = Field(default_factory=list)
    critical_path_summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class LatencyPhaseAnalysis(StrictModel):
    name: str = Field(min_length=1)
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    critical_threads: list[ThreadExecutionAnalysis] = Field(
        default_factory=list
    )
    assessment: str
    evidence_ids: list[str] = Field(default_factory=list)


class CompletionLatencyAnalysis(StrictModel):
    resolved_process: ResolvedProcess
    input_boundary: TraceBoundary
    response_boundary: TraceBoundary | None = None
    completion_boundary: TraceBoundary | None = None
    completion_proven: bool
    completion_semantics: str = Field(min_length=1)
    response_latency_ms: float | None = Field(default=None, ge=0)
    post_response_duration_ms: float | None = Field(default=None, ge=0)
    completion_latency_ms: float | None = Field(default=None, ge=0)
    phases: list[LatencyPhaseAnalysis] = Field(default_factory=list)
    critical_path_summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class PerfCollectionMetadata(StrictModel):
    config_names: list[str] = Field(default_factory=list)
    command_line: str | None = None
    scope: str = Field(min_length=1)
    sampling_frequency_hz: float | None = Field(default=None, gt=0)
    callstack_mode: str | None = None
    clock_id: str | None = None
    target_pids: list[int] = Field(default_factory=list)


class PerfHotspot(StrictModel):
    symbol: str = Field(min_length=1)
    file_path: str | None = None
    layer: str = "unknown"
    self_samples: int = Field(ge=0)
    inclusive_samples: int = Field(ge=0)
    self_event_count: int = Field(ge=0)
    inclusive_event_count: int = Field(ge=0)
    inclusive_share: float = Field(ge=0, le=1)
    critical_path_relevance: str
    evidence_ids: list[str] = Field(default_factory=list)


class PerfEventProfile(StrictModel):
    event_type_id: int = Field(ge=0)
    event_name: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    total_event_count: int = Field(ge=0)
    hotspots: list[PerfHotspot] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class PerfAnalysis(StrictModel):
    interval_start_ns: int = Field(ge=0)
    interval_end_ns: int = Field(ge=0)
    collection: PerfCollectionMetadata
    process_ids: list[int] = Field(default_factory=list)
    thread_ids: list[int] = Field(default_factory=list)
    sample_count: int = Field(ge=0)
    total_callchain_frames: int = Field(ge=0)
    symbolized_callchain_frames: int = Field(ge=0)
    symbolization_rate: float | None = Field(default=None, ge=0, le=1)
    events: list[PerfEventProfile] = Field(default_factory=list)
    assessment: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnalysisResult(StrictModel):
    summary: str
    problem_interval: ProblemInterval | None = None
    cold_start: ColdStartAnalysis | None = None
    completion_latency: CompletionLatencyAnalysis | None = None
    perf: PerfAnalysis | None = None
    findings: list[Finding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ColdStartStageDraft(StrictModel):
    """Semantic stage description; thread projections are hydrated later."""

    name: str = Field(min_length=1)
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    assessment: str
    evidence_ids: list[str] = Field(default_factory=list)


class ColdStartAnalysisDraft(StrictModel):
    resolved_process: ResolvedProcess
    cold_start_proven: bool
    classification_reason: str = Field(min_length=1)
    start_boundary: TraceBoundary
    end_boundary: TraceBoundary
    total_duration_ms: float = Field(ge=0)
    presentation_boundary: TraceBoundary | None = None
    presentation_duration_ms: float | None = Field(default=None, ge=0)
    stages: list[ColdStartStageDraft] = Field(default_factory=list)
    critical_path_summary: str
    evidence_ids: list[str] = Field(default_factory=list)

    def to_analysis(self) -> ColdStartAnalysis:
        payload = self.model_dump()
        payload["stages"] = [
            {
                **stage.model_dump(),
                "critical_threads": [],
            }
            for stage in self.stages
        ]
        return ColdStartAnalysis.model_validate(payload)


class LatencyPhaseDraft(StrictModel):
    """Semantic phase description; deterministic threads stay out of LLM IO."""

    name: str = Field(min_length=1)
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    duration_ms: float = Field(ge=0)
    assessment: str
    evidence_ids: list[str] = Field(default_factory=list)


class CompletionLatencyAnalysisDraft(StrictModel):
    resolved_process: ResolvedProcess
    input_boundary: TraceBoundary
    response_boundary: TraceBoundary | None = None
    completion_boundary: TraceBoundary | None = None
    completion_proven: bool
    completion_semantics: str = Field(min_length=1)
    response_latency_ms: float | None = Field(default=None, ge=0)
    post_response_duration_ms: float | None = Field(default=None, ge=0)
    completion_latency_ms: float | None = Field(default=None, ge=0)
    phases: list[LatencyPhaseDraft] = Field(default_factory=list)
    critical_path_summary: str
    evidence_ids: list[str] = Field(default_factory=list)

    def to_analysis(self) -> CompletionLatencyAnalysis:
        payload = self.model_dump()
        payload["phases"] = [
            {
                **phase.model_dump(),
                "critical_threads": [],
            }
            for phase in self.phases
        ]
        return CompletionLatencyAnalysis.model_validate(payload)


class AgentAnalysisDraft(StrictModel):
    """Small LLM contract; deterministic transport fields are merged later."""

    summary: str
    problem_interval: ProblemInterval | None = None
    cold_start: ColdStartAnalysisDraft | None = None
    completion_latency: CompletionLatencyAnalysisDraft | None = None
    findings: list[Finding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def to_analysis_result(self) -> AnalysisResult:
        return AnalysisResult(
            summary=self.summary,
            problem_interval=self.problem_interval,
            cold_start=(
                self.cold_start.to_analysis()
                if self.cold_start is not None
                else None
            ),
            completion_latency=(
                self.completion_latency.to_analysis()
                if self.completion_latency is not None
                else None
            ),
            perf=None,
            findings=self.findings,
            limitations=self.limitations,
        )


class ValidationIssue(StrictModel):
    path: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ValidationReport(StrictModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)


class SkillSnapshot(StrictModel):
    name: str
    fingerprint: str


class RunStepState(StrictModel):
    step: str
    status: StepStatus
    input_hash: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobError(StrictModel):
    code: str
    category: str
    stage: str | None = None
    retryable: bool = False
    resumable: bool = False
    user_message: str
    suggested_action: str
    trace_id: str | None = None
    internal_detail: str | None = None


class RunManifest(StrictModel):
    run_id: str
    trace_id: str
    status: RunStatus
    agent: AgentKind
    provider: LlmProvider | None = None
    model: str | None = None
    scenario_type: ScenarioType
    scenario: str
    symptom: str
    device: str | None = None
    build: str | None = None
    time_range: str | None = None
    target_process: str | None = None
    operation_marker: str | None = None
    start_marker: str | None = None
    end_marker: str | None = None
    response_marker: str | None = None
    completion_marker: str | None = None
    problem_duration_ms: float | None = None
    refresh_rate_hz: float | None = None
    trace_path: str
    baseline_trace_path: str | None = None
    database_path: str | None = None
    baseline_database_path: str | None = None
    trace_conversions: list[TraceConversionRecord] = Field(
        default_factory=list
    )
    skills: list[SkillSnapshot] = Field(default_factory=list)
    trace_capabilities: list[TraceCapability] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    step_states: list[RunStepState] = Field(default_factory=list)
    resume_from: str | None = None
    attempt_id: str | None = None
    last_heartbeat_at: datetime | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    error: str | None = None


class RunResult(StrictModel):
    run_id: str
    output_dir: Path
    report_path: Path
    analysis: AnalysisResult
