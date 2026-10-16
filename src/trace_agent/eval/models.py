from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from trace_agent.models import (
    AnalysisResult,
    ScenarioType,
    StrictModel,
    utc_now,
)


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class MetricGold(StrictModel):
    """Expected numeric metric plus a deterministic tolerance window."""

    value: float
    tolerance: float = Field(default=0.0, ge=0.0)
    tolerance_ms: float | None = Field(default=None, ge=0.0)

    @property
    def effective_tolerance(self) -> float:
        if self.tolerance_ms is not None:
            return self.tolerance_ms
        return self.tolerance


class RootCauseGold(StrictModel):
    """Correctness model for one expected root cause.

    Supports both the legacy ``type``/``component`` shape and the newer
    ``canonical``/``aliases`` shape.  ``canonical`` is the normalized concept
    ID; ``aliases`` are natural-language surface forms that also map to it and
    are the deterministic fallback when the agent only emits free text.
    """

    id: str = Field(min_length=1)
    type: str | None = None
    category: str | None = None
    canonical: str | None = None
    component: str | None = None
    acceptable_components: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return (
            self.canonical or self.type or self.category or self.id or ""
        ).lower()

    @property
    def match_terms(self) -> list[str]:
        """Canonical ID plus every surface form that maps to this concept."""
        values: list[str] = []
        for value in (
            self.canonical,
            self.type,
            self.category,
            self.component,
        ):
            if value:
                values.append(value)
        values.extend(self.acceptable_components)
        values.extend(self.aliases)
        return values


class BottleneckGold(StrictModel):
    type: str | None = None
    category: str | None = None
    canonical: str | None = None
    component: str | None = None
    stage: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return (
            self.canonical or self.type or self.category or self.stage or ""
        ).lower()

    @property
    def match_terms(self) -> list[str]:
        values: list[str] = []
        for value in (
            self.canonical,
            self.type,
            self.category,
            self.component,
            self.stage,
        ):
            if value:
                values.append(value)
        values.extend(self.aliases)
        return values


class EvidenceGold(StrictModel):
    event: str | None = None
    name: str | None = None
    type: str | None = None
    thread: str | None = None
    relation: str | None = None

    @property
    def subject(self) -> str:
        return (self.event or self.name or "").lower()


class UncertaintyGold(StrictModel):
    """Structured uncertainty topic instead of a literal expected sentence.

    ``topic`` is a canonical concept ID; ``aliases`` are trigger phrases that
    may appear in the agent's ``limitations`` free text.  The grader compares
    topics, never long Chinese sentences.
    """

    topic: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return self.topic.lower()

    @property
    def match_terms(self) -> list[str]:
        values = [self.topic]
        values.extend(self.aliases)
        return values


class Gold(StrictModel):
    """Correctness model, not a reference answer.

    Fields map to the weighted diagnosis domains used by the deterministic
    grader: root causes, bottlenecks, evidence, metrics and uncertainty.
    """

    root_causes: list[RootCauseGold] = Field(default_factory=list)
    bottlenecks: list[BottleneckGold] = Field(default_factory=list)
    evidence: list[EvidenceGold] = Field(default_factory=list)
    metrics: dict[str, MetricGold] = Field(default_factory=dict)
    uncertainty: list[str] = Field(default_factory=list)
    uncertainty_topics: list[UncertaintyGold] = Field(default_factory=list)
    uncertainty_status: str | None = None
    forbidden_conclusions: list[str] = Field(default_factory=list)
    severity: str | None = None


class CaseInput(StrictModel):
    """User-facing inputs that map to an :class:`AnalyzeRequest`.

    ``trace`` and ``type`` are the case-file spellings for the production
    request fields ``trace_path`` and ``scenario_type``.  Everything below
    mirrors the corresponding CLI inputs so a case can be written exactly the
    way a user would describe the analysis task.
    """

    trace: Path
    type: ScenarioType
    scenario: str = Field(min_length=1)
    symptom: str = Field(min_length=1)
    device: str | None = None
    build: str | None = None
    time_range: str | None = None
    target_process: str | None = None
    operation_marker: str | None = None
    start_marker: str | None = None
    end_marker: str | None = None
    response_marker: str | None = None
    completion_marker: str | None = None
    problem_duration_ms: float | None = Field(default=None, gt=0, le=3_600_000)
    refresh_rate_hz: float | None = Field(default=None, gt=0)
    baseline_trace_path: Path | None = None


class CaseMetadata(StrictModel):
    suite: str = "regression"
    difficulty: Difficulty = Difficulty.MEDIUM
    tags: list[str] = Field(default_factory=list)


