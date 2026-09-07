from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from trace_agent.models import FindingSeverity, StrictModel, utc_now


class MemoryKind(StrEnum):
    """Kinds of curated long-term memory entries."""

    PATTERN = "pattern"
    FACT = "fact"
    PROCEDURE = "procedure"
    ANTI_PATTERN = "anti-pattern"


class MemoryStatus(StrEnum):
    """Lifecycle of a curated memory, from candidate to archived."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    CONSOLIDATED = "consolidated"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


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
    occurrences: int = Field(default=1, ge=1)
    provenance: Provenance
    superseded_by: str | None = None
    archived_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RecallHit(StrictModel):
    """One ranked memory match for conditional recall."""

    memory: CuratedMemory | EpisodicMemory
    score: float = Field(ge=0)
    why: str = ""


class RecallResult(StrictModel):
    """Complete conditional recall answer with traceable provenance."""

    hits: list[RecallHit] = Field(default_factory=list)
    query: str = ""
    context_prompt: str = ""

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
    messages: list[SessionMessage] = Field(default_factory=list)
    scenario_type: str | None = None
    device: str | None = None
    build: str | None = None
    status: str = "open"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
