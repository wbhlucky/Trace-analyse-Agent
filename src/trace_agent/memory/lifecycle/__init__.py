"""Governance for long-term memory: what it may claim, and for how long.

Four modules, one responsibility each, and the split matters because they run at
different times and must not be able to overrule one another by accident:

:mod:`~trace_agent.memory.lifecycle.quality_gate`
    Pure rules. Evidence in, verdict out, no store access - which is why the
    same thresholds can be exercised from consolidation, from the dreaming beat
    and from a unit test without a database.
:mod:`~trace_agent.memory.lifecycle.promoter`
    The only writer allowed to *persist* a verdict, and the only writer allowed
    to record a human one: :func:`~trace_agent.memory.lifecycle.promoter.sign_off`
    is section 5.2's expert route to ``verified``. Section 7's rule that every
    trust transition carries a reason and an actor lives here.
:mod:`~trace_agent.memory.lifecycle.conflicts`
    Decides that two records disagree or say the same thing, and turns that into
    links the gate can then read as evidence.
:mod:`~trace_agent.memory.lifecycle.retention`
    Decides that a record has outlived its usefulness.
:mod:`~trace_agent.memory.lifecycle.contamination`
    Read-only audit of what an eval trial was shown, answering "was this prior
    minted by the experiment that is now scoring it".

Ordering is deliberate: conflicts and retention *produce evidence*, the gate
*judges* it, and the promoter *records* the judgement. Nothing in this package
sets a status directly except through :func:`~trace_agent.memory.lifecycle.promoter.commit_gate`
or :meth:`~trace_agent.memory.ports.MemoryStore.set_trust`, both of which write
a version row.
"""

from __future__ import annotations

from trace_agent.memory.lifecycle.conflicts import (
    CONFLICT_ACTOR,
    CONFLICT_CONFIDENCE_PENALTY,
    ConflictDetector,
    ConflictPair,
    ConflictReport,
)
from trace_agent.memory.lifecycle.contamination import (
    ContaminatedItem,
    ContaminationAudit,
    audit_injection,
)
from trace_agent.memory.lifecycle.promoter import (
    EXPERT_ACTOR_PREFIX,
    GATE_ACTOR,
    Corroboration,
    Promoter,
    SignOff,
    TrustTransition,
    commit_gate,
    confirmed_runs_for,
    contradictions_for,
    corroboration_for,
    graded_pass_for,
    revoke_sign_off,
    sign_off,
    supporting_runs_for,
)
from trace_agent.memory.lifecycle.quality_gate import (
    MAX_CONTRADICTIONS,
    GateEvidence,
    apply_trust_gate,
    evaluate_trust,
    evidence_for,
    normalize_validator,
)
from trace_agent.memory.lifecycle.retention import (
    RETENTION_ACTOR,
    RetentionReport,
    RetentionService,
)

__all__ = [
    "CONFLICT_ACTOR",
    "CONFLICT_CONFIDENCE_PENALTY",
    "EXPERT_ACTOR_PREFIX",
    "GATE_ACTOR",
    "MAX_CONTRADICTIONS",
    "RETENTION_ACTOR",
    "ConflictDetector",
    "ConflictPair",
    "ConflictReport",
    "ContaminatedItem",
    "ContaminationAudit",
    "Corroboration",
    "GateEvidence",
    "Promoter",
    "RetentionReport",
    "RetentionService",
    "SignOff",
    "TrustTransition",
    "apply_trust_gate",
    "audit_injection",
    "commit_gate",
    "confirmed_runs_for",
    "contradictions_for",
    "corroboration_for",
    "evaluate_trust",
    "evidence_for",
    "graded_pass_for",
    "normalize_validator",
    "revoke_sign_off",
    "sign_off",
    "supporting_runs_for",
]