class Case(StrictModel):
    """A single reproducible eval case loaded from disk."""

    id: str = Field(min_length=1)
    input: CaseInput
    metadata: CaseMetadata = Field(default_factory=CaseMetadata)
    gold: Gold = Field(default_factory=Gold)

    @property
    def suite(self) -> str:
        return self.metadata.suite


class AgentOutput(StrictModel):
    """Machine-gradeable agent output captured from one trial.

    ``result`` is the production ``AnalysisResult`` produced through the same
    ``AnalyzeApplication -> AgentRuntime -> Transport`` entry point used by the
    CLI/Web path.  ``raw`` preserves provider/transport telemetry verbatim.
    """

    result: AnalysisResult
    raw: dict[str, Any] = Field(default_factory=dict)


class TrialMemoryHit(StrictModel):
    """One recalled memory as it reached a trial's prompt.

    Mirrors :class:`trace_agent.memory.models.MemoryTraceHit` rather than reusing
    it: the eval artifact must stay loadable from disk without the memory package
    being importable, and ``eval-run.json`` is a report, not a live handle.
    """

    memory_id: str
    trust: str = ""
    kind: str = ""
    score: float = Field(default=0.0, ge=0)
    lexical: float = Field(default=0.0, ge=0, le=1)
    semantic: float = Field(default=0.0, ge=0, le=1)
    injected: bool = False


class ContaminatedRecord(StrictModel):
    """One injected record that was minted inside this very eval run."""

    record_id: str
    record_kind: str = "curated"
    trust: str = ""
    episode_id: str = ""
    reason: str = ""


class MemoryContamination(StrictModel):
    """Self-contamination measurement for one trial (section 8.4).

    ``contaminated`` counts the unverified records a trial was shown that came
    from another trial of the same run - the number section 14 targets at zero.
    ``trusted_from_same_run`` is reported separately because it is a *worse*
    finding, not a milder one: it means the gate promoted something inside a
    single experiment, so the trials were never independent to begin with.
    """

    eval_run_id: str = ""
    injected: int = Field(default=0, ge=0)
    contaminated: int = Field(default=0, ge=0)
    trusted_from_same_run: int = Field(default=0, ge=0)
    rate: float = Field(default=0.0, ge=0, le=1)
    clean: bool = True
    items: list[ContaminatedRecord] = Field(default_factory=list)
    error: str = ""

    @property
    def same_run_total(self) -> int:
        return self.contaminated + self.trusted_from_same_run


class TrialMemory(StrictModel):
    """Everything one trial's memory usage tells the report.

    Built from the ``memory_trace`` the run wrote into ``run.json``, which is why
    the fields are a projection of
    :class:`trace_agent.memory.models.MemoryTrace` plus the two eval-only
    additions: the grade this trial earned, and the contamination audit of what
    it was shown. Without this, "did memory help or did memory leak" is
    unanswerable from an eval artifact.
    """

    enabled: bool = True
    query: str = ""
    namespace: str = ""
    store: str = ""
    episode_id: str | None = None
    context_chars: int = Field(default=0, ge=0)
    budget_chars: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    injected_ids: list[str] = Field(default_factory=list)
    trust_mix: dict[str, int] = Field(default_factory=dict)
    hits: list[TrialMemoryHit] = Field(default_factory=list)
    created: list[str] = Field(default_factory=list)
    trust_changed: list[str] = Field(default_factory=list)
    #: Trust transitions the post-grade gate review produced, kept as raw dicts
    #: so the report can show ``candidate -> verified`` with its reason.
    gate_transitions: list[dict[str, Any]] = Field(default_factory=list)
    graded_pass: bool | None = None
    graded_score: float | None = Field(default=None, ge=0, le=1)
    contamination: MemoryContamination | None = None

    @property
    def injected_trust_mix(self) -> dict[str, int]:
        """Trust distribution of what the model saw, not of what recall found."""
        mix: dict[str, int] = {}
        for hit in self.hits:
            if not hit.injected:
                continue
            mix[hit.trust] = mix.get(hit.trust, 0) + 1
        return mix

    @classmethod
    def from_trace(cls, trace: Any) -> TrialMemory | None:
        """Project a ``run.json`` ``memory_trace`` payload, tolerating junk.

        Defensive on purpose. The trace is written by a best-effort subsystem and
        read back from a file that may predate a field or have been truncated by
        a crashed run; an eval report that cannot load because one memory key was
        malformed would lose every other measurement in the run with it.
        """
        if not isinstance(trace, dict):
            return None
        raw_hits = trace.get("hits")
        hits: list[TrialMemoryHit] = []
        if isinstance(raw_hits, list):
            for item in raw_hits:
                if not isinstance(item, dict) or not item.get("memory_id"):
                    continue
                hits.append(
                    TrialMemoryHit(
                        memory_id=str(item["memory_id"]),
                        trust=str(item.get("trust") or ""),
                        kind=str(item.get("kind") or ""),
                        score=_as_float(item.get("score")),
                        lexical=_as_unit_float(item.get("lexical")),
                        semantic=_as_unit_float(item.get("semantic")),
                        injected=bool(item.get("injected")),
                    )
                )
        injected = [hit.memory_id for hit in hits if hit.injected]
        raw_ids = trace.get("injected_ids")
        if not injected and isinstance(raw_ids, list):
            injected = [str(item) for item in raw_ids if item]
        return cls(
            enabled=bool(trace.get("enabled", True)),
            query=str(trace.get("query") or ""),
            namespace=str(trace.get("namespace") or ""),
            store=str(trace.get("store") or ""),
            episode_id=(
                str(trace["episode_id"]) if trace.get("episode_id") else None
            ),
            context_chars=_as_int(trace.get("context_chars")),
            budget_chars=_as_int(trace.get("budget_chars")),
            latency_ms=_as_float(trace.get("latency_ms")),
            injected_ids=injected,
            trust_mix=_as_int_map(trace.get("trust_mix")),
            hits=hits,
            created=_as_str_list(trace.get("created")),
            trust_changed=_as_str_list(trace.get("trust_changed")),
        )


