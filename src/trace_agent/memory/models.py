from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from trace_agent.memory.flags import memory_project_id
from trace_agent.models import FindingSeverity, StrictModel, utc_now


class MemoryKind(StrEnum):
    """Kinds of curated long-term memory entries."""

    PATTERN = "pattern"
    FACT = "fact"
    PROCEDURE = "procedure"
    ANTI_PATTERN = "anti-pattern"
    #: Rolling digest of one session, produced by the dreaming beat rather than
    #: by consolidation. Distinct from ``fact`` because it is a *narrative*
    #: about what happened, not a claim about the world, and recall treats it
    #: as background rather than as a prior.
    SUMMARY = "summary"
    #: Stable device/build/process attributes observed across runs - section
    #: 2.3's ``PROFILE``. A ``fact`` is a claim about the world that has to be
    #: corroborated; a profile entry is metadata the run *reported about
    #: itself*, so it needs its own kind to carry that difference into recall
    #: (``MemoryType.PROFILE``) instead of competing with domain facts for the
    #: ``semantic`` quota.
    PROFILE = "profile"
    #: Rolling digest of the project's long-term state - section 2.3's
    #: ``PROJECT``. Produced by the dreaming beat the same way ``SUMMARY`` is,
    #: and for the same reason: it is a projection of records the store already
    #: holds, so it can be rebuilt deterministically and never has to be
    #: believed on trust.
    PROJECT = "project"


class MemoryStatus(StrEnum):
    """Lifecycle of a curated memory, from candidate to archived."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    VERIFIED = "verified"
    CONSOLIDATED = "consolidated"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


# Trust levels that may be injected as an ordinary prior. ``candidate`` is
# deliberately absent: a single-run guess must never carry the same weight as a
# pattern confirmed by independent runs.
TRUSTED_STATUSES: frozenset[MemoryStatus] = frozenset(
    {
        MemoryStatus.ACTIVE,
        MemoryStatus.VERIFIED,
        MemoryStatus.CONSOLIDATED,
    }
)

# Kept out of every recall path; only audit and governance tooling reads these.
RETIRED_STATUSES: frozenset[MemoryStatus] = frozenset(
    {
        MemoryStatus.SUPERSEDED,
        MemoryStatus.ARCHIVED,
    }
)


# Finding statuses that count as real evidence. ``suspected`` is a guess, and a
# guess must never be the thing that promotes another guess - nor the thing that
# lets a raw episode into a prompt as a "historical similar case".
#
# One definition, three consumers: ``episode.build_episode`` decides what goes
# into ``root_causes``, ``consolidation`` decides which runs are
# ``confirmed_run_ids`` for the gate, and ``rerank.episode_trust`` decides
# whether an episode may be recalled at all. They have to agree, because
# "confirmed" meaning two different things in two of them is how a guess ends up
# labelled as evidence.
CORROBORATED_FINDING_STATUSES: frozenset[str] = frozenset({"confirmed", "observed"})


class MemoryConfidence(StrEnum):
    """Human-readable trust level used during consolidation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Provenance(StrictModel):
    """Where a memory came from and why it is trusted.

    ``source_type`` is kept a free-form string on purpose: it can describe
    ``finding``, ``evidence``, ``user``, ``session``, or future sources without
    versioning the schema. ``references`` are opaque, durable locators such as
    run ids, evidence ids, or file paths.
    """

    source_type: str = Field(min_length=1)
    references: list[str] = Field(default_factory=list)
    created_by: str | None = None
    validated_by: str | None = None


class EpisodicMemory(StrictModel):
    """One completed analysis session, kept as a searchable episode.

    This is deliberately a projection of the durable result store, not a copy
    of all raw evidence. Large artifacts stay in ``results/<case>/``; the
    episode only needs enough structured context to support recall.
    """

    episode_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    #: Conversation this run belonged to, when it had one - section 4's
    #: ``episodes.session_id``. It is the link that makes "what did this session
    #: learn" answerable by a filter instead of by reading every episode's text,
    #: and ``None`` is a legitimate value: a bare CLI analysis has no session
    #: around it.
    session_id: str | None = None
    #: Project the trace was analysed for. Stamped from configuration rather than
    #: from the request, because :class:`~trace_agent.models.AnalyzeRequest`
    #: describes a trace and not a tenancy.
    project_id: str | None = None
    scenario_type: str = Field(min_length=1)
    scenario: str = ""
    symptom: str = ""
    device: str | None = None
    build: str | None = None
    target_process: str | None = None
    summary: str = ""
    root_causes: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str = "completed"
    output_dir: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    #: Eval provenance, section 8.5. ``None`` means "this episode was never
    #: graded", which is deliberately distinct from ``False`` ("graded and
    #: failed"): a production run has no grade at all, and treating the absence
    #: of one as a failure would demote every real-world episode.
    eval_run_id: str | None = None
    graded_pass: bool | None = None
    graded_score: float | None = None


