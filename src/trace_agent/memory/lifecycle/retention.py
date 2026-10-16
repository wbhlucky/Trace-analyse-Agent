"""Deciding when a memory stops being worth recalling.

The other half of section 5.4. The quality gate decides whether a memory has
*earned* trust; retention decides whether it has *kept* it. A corpus that can
only grow eventually drowns recall in priors nobody has believed in for a year,
and the failure is invisible: every stale record still matches its old query,
still carries its old confidence, and still spends prompt budget.

Two rules, both configured in :mod:`trace_agent.memory.flags`:

``candidate`` TTL
    A single-run guess that never earned a second run is the most dangerous kind
    of memory, so it gets the shorter leash. Once it is older than
    :func:`~trace_agent.memory.flags.memory_candidate_ttl_days` and still below
    the gate's run threshold, it is archived outright - waiting forever for a
    confirmation that will never arrive helps nobody.

general retention
    Everything else is stamped with ``expires_at = updated_at +
    retention_days`` rather than deleted on sight. Stamping first means an
    operator can read the deadline off the record, and it means a memory that
    gets re-confirmed (``updated_at`` moves) pushes its own deadline out.
    :meth:`MemoryStore.expire` then archives whatever has passed it.

Nothing here deletes a row. Section 7 keeps retired memory auditable, and an
archived record is still readable by ``memory show`` and by the dashboard.

Stamping goes through :meth:`MemoryStore.import_curated` on purpose. That is
the one write which preserves ``updated_at``: freshness is a ranking signal, so
a periodic sweep that restamped every record would make the whole corpus look
brand new, quietly reorder recall, and push every deadline a further year out -
retention that never retains anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from trace_agent.memory.flags import (
    memory_candidate_ttl_days,
    memory_gate_min_runs,
    memory_retention_days,
)
from trace_agent.memory.lifecycle.promoter import TrustTransition
from trace_agent.memory.models import (
    RETIRED_STATUSES,
    CuratedMemory,
    MemoryStatus,
)
from trace_agent.memory.ports import MemoryStore
from trace_agent.models import utc_now

# Recorded in ``memory_versions.actor`` so a retention archive is
# distinguishable from a gate archive and from a conflict-detector archive.
RETENTION_ACTOR = "retention"

# Tolerance for "same deadline". Timestamps round-trip through ISO strings and
# through SQLite TEXT columns, so comparing exactly would restamp every record
# on every sweep and defeat the point of using ``import_curated``.
_STAMP_TOLERANCE = timedelta(seconds=1)


@dataclass(slots=True)
class RetentionReport:
    """What one sweep looked at and what it changed.

    ``stamped`` and ``stale_candidates`` are separated because they mean
    different things to an operator: the first is bookkeeping that will matter
    in a year, the second just removed a record from recall.
    """

    scanned: int = 0
    stamped: list[str] = field(default_factory=list)
    stale_candidates: list[str] = field(default_factory=list)
    expired: int = 0
    transitions: list[TrustTransition] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection for events, ``run.json`` and the dashboard."""
        return {
            "scanned": self.scanned,
            "stamped": len(self.stamped),
            "stale_candidates": list(self.stale_candidates),
            "expired": self.expired,
            "transitions": [item.as_dict() for item in self.transitions],
        }


class RetentionService:
    """Stamps retention deadlines and archives what has outlived them."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        actor: str = RETENTION_ACTOR,
        retention_days: float | None = None,
        candidate_ttl_days: float | None = None,
        min_runs: int | None = None,
    ) -> None:
        self._store = store
        self._actor = actor
        # ``None`` means "read the environment on every sweep", so an operator
        # can retune retention without rebuilding the service.
        self._retention_days = retention_days
        self._candidate_ttl_days = candidate_ttl_days
        self._min_runs = min_runs

    def run(self, *, now: datetime | None = None) -> RetentionReport:
        """One idempotent sweep. Safe to fire from the dreaming beat."""
        reference = _as_utc(now or utc_now())
        retention_days = (
            memory_retention_days()
            if self._retention_days is None
            else self._retention_days
        )
        ttl_days = (
            memory_candidate_ttl_days()
            if self._candidate_ttl_days is None
            else self._candidate_ttl_days
        )
        min_runs = (
            memory_gate_min_runs() if self._min_runs is None else self._min_runs
        )

        report = RetentionReport()
        try:
            memories = self._store.list_curated(active_only=False)
        except Exception:
            return report

        for memory in memories:
            report.scanned += 1
            if memory.status in RETIRED_STATUSES:
                # Already out of recall. Re-stamping a retired record would only
                # churn ``updated_at``-derived state for nothing.
                continue
            if self._archive_stale_candidate(
                memory, reference, ttl_days, min_runs, report
            ):
                continue
            self._stamp(memory, reference, retention_days, report)

        try:
            report.expired = int(self._store.expire(reference))
        except Exception:
            report.expired = 0
        return report

    # ------------------------------------------------------------------ internals
    def _archive_stale_candidate(
        self,
        memory: CuratedMemory,
        reference: datetime,
        ttl_days: float,
        min_runs: int,
        report: RetentionReport,
    ) -> bool:
        """Retire a single-run guess that never earned corroboration."""
        if ttl_days <= 0:
            return False
        if memory.status is not MemoryStatus.CANDIDATE:
            return False
        if memory.distinct_runs >= min_runs:
            # Corroborated enough that the gate should have moved it already. If
            # it did not, the gate's own verdict is the interesting fact and
            # retention must not second-guess it.
            return False
        age_days = _age_days(memory.updated_at, reference)
        if age_days < ttl_days:
            return False
        reason = (
            f"retention: unconfirmed candidate older than {ttl_days:.0f} days "
            f"({age_days:.0f} days, {memory.distinct_runs}/{min_runs} runs)"
        )
        try:
            applied = self._store.set_trust(
                memory.memory_id,
                MemoryStatus.ARCHIVED,
                reason=reason,
                actor=self._actor,
            )
        except Exception:
            return False
        if not applied:
            return False
        report.stale_candidates.append(memory.memory_id)
        report.transitions.append(
            TrustTransition(
                memory_id=memory.memory_id,
                previous=memory.status,
                current=MemoryStatus.ARCHIVED,
                reason=reason,
                actor=self._actor,
            )
        )
        return True

    def _stamp(
        self,
        memory: CuratedMemory,
        reference: datetime,
        retention_days: float,
        report: RetentionReport,
    ) -> None:
        """Give a live record a deadline derived from its own ``updated_at``."""
        if retention_days <= 0:
            return
        deadline = _as_utc(memory.updated_at) + timedelta(days=retention_days)
        current = memory.expires_at
        if current is not None and abs(_as_utc(current) - deadline) <= _STAMP_TOLERANCE:
            return
        try:
            self._store.import_curated(
                memory.model_copy(update={"expires_at": deadline}),
                reason="retention: stamped expires_at",
            )
        except Exception:
            return
        report.stamped.append(memory.memory_id)


def _as_utc(value: datetime) -> datetime:
    # Legacy JSON records may predate tz-aware timestamps.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _age_days(value: datetime, reference: datetime) -> float:
    return max(0.0, (reference - _as_utc(value)).total_seconds() / 86_400.0)


__all__ = [
    "RETENTION_ACTOR",
    "RetentionReport",
    "RetentionService",
]
