"""Compatibility shim for the pre-P2 location of the quality gate.

P2 grouped the lifecycle rules under :mod:`trace_agent.memory.lifecycle`. The
decision function itself did not change, and callers - including the test suite
written against the P0 layout - import it from here, so the old path keeps
working as a thin re-export rather than a second copy that could drift.
"""

from __future__ import annotations

from trace_agent.memory.lifecycle.quality_gate import (
    DERIVED_DIGEST_KINDS,
    MAX_CONTRADICTIONS,
    PROJECT_STATUS_REASON,
    SUMMARY_STATUS_REASON,
    GateEvidence,
    apply_trust_gate,
    evaluate_trust,
    evidence_for,
    normalize_validator,
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