class CuratedMemory(StrictModel):
    """A distilled, reusable fact or pattern promoted from episodes."""

    memory_id: str = Field(min_length=1)
    kind: MemoryKind
    scenario_type: str | None = None
    topic: str = Field(min_length=1)
    content: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    severity: FindingSeverity | None = None
    confidence: float = Field(ge=0, le=1)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    # ``occurrences`` is a legacy alias kept for on-disk compatibility and is
    # always rewritten from :attr:`distinct_runs`. It must never be incremented
    # per write: re-consolidating one run is not a second confirmation.
    occurrences: int = Field(default=1, ge=1)
    confirmed_run_ids: list[str] = Field(default_factory=list)
    status_reason: str | None = None
    provenance: Provenance
    superseded_by: str | None = None
    archived_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    # Retention deadline, owned by the record rather than by one adapter: the
    # SQL store mirrors it into an indexed column, and the JSON store has
    # nowhere else to put it. ``None`` means "no deadline has been decided
    # yet", which is not the same as "never expires" - the retention sweep is
    # what stamps one.
    expires_at: datetime | None = None

    # ------------------------------------------------------------- governance
    # Both are store-owned columns: the SQL adapters maintain them, and nothing
    # on the write path may set them (a consolidation that rewrote ``version``
    # would break the audit trail it is supposed to be feeding). They are
    # therefore excluded from serialisation, which keeps the JSON payload of a
    # record byte-identical before and after their introduction - the file
    # adapter has no version column to keep in step, and two adapters that dump
    # different payloads are two adapters whose recall cannot be compared.
    # Reads are backfilled by :meth:`MemoryStore.load_curated`.
    version: int | None = Field(default=None, ge=1, exclude=True)
    last_accessed_at: datetime | None = Field(default=None, exclude=True)

    @property
    def memory_type(self) -> "MemoryType":
        """Functional type of this record, derived from its kind.

        The plan keeps two orthogonal classifications: ``kind`` is the shape of
        the content (pattern / fact / procedure), ``type`` is its job in
        retrieval (semantic / procedural / summary). Deriving one from the other
        rather than storing both means a record cannot disagree with itself, and
        the mapping lives in exactly one place
        (:data:`~trace_agent.memory.ports.TYPE_FOR_KIND`).

        Imported inside the property: ``ports`` imports this module for the
        record types, so a module-level import here would be a cycle.
        """
        from trace_agent.memory.ports import memory_type_for

        return memory_type_for(self.kind)

    @property
    def trust(self) -> MemoryStatus:
        """Trust level, under the name section 7 uses for it.

        An alias for ``status`` rather than a second field: the on-disk corpus
        says ``status``, the SQL column says ``trust``, and code that reasons
        about injection weight should read as the contract it implements.
        """
        return self.status

    @property
    def importance(self) -> float:
        """Ranking weight derived from severity and kind.

        Not stored: :func:`~trace_agent.memory.rerank.importance_for` computes
        it from fields the record already carries, so mirroring it into a column
        would create a second source of truth that could go stale the moment the
        weighting changes.
        """
        from trace_agent.memory.rerank import importance_for

        return importance_for(self)

    @property
    def scope(self) -> dict[str, str | None]:
        """Isolation scope stamped into the ``memories.scope`` column.

        Owned by the record so the three store adapters cannot each invent a
        different scope shape - which is what a hardcoded dict per adapter would
        do, and it is invisible until two adapters disagree about what a scoped
        query matches. ``project_id`` comes from the environment/namespace
        derivation rather than from the record, because a memory has no project
        of its own; it belongs to whichever project the corpus was written for.
        """
        return {
            "scenario_type": self.scenario_type,
            "project_id": memory_project_id(),
            "device": None,
            "build": None,
            "process": None,
        }

    @property
    def distinct_runs(self) -> int:
        """Independent runs that produced this memory.

        Derived from ``provenance.references`` so it cannot drift from the
        recorded provenance. P1 moves this onto a ``memory_sources`` table with
        a ``UNIQUE(memory_id, run_id)`` constraint.
        """
        return max(1, len(self.source_run_ids))

    @property
    def source_run_ids(self) -> frozenset[str]:
        """Run ids behind this memory, parsed from ``run:<id>`` references."""
        return frozenset(
            ref.removeprefix("run:")
            for ref in self.provenance.references
            if ref.startswith("run:")
        )

    @property
    def source_episode_ids(self) -> frozenset[str]:
        """Episode ids behind this memory.

        Episode references are the only provenance entries without a ``kind:``
        prefix (``run:``, ``evidence:``), so they are identified by shape.
        """
        return frozenset(
            ref
            for ref in self.provenance.references
            if ":" not in ref and ref
        )


