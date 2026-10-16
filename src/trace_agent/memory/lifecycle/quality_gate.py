"""Evidence-driven trust promotion for curated memory.

The gate exists because a single analysis run must not be able to mint a
trusted prior. Re-consolidating the same run produces the same evidence and
therefore the same trust level, so repetition can never masquerade as
independent confirmation.

This module is pure: it reads a memory plus the evidence recorded about it and
returns a verdict. It never touches a store, which is what lets the same rules
be exercised from consolidation, from the dreaming beat and from a unit test
without a database. Persisting a verdict is :mod:`trace_agent.memory.lifecycle.promoter`'s
job, because section 7 requires the transition itself to be auditable and that
is a store concern.
"""

from __future__ import annotations

from dataclasses import dataclass

from trace_agent.memory.flags import (
    memory_gate_min_confidence,
    memory_gate_min_runs,
    memory_gate_verified_runs,
)
from trace_agent.memory.models import (
    RETIRED_STATUSES,
    CuratedMemory,
    MemoryKind,
    MemoryStatus,
)

# Independent contradictions tolerated before a memory is retired outright.
MAX_CONTRADICTIONS = 2

# Kinds that are *projections of records the store already holds* rather than
# claims about the world: a session digest is a compression of stored messages,
# a project digest is a roll-up of stored episodes and trust counts. The gate has
# no evidence model for either, because there is no "second run" that could
# corroborate that a transcript happened or that a corpus has the shape it has.
# Running one through the confirmation rules would leave it a permanent
# ``candidate`` that retention eventually archives, i.e. a feature that could
# only ever delete itself. They are therefore recorded as ``active`` on the
# strength of their own derivation, and stay archivable by the one rule that
# applies to everything.
DERIVED_DIGEST_KINDS: frozenset[MemoryKind] = frozenset(
    {MemoryKind.SUMMARY, MemoryKind.PROJECT}
)

SUMMARY_STATUS_REASON = (
    "session digest: projection of stored messages, not a claim requiring "
    "corroboration"
)
PROJECT_STATUS_REASON = (
    "project digest: roll-up of stored episodes and trust counts, not a claim "
    "requiring corroboration"
)

_DIGEST_STATUS_REASON: dict[MemoryKind, str] = {
    MemoryKind.SUMMARY: SUMMARY_STATUS_REASON,
    MemoryKind.PROJECT: PROJECT_STATUS_REASON,
}

# Older releases wrote the literal string ``"pending"`` into
# ``provenance.validated_by`` as a placeholder. Read naively that looks like an
# expert sign-off and would mint ``verified`` trust for unreviewed memories, so
# it is normalised away here rather than trusted.
_LEGACY_VALIDATION_SENTINELS = frozenset({"pending", "none", "null", ""})


def normalize_validator(validated_by: str | None) -> str | None:
    """The real human identity behind ``validated_by``, or ``None``.

    Public because two writers now have to agree on what counts as a signature:
    the gate reads it as the strongest evidence a memory can carry, and
    :func:`~trace_agent.memory.lifecycle.promoter.sign_off` writes it. A
    placeholder that one of them accepted and the other rejected would either
    mint ``verified`` trust for an unreviewed record or silently discard a real
    reviewer's name.
    """
    if validated_by is None:
        return None
    cleaned = validated_by.strip()
    if cleaned.lower() in _LEGACY_VALIDATION_SENTINELS:
        return None
    return cleaned


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """Everything the gate is allowed to look at when deciding trust."""

    distinct_runs: int
    confidence: float
    confirmed_runs: int
    validated_by: str | None = None
    contradictions: int = 0
    #: Independent runs recorded by a ``supports`` link rather than by this
    #: record's own provenance. Two analyses that worded the same conclusion
    #: differently stay two rows - merging them would need near-identical text -
    #: but they *are* corroboration, and without this the run threshold could
    #: only ever be met by byte-identical findings. Counted separately so the
    #: reason string can still say where the evidence came from.
    supporting_runs: int = 0
    #: Whether at least one source episode passed the deterministic eval grader
    #: (section 5.2 / 8.5). Carried in from the episodes rather than read here so
    #: the gate stays pure: it decides trust from evidence, it does not go and
    #: collect it.
    graded_pass: bool = False

    @property
    def corroborated_runs(self) -> int:
        """Own runs plus runs that agreed from a separate record."""
        return max(0, int(self.distinct_runs)) + max(0, int(self.supporting_runs))