def _as_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _as_unit_float(value: Any) -> float:
    """Clamp a component score into the ``[0, 1]`` its field declares.

    ``_as_float`` only floors at zero, and :class:`TrialMemoryHit` bounds
    ``lexical``/``semantic`` at one. Without the ceiling a trace carrying an
    out-of-range component would raise inside :meth:`TrialMemory.from_trace`,
    which is the exact failure that method exists to prevent: the runner calls
    it inside the trial's ``try`` block, so one malformed memory key would
    reclassify an otherwise successful trial as an error.
    """
    return min(1.0, _as_float(value))


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _as_int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _as_int(item) for key, item in value.items()}


class TrialStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"


class TrialFailure(StrEnum):
    """Why a trial did not produce a valid outcome observation.

    ``success`` means the trial produced a gradeable outcome (it may still
    fail outcome gates).  The remaining values partition failure modes so the
    multi-trial aggregator can exclude non-agent noise and evaluator bugs
    instead of silently contaminating ``pass@k`` / ``pass^k``:

    * ``agent_failure``: the agent/provider boundary failed on a valid case.
    * ``infra_failure``: environment, OS, runtime, sandbox, or resource issue.
    * ``timeout``: the trial exceeded its execution budget.
    * ``invalid``: the case/trial data is malformed or does not satisfy
      execution preconditions.
    * ``eval_failure``: the harness, grader, or evaluator itself has a bug.
    """

    SUCCESS = "success"
    AGENT_FAILURE = "agent_failure"
    INFRA_FAILURE = "infra_failure"
    TIMEOUT = "timeout"
    INVALID = "invalid"
    EVAL_FAILURE = "eval_failure"


class Trial(StrictModel):
    """A first-class, isolated execution of one case.

    Multi-trial is a V2 concern, but the harness treats trial as the unit of
    observation from day one so repeated runs never require a harness rewrite.
    """

    trial_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    index: int = Field(default=0, ge=0)
    status: TrialStatus = TrialStatus.RUNNING
    failure: TrialFailure | None = None
    output: AgentOutput | None = None
    elapsed_ms: float | None = Field(default=None, ge=0)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    #: Memory side of this trial, ``None`` when memory was off or the run wrote
    #: no trace. Kept on the trial and not only on the aggregate so a single
    #: contaminated trial stays identifiable.
    memory: TrialMemory | None = None


class ComponentScore(StrictModel):
    component: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    weight: float = Field(ge=0, le=1)
    notes: list[str] = Field(default_factory=list)


class GraderResult(StrictModel):
    """Weighted component scores plus the aggregate overall score.

    The system stores per-component scores (diagnosis, evidence, metrics,
    reasoning, uncertainty, report, efficiency) rather than collapsing to a
    single number, so capability regressions stay diagnosable.
    """

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    overall: float = Field(ge=0, le=1)
    components: list[ComponentScore] = Field(default_factory=list)


class EvalRun(StrictModel):
    """Durable aggregation of one or more trials for one or more cases."""

    run_id: str = Field(min_length=1)
    trials: list[Trial] = Field(default_factory=list)
    grader_results: list[GraderResult] = Field(default_factory=list)
    case_scores: dict[str, float] = Field(default_factory=dict)
    suite_scores: dict[str, float] = Field(default_factory=dict)
    multi_trial_results: dict[str, MultiTrialResult] = Field(default_factory=dict)
    artifact_dir: Path | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None