class RecallHit(StrictModel):
    """One ranked memory match for conditional recall."""

    memory: CuratedMemory | EpisodicMemory
    score: float = Field(ge=0)
    why: str = ""
    # Relevance component kept alongside the blended score. ``why`` already
    # renders it for humans, but observability needs the number: "was this hit
    # relevant, or did trust and recency carry an unrelated record over the
    # floor" is only answerable if the two are separable after the fact.
    lexical: float = Field(default=0.0, ge=0, le=1)
    #: Cosine against the query vector, ``0.0`` when the semantic channel is
    #: unavailable or the record has no stored vector. Kept separate from
    #: ``lexical`` for the same reason: a paraphrase hit that scores 0.05 lexical
    #: and 0.71 semantic is evidence the vector channel is doing its job, and
    #: collapsing the two into one number would hide exactly that.
    semantic: float = Field(default=0.0, ge=0, le=1)


class RecallResult(StrictModel):
    """Complete conditional recall answer with traceable provenance."""

    hits: list[RecallHit] = Field(default_factory=list)
    query: str = ""
    context_prompt: str = ""
    #: Ids that actually made it into ``context_prompt``.
    #:
    #: Not the same list as ``hits``: the composer enforces a character budget
    #: per trust partition and drops whole items that do not fit. Section 9's
    #: question - "was this memory in front of the model" - can only be answered
    #: from what was rendered, so the renderer reports it.
    injected_ids: list[str] = Field(default_factory=list)
    #: Wall-clock milliseconds this answer took, stamped by
    #: :meth:`~trace_agent.memory.recall.RecallService.recall` on the way out.
    #:
    #: Section 9 lists ``latency_ms`` as part of ``memory.recalled``, and the run
    #: already measures it around the call. Carrying it on the result is what
    #: lets a caller that did *not* wrap the call in a timer - the dashboard's
    #: recall probe, ``memory stats``, an eval trial - report the same number
    #: instead of measuring its own overhead and calling it retrieval cost. A
    #: cached answer reports the time the cache took, which is the honest
    #: reading: that is how long the caller waited.
    latency_ms: float = Field(default=0.0, ge=0)
    #: Whether the composer dropped hits it had ranked, under budget pressure.
    #:
    #: Section 9 asks for this alongside the hit list because "the right memory
    #: was recalled" and "the right memory was injected" are different claims,
    #: and the second one is the one that can change a conclusion. ``hits``
    #: longer than ``injected_ids`` implies it, but the composer also elides
    #: whole partitions whose heading alone did not fit, so the flag is carried
    #: rather than inferred.
    truncated: bool = False

