"""Self-contamination audit for eval runs (section 8.4).

Section 8's whole point is that an eval score must measure the agent, not the
agent's ability to recall its own earlier guess. Namespace isolation stops a
trial from reading the *production* corpus; it does not stop trial 2 from
reading what trial 1 wrote into the shared eval namespace, because both trials
are inside that namespace by design.

So the isolation is necessary but not sufficient, and this module is the
sufficient part: it walks what actually reached a trial's prompt, follows each
record back to the episodes that produced it, and reports how many of them were
minted by the same eval run. The target is zero - not "low", zero - because any
non-zero value means at least one trial was scored on a prior it created.

The audit is read-only. It never mutates trust and never blocks a run; it exists
so a contaminated result can be *diagnosed* from the report instead of being
discovered weeks later as a suspiciously high score.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from trace_agent.memory.models import EpisodicMemory, MemoryStatus
from trace_agent.memory.rerank import episode_trust

# Trust levels that reached the prompt without corroboration. A candidate that
# slipped in is the metric section 14 asks for; a candidate-free prompt still
# fails the independence test if an ``active`` record was minted by this very
# run, so both are counted - separately, because they mean different things.
_UNVERIFIED = frozenset({MemoryStatus.CANDIDATE.value})


@dataclass(frozen=True, slots=True)
class ContaminatedItem:
    """One injected record that traces back to this eval run."""

    record_id: str
    #: ``episodic`` when the raw episode itself was injected, ``curated`` when a
    #: distilled memory was. An injected episode is the louder failure: it hands
    #: the model another trial's whole case rather than a conclusion from it.
    record_kind: str
    trust: str
    episode_id: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_kind": self.record_kind,
            "trust": self.trust,
            "episode_id": self.episode_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContaminationAudit:
    """Result of auditing one trial's injection list."""

    eval_run_id: str = ""
    injected: int = 0
    #: Unverified records from this eval run - the section 14 headline metric.
    contaminated: int = 0
    #: Records from this eval run that were already ``active``/``verified`` when
    #: injected. Zero is the goal here too, and a non-zero value means the gate
    #: promoted something inside a single experiment, i.e. trials were not
    #: independent even though no *candidate* leaked.
    trusted_from_same_run: int = 0
    items: tuple[ContaminatedItem, ...] = ()

    @property
    def same_run_total(self) -> int:
        return self.contaminated + self.trusted_from_same_run

    @property
    def rate(self) -> float:
        """Share of injected records that were unverified *and* self-sourced."""
        if self.injected <= 0:
            return 0.0
        return round(self.contaminated / self.injected, 4)

    @property
    def clean(self) -> bool:
        """True when nothing injected came from this eval run at all."""
        return self.same_run_total == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "eval_run_id": self.eval_run_id,
            "injected": self.injected,
            "contaminated": self.contaminated,
            "trusted_from_same_run": self.trusted_from_same_run,
            "same_run_total": self.same_run_total,
            "rate": self.rate,
            "clean": self.clean,
            "items": [item.as_dict() for item in self.items],
        }


def audit_injection(
    store: Any,
    *,
    eval_run_id: str | None,
    injected: Iterable[str] | None,
    trust_by_id: Mapping[str, str] | None = None,
) -> ContaminationAudit:
    """Classify one trial's injected records against the run that produced them.

    ``eval_run_id`` unset, or an empty injection list, yields an empty audit
    rather than an error: production runs have no experiment to be contaminated
    by, and reporting ``rate=0`` for them keeps the report shape stable instead
    of making every consumer handle a missing key.

    ``trust_by_id`` is what the prompt actually said. The gate may have moved a
    record since - indeed this audit usually runs moments before a grade re-gates
    the corpus - and classifying by the *stored* trust would report a candidate
    the model saw as an unverified guess under the softer "trusted" heading.
    """
    ids = [item for item in (injected or ()) if item]
    if not eval_run_id or not ids:
        return ContaminationAudit(eval_run_id=eval_run_id or "", injected=len(ids))

    curated_loader = getattr(store, "load_curated", None)
    episode_loader = getattr(store, "load_episode", None)
    seen_trust = dict(trust_by_id or {})
    contaminated = 0
    trusted = 0
    items: list[ContaminatedItem] = []

    for record_id in ids:
        memory = _safe_load(curated_loader, record_id)
        if memory is not None:
            episode_id, same_run = _same_run_episode(
                episode_loader, memory.source_episode_ids, eval_run_id
            )
            if not same_run:
                continue
            trust = seen_trust.get(record_id) or str(
                getattr(memory.status, "value", memory.status)
            )
            if trust in _UNVERIFIED:
                contaminated += 1
                reason = (
                    f"unverified memory minted by eval run {eval_run_id} "
                    f"(episode {episode_id})"
                )
            else:
                trusted += 1
                reason = (
                    f"{trust} memory minted by eval run {eval_run_id} "
                    f"(episode {episode_id}): trials are not independent"
                )
            items.append(
                ContaminatedItem(
                    record_id=record_id,
                    record_kind="curated",
                    trust=trust,
                    episode_id=episode_id,
                    reason=reason,
                )
            )
            continue

        episode = _safe_load(episode_loader, record_id)
        if isinstance(episode, EpisodicMemory) and episode.eval_run_id == eval_run_id:
            # Episodes are bucketed by the same derived trust the prompt used, so
            # the two counters keep meaning what their names say: an episode the
            # model was shown as an unverified guess counts against the rate, one
            # shown as a corroborated case is the "trials were not independent"
            # failure instead. Filing every episode as unverified would make the
            # rate depend on how the analysis labelled its own findings rather
            # than on what the prompt claimed.
            trust = seen_trust.get(record_id) or episode_trust(episode).value
            if trust in _UNVERIFIED:
                contaminated += 1
                reason = (
                    f"raw episode from another trial of eval run {eval_run_id} "
                    "was injected verbatim as an unverified guess"
                )
            else:
                trusted += 1
                reason = (
                    f"raw episode from another trial of eval run {eval_run_id} "
                    f"was injected verbatim as {trust}: trials are not independent"
                )
            items.append(
                ContaminatedItem(
                    record_id=record_id,
                    record_kind="episodic",
                    trust=trust,
                    episode_id=episode.episode_id,
                    reason=reason,
                )
            )

    return ContaminationAudit(
        eval_run_id=eval_run_id,
        injected=len(ids),
        contaminated=contaminated,
        trusted_from_same_run=trusted,
        items=tuple(items),
    )


def _safe_load(loader: Any, record_id: str) -> Any:
    """Load one record, treating any failure as "not found".

    An audit that raises would take the eval report down with it, and a report
    that cannot be produced is worse than one that says "unknown".
    """
    if not callable(loader):
        return None
    try:
        return loader(record_id)
    except Exception:
        return None


def _same_run_episode(
    loader: Any,
    episode_ids: Iterable[str],
    eval_run_id: str,
) -> tuple[str, bool]:
    """First source episode belonging to this eval run, if any."""
    for episode_id in episode_ids:
        episode = _safe_load(loader, episode_id)
        if isinstance(episode, EpisodicMemory) and episode.eval_run_id == eval_run_id:
            return episode_id, True
    return "", False


__all__ = [
    "ContaminatedItem",
    "ContaminationAudit",
    "audit_injection",
]