class HardGate(StrictModel):
    """Deterministic pass threshold applied to a component score.

    A gate fails when the named component score is strictly below
    ``minimum``.  Gates run after weighted scoring and never raise the
    aggregate correctness score; they can only downgrade a result to FAIL.
    """

    component: str = Field(min_length=1)
    minimum: float = Field(default=0.5, ge=0, le=1)


class GateCheck(StrictModel):
    component: str = Field(min_length=1)
    passed: bool
    score: float = Field(ge=0, le=1)
    minimum: float = Field(ge=0, le=1)
    message: str = ""


class TrajectoryCheck(StrictModel):
    code: str = Field(min_length=1)
    passed: bool
    message: str = ""


class TrajectoryResult(StrictModel):
    """Behavioral score derived from the trial tool/transcript trace.

    This is a separate layer from outcome correctness: tool usage is tracked
    here, never folded back into ``GraderResult.overall``.
    """

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    tool_calls: int = Field(ge=0)
    tool_errors: int = Field(ge=0)
    turns: int = Field(ge=0)
    invalid_actions: int = Field(ge=0)
    evidence_retrievals: int = Field(ge=0)
    repeated_calls: int = Field(default=0, ge=0)
    forbidden_calls: int = Field(default=0, ge=0)
    tool_names: list[str] = Field(default_factory=list)
    checks: list[TrajectoryCheck] = Field(default_factory=list)


class SemanticResult(StrictModel):
    """Model-based quality score for what deterministic rules cannot judge.

    Kept as a first-class layer so a downstream LLM judge can replace the
    deterministic heuristic without changing the aggregate interface.
    """

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    dimensions: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class LayerResult(StrictModel):
    """Aggregated output of the composite layered grader."""

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    outcome: GraderResult
    trajectory: TrajectoryResult | None = None
    semantic: SemanticResult | None = None
    gates: list[GateCheck] = Field(default_factory=list)
    passed: bool = True
    overall: float = Field(ge=0, le=1)

class TrialOutcome(StrictModel):
    """Outcome-correctness result for a single trial.

    Separates the ``grader`` score from the binary ``passed`` result produced
    by deterministic hard gates.  Gates can only downgrade a score to FAIL;
    they never change ``graded_score``.
    """

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    graded_score: float = Field(ge=0, le=1)
    passed: bool = False
    gates: list[GateCheck] = Field(default_factory=list)


class OperationalMetrics(StrictModel):
    """Runtime/process metrics for one trial, tracked independently of outcome.

    These describe how much work the agent did (turns/tools/tokens/latency/
    cost); they are never folded into correctness or trajectory quality.
    """

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tool_errors: int = Field(default=0, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    ttft_ms: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)


class TrialResult(StrictModel):
    """Full per-trial observation: outcome, trajectory, and operations.

    Trajectory and operational data are independent quality dimensions and are
    stored side-by-side with, never embedded into, outcome correctness.
    """

    case_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    index: int = Field(default=0, ge=0)
    status: TrialStatus = TrialStatus.COMPLETED
    failure: TrialFailure | None = None
    outcome: TrialOutcome | None = None
    trajectory: TrajectoryResult | None = None
    operational: OperationalMetrics | None = None
    error: str | None = None
    #: Same observation as ``Trial.memory``, carried onto the result so the
    #: aggregate view and the report can reach it without walking back into the
    #: raw trial list.
    memory: TrialMemory | None = None


class MultiTrialResult(StrictModel):
    """Aggregation over ``k`` independent trials of one case.

    ``pass@k`` is the probability that at least one trial passes; ``pass^k``
    requires every trial to pass and so measures stable success rather than
    best-case success.  Trajectory pass rate and operational aggregates are
    reported independently from outcome correctness.
    """

    case_id: str = Field(min_length=1)
    k: int = Field(default=1, ge=1)
    trial_results: list[TrialResult] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)
    pass_count: int = Field(default=0, ge=0)
    pass_at_1: float = Field(default=0, ge=0, le=1)
    pass_at_k: float = Field(default=0, ge=0, le=1)
    pass_power_k: float = Field(default=0, ge=0, le=1)
    mean_score: float = Field(default=0, ge=0, le=1)
    score_variance: float = Field(default=0, ge=0)
    trajectory_pass_rate: float | None = Field(default=None, ge=0, le=1)
    operational: OperationalMetrics | None = None


class GateThresholds(StrictModel):
    """Outcome hard-gate configuration for trial pass determination."""

    diagnosis: float = Field(default=0.5, ge=0, le=1)
    evidence: float = Field(default=0.5, ge=0, le=1)
    metrics: float = Field(default=0.5, ge=0, le=1)
    overall: float = Field(default=0.75, ge=0, le=1)