class SessionRole(StrEnum):
    """Participants in a durable analysis conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SessionMessage(StrictModel):
    """One conversation turn stored inside a session memory."""

    role: SessionRole
    content: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    references: list[str] = Field(default_factory=list)


class SessionMemory(StrictModel):
    """Durable, project-scoped session/conversation memory."""

    session_id: str = Field(min_length=1)
    title: str = ""
    topic: str = ""
    #: Rolling summary of the turns below, maintained by the dreaming beat.
    #: Kept on the record (not only in a SQL column) so the file adapter can
    #: carry it too, and so a ``SUMMARY`` memory can cite its source verbatim.
    summary: str = ""
    messages: list[SessionMessage] = Field(default_factory=list)
    scenario_type: str | None = None
    device: str | None = None
    build: str | None = None
    status: str = "open"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    # --------------------------------------------------------------- ownership
    # Section 4's ``sessions`` columns. ``namespace`` says which corpus a session
    # lives in; these say whose it is. Stamped at creation from
    # :mod:`~trace_agent.memory.flags` so a dashboard that never passes them
    # still produces rows a project-scoped query can find, and so a CLI-only
    # deployment - which has no user directory to consult - writes ``None``
    # instead of failing a bookkeeping write.
    agent_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None


class ExecutionCheckpointSnapshot(StrictModel):
    """Read-only projection of durable execution state for resume/audit."""

    output_dir: str
    run_id: str | None = None
    status: str | None = None
    resume_from: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    resumable: bool = False


class RecallIntent(StrictModel):
    """Whether fast recall should be upgraded to deep recall."""

    deep: bool = False
    reason: str = ""


class DeepRecallResult(StrictModel):
    """A longer, structure-aware recall answer for history/time questions."""

    query: str = ""
    context_prompt: str = ""
    source_count: int = 0
    #: Ids behind the rendered lines, so a deep-recall prompt is as attributable
    #: as a fast-recall one. Sessions report their ``session_id``; the list is
    #: parallel to the chosen items, not to any ranking the caller can see.
    source_ids: list[str] = Field(default_factory=list)


class MemoryTraceHit(StrictModel):
    """One recalled memory as it reached the prompt.

    Section 9 exists because "did the recalled memory change the conclusion"
    cannot be answered from a character count. Recording the id, the trust
    level and the score per hit is what makes a contaminated run diagnosable
    after the fact - and what lets the eval report compute a self-contamination
    rate instead of guessing one.
    """

    memory_id: str
    trust: str = ""
    kind: str = ""
    score: float = 0.0
    lexical: float = 0.0
    #: Semantic share of the hit's relevance, mirrored from ``RecallHit``. A run
    #: whose prompt was assembled entirely from lexical matches is distinguishable
    #: after the fact from one carried by embeddings, which is what makes "did the
    #: semantic channel change this conclusion" answerable from ``run.json``.
    semantic: float = 0.0
    injected: bool = False


class MemoryTrace(StrictModel):
    """Per-run record of what memory was recalled and what actually landed."""

    query: str = ""
    namespace: str = ""
    store: str = ""
    enabled: bool = True
    hits: list[MemoryTraceHit] = Field(default_factory=list)
    trust_mix: dict[str, int] = Field(default_factory=dict)
    context_chars: int = 0
    budget_chars: int = 0
    latency_ms: float = 0.0
    #: Mirrored from :attr:`RecallResult.truncated`: recall found more than the
    #: prompt budget could carry. Kept on the trace so a run whose conclusion
    #: changed can be checked for "was the decisive memory the one that got
    #: dropped" without re-running recall against the corpus as it was then.
    truncated: bool = False
    episode_id: str | None = None
    created: list[str] = Field(default_factory=list)
    trust_changed: list[str] = Field(default_factory=list)

    @property
    def injected_ids(self) -> list[str]:
        """Ids that actually reached the prompt, in render order.

        Derived from the per-hit flag rather than stored separately so the two
        cannot disagree: the composer drops whole items under budget pressure,
        and a stale list here would claim an injection that never happened.
        """
        return [hit.memory_id for hit in self.hits if hit.injected]

    @property
    def injected_trust_mix(self) -> dict[str, int]:
        """Trust distribution of what the model saw, not of what recall found.

        ``trust_mix`` counts every hit; this counts only the injected ones. The
        difference is the self-contamination signal section 14 asks for: a run
        whose prompt is mostly ``candidate`` is a run whose conclusion should be
        re-read with that in mind.
        """
        mix: dict[str, int] = {}
        for hit in self.hits:
            if not hit.injected:
                continue
            mix[hit.trust] = mix.get(hit.trust, 0) + 1
        return mix