def evaluate_trust(
    current: MemoryStatus,
    evidence: GateEvidence,
    *,
    min_runs: int | None = None,
    min_confidence: float | None = None,
) -> tuple[MemoryStatus, str]:
    """Return the trust level this memory has earned plus an auditable reason.

    Demotion is as important as promotion: a memory contradicted by enough
    independent runs is archived instead of lingering as a stale prior.
    """
    runs_required = memory_gate_min_runs() if min_runs is None else min_runs
    confidence_required = (
        memory_gate_min_confidence() if min_confidence is None else min_confidence
    )

    if current in RETIRED_STATUSES:
        return current, f"{current.value} memory is not auto-promoted"
    if evidence.contradictions >= MAX_CONTRADICTIONS:
        return (
            MemoryStatus.ARCHIVED,
            f"contradicted by {evidence.contradictions} independent runs",
        )
    if evidence.validated_by:
        return MemoryStatus.VERIFIED, f"expert sign-off by {evidence.validated_by}"
    if current is MemoryStatus.VERIFIED:
        return MemoryStatus.VERIFIED, "verification is sticky until contradicted"
    corroborated = evidence.corroborated_runs
    # ``active`` is the rung every automatic ``verified`` decision stands on:
    # section 5.2 lists the verified paths as ``active -> verified`` transitions,
    # and letting a record skip straight from a single-run guess to "may reorder
    # an investigation" would reintroduce exactly the self-contamination the gate
    # exists to prevent, only wearing a stronger label.
    active_earned = (
        corroborated >= runs_required
        and evidence.confidence >= confidence_required
        and evidence.confirmed_runs >= 1
    )
    if active_earned and evidence.contradictions == 0:
        verified_required = max(runs_required, memory_gate_verified_runs())
        if corroborated >= verified_required:
            return (
                MemoryStatus.VERIFIED,
                (
                    f"{corroborated} independent runs agree, zero contradictions "
                    f"(threshold {verified_required}, "
                    f"confidence {evidence.confidence:.2f})"
                ),
            )
        if evidence.graded_pass:
            # A grade says "this conclusion was right on the trace it was scored
            # against". It only generalises once the same conclusion has also
            # survived the corroboration rung, which is why it is checked inside
            # ``active_earned`` rather than as a standalone shortcut.
            return (
                MemoryStatus.VERIFIED,
                (
                    f"eval-graded pass over {corroborated} independent runs "
                    f"(confidence {evidence.confidence:.2f}, "
                    f"{evidence.confirmed_runs} confirmed)"
                ),
            )
    if active_earned:
        return (
            MemoryStatus.ACTIVE,
            (
                f"{corroborated} independent runs agree "
                f"(confidence {evidence.confidence:.2f}, "
                f"{evidence.confirmed_runs} confirmed"
                + (
                    f", {evidence.supporting_runs} via supports link"
                    if evidence.supporting_runs
                    else ""
                )
                + ")"
            ),
        )
    return (
        MemoryStatus.CANDIDATE,
        (
            f"gate not met: runs={corroborated}/{runs_required} "
            f"confidence={evidence.confidence:.2f}/{confidence_required:.2f} "
            f"confirmed_runs={evidence.confirmed_runs}"
            + (
                f" supporting_runs={evidence.supporting_runs}"
                if evidence.supporting_runs
                else ""
            )
        ),
    )


def evidence_for(
    memory: CuratedMemory,
    *,
    contradictions: int = 0,
    supporting_runs: int = 0,
    graded_pass: bool = False,
    confirmed_runs: int | None = None,
) -> GateEvidence:
    """Project a memory onto the evidence the gate is allowed to use."""
    return GateEvidence(
        distinct_runs=memory.distinct_runs,
        confidence=memory.confidence,
        # ``None`` means "only what this record itself cites", which is the
        # right default for a caller that has not looked at corroborating
        # neighbours. The promoter passes a group-wide count, because once a
        # ``supports`` link says two records are the same conclusion reached
        # independently, a confirmed finding on either side is evidence for it.
        confirmed_runs=(
            len(set(memory.confirmed_run_ids))
            if confirmed_runs is None
            else max(0, int(confirmed_runs))
        ),
        validated_by=normalize_validator(memory.provenance.validated_by),
        contradictions=contradictions,
        supporting_runs=supporting_runs,
        graded_pass=graded_pass,
    )


def apply_trust_gate(
    memory: CuratedMemory,
    *,
    contradictions: int = 0,
    supporting_runs: int = 0,
    graded_pass: bool = False,
    confirmed_runs: int | None = None,
) -> CuratedMemory:
    """Recompute ``status``/``occurrences`` from recorded evidence.

    Idempotent: applying it twice to the same memory is a no-op, which is what
    makes a repeated consolidation of one run safe.
    """
    if (
        memory.kind in DERIVED_DIGEST_KINDS
        and contradictions < MAX_CONTRADICTIONS
        and memory.status not in RETIRED_STATUSES
    ):
        return memory.model_copy(
            update={
                "status": MemoryStatus.ACTIVE,
                "status_reason": _DIGEST_STATUS_REASON[memory.kind],
                "occurrences": memory.distinct_runs,
            }
        )
    status, reason = evaluate_trust(
        memory.status,
        evidence_for(
            memory,
            contradictions=contradictions,
            supporting_runs=supporting_runs,
            graded_pass=graded_pass,
            confirmed_runs=confirmed_runs,
        ),
    )
    return memory.model_copy(
        update={
            "status": status,
            "status_reason": reason,
            "occurrences": memory.distinct_runs,
        }
    )


__all__ = [
    "DERIVED_DIGEST_KINDS",
    "MAX_CONTRADICTIONS",
    "PROJECT_STATUS_REASON",
    "SUMMARY_STATUS_REASON",
    "GateEvidence",
    "apply_trust_gate",
    "evaluate_trust",
    "evidence_for",
    "normalize_validator",
]
