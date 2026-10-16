"""Persisting a gate verdict through the store's audited path.

Section 7 makes two rules non-negotiable: a trust transition may only be
triggered by the quality gate, and it must leave a ``memory_versions`` row plus
a ``status_reason`` behind. Before P2 the dreaming beat satisfied the first rule
and broke the second - it recomputed the verdict correctly and then saved it
with :meth:`MemoryStore.write_curated`, which records the reason as
``"consolidate"``. Every transition in the audit trail therefore looked like a
routine write, and "who promoted this memory and on what evidence" was
unanswerable.

:func:`commit_gate` is the single place a verdict becomes durable. It routes
status changes through :meth:`MemoryStore.set_trust`, which is the only writer
that carries a reason *and* an actor, and falls back to a plain write for the
non-transition bookkeeping the gate also maintains (``occurrences``,
``status_reason``) so that a no-op sweep stays a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trace_agent.memory.lifecycle.quality_gate import (
    apply_trust_gate,
    normalize_validator,
)
from trace_agent.memory.models import RETIRED_STATUSES, CuratedMemory, MemoryStatus
from trace_agent.memory.ports import MemoryRelation, MemoryStore

# Recorded in ``memory_versions.actor`` so a transition made by the automatic
# gate is distinguishable from one made by retention, the conflict detector or
# an operator.
GATE_ACTOR = "quality-gate"

# ``memory_versions.actor`` prefix for a transition a human caused. Kept distinct
# from :data:`GATE_ACTOR` on purpose: "the evidence promoted this" and "a person
# vouched for this" are different claims, and section 5.2 lists them as separate
# routes to ``verified``. An operator auditing the trail has to be able to tell
# which memories carry a human name without joining anything.
EXPERT_ACTOR_PREFIX = "expert:"

# ``provenance.validated_by`` is a single string field, so more than one
# reviewer is recorded as a joined list. Reading it back always goes through
# :func:`_signatories`, which means a record signed before that convention
# existed - one bare name - parses as a list of one.
_SIGNATORY_SEPARATOR = ", "

@dataclass(frozen=True, slots=True)
class TrustTransition:
    """One persisted change of trust level."""

    memory_id: str
    previous: MemoryStatus
    current: MemoryStatus
    reason: str = ""
    actor: str = GATE_ACTOR

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection for events, ``run.json`` and the dashboard."""
        return {
            "memory_id": self.memory_id,
            "from": self.previous.value,
            "to": self.current.value,
            "reason": self.reason,
            "actor": self.actor,
        }


def contradictions_for(store: Any, memory_id: str) -> int:
    """Independent refutations recorded against a memory.

    Counted from ``memory_links`` rather than from the denormalised column, so
    the verdict is reproducible from the audit trail alone and a stale counter
    cannot change a decision. Stores that predate the link API report zero,
    which degrades to "no known contradiction" instead of failing the sweep.
    """
    counter = getattr(store, "count_links", None)
    if not callable(counter):
        return 0
    try:
        return max(0, int(counter(memory_id, MemoryRelation.CONTRADICTS.value)))
    except Exception:
        return 0


@dataclass(frozen=True, slots=True)
class Corroboration:
    """What a memory's ``supports`` neighbours contribute to its gate evidence.

    Both fields answer the same question - "how much independent backing does
    this conclusion have?" - and are computed in one traversal so the two can
    never disagree about which neighbours were consulted.
    """

    #: Runs recorded by corroborating neighbours, minus this record's own runs,
    #: so a run that produced both sides cannot corroborate itself.
    supporting_runs: int = 0
    #: Runs anywhere in the corroborating group whose finding was
    #: ``confirmed``/``observed``, this record's own included.
    confirmed_runs: int = 0


def corroboration_for(store: Any, memory_id: str) -> Corroboration:
    """Aggregate gate evidence across a memory and its ``supports`` neighbours.

    Two analyses that worded the same conclusion differently stay two records -
    merging them would need near-identical text - but they *are* corroboration.
    Without reading neighbours here, the ``candidate -> active`` run threshold
    could only ever be met by byte-identical findings folding into one record,
    which left a real second opinion stuck at ``candidate`` forever.

    The confirmation signal is aggregated for exactly the same reason the run
    count is. Section 5.2 asks that "at least one source run" carried a
    confirmed finding; once a ``supports`` link has established that two records
    are the same conclusion reached independently, both records' runs are
    sources of it. Counting a neighbour's runs toward the threshold while
    ignoring its confirmation split one group in two: the confirmed row promoted
    and the row that reached the same conclusion from a ``suspected`` finding
    never did, even though the evidence for the *conclusion* was identical.

    This does not weaken the anti-self-contamination property. A lone single-run
    guess still cannot promote: it needs a neighbour from a disjoint run, and
    that neighbour must itself carry confirmed evidence.

    Stores that predate the link API degrade to "only own provenance counts"
    rather than failing the sweep.
    """
    memory = store.load_curated(memory_id)
    own_runs = set(memory.source_run_ids) if memory is not None else set()
    own_confirmed = set(memory.confirmed_run_ids) if memory is not None else set()

    def _own_only() -> Corroboration:
        return Corroboration(confirmed_runs=len(own_confirmed))

    linker = getattr(store, "linked_ids", None)
    if not callable(linker):
        return _own_only()
    try:
        neighbour_ids = linker(memory_id, MemoryRelation.SUPPORTS.value)
    except Exception:
        return _own_only()

    supporting: set[str] = set()
    confirmed: set[str] = set(own_confirmed)
    for neighbour_id in neighbour_ids:
        neighbour = store.load_curated(neighbour_id)
        if neighbour is None:
            continue
        supporting |= set(neighbour.source_run_ids)
        confirmed |= set(neighbour.confirmed_run_ids)
    supporting -= own_runs
    return Corroboration(
        supporting_runs=len(supporting), confirmed_runs=len(confirmed)
    )


def supporting_runs_for(store: Any, memory_id: str) -> int:
    """Independent runs that corroborate a memory through a ``supports`` link."""
    return corroboration_for(store, memory_id).supporting_runs


def confirmed_runs_for(store: Any, memory_id: str) -> int:
    """Confirmed/observed runs across a memory and its ``supports`` neighbours."""
    return corroboration_for(store, memory_id).confirmed_runs


def graded_pass_for(store: Any, memory: CuratedMemory) -> bool:
    """Whether any episode behind this memory passed its deterministic grade.

    Read through the store instead of being passed in, because the gate must stay
    pure while the *evidence* still has to come from somewhere durable: the grade
    is written by the eval harness onto the episode, and a memory that cites that
    episode inherits it. A store without episode loading degrades to ``False``,
    i.e. "no eval evidence", never to an error.
    """
    loader = getattr(store, "load_episode", None)
    if not callable(loader):
        return False
    for episode_id in memory.source_episode_ids:
        try:
            episode = loader(episode_id)
        except Exception:
            continue
        if episode is not None and episode.graded_pass:
            return True
    return False


def commit_gate(
    store: MemoryStore,
    memory: CuratedMemory,
    *,
    actor: str = GATE_ACTOR,
) -> TrustTransition | None:
    """Re-evaluate one memory and persist the verdict. Returns the transition.

    ``None`` means "nothing moved", which is the common case and the property
    that makes a beat safe to fire as often as the dashboard likes.
    """
    contradictions = contradictions_for(store, memory.memory_id)
    corroboration = corroboration_for(store, memory.memory_id)
    gated = apply_trust_gate(
        memory,
        contradictions=contradictions,
        supporting_runs=corroboration.supporting_runs,
        confirmed_runs=corroboration.confirmed_runs,
        graded_pass=graded_pass_for(store, memory),
    )

    if gated.status is not memory.status:
        reason = gated.status_reason or ""
        # ``set_trust`` re-reads the record, so a concurrent writer cannot be
        # silently overwritten by the copy this function was handed.
        if not store.set_trust(
            memory.memory_id, gated.status, reason=reason, actor=actor
        ):
            return None
        return TrustTransition(
            memory_id=memory.memory_id,
            previous=memory.status,
            current=gated.status,
            reason=reason,
            actor=actor,
        )

    stale_reason = (gated.status_reason or "") != (memory.status_reason or "")
    if gated.occurrences != memory.occurrences or stale_reason:
        store.write_curated(gated)
    return None


def _signatories(validated_by: str | None) -> list[str]:
    """Real human identities on a record, in signing order.

    Every part goes through :func:`normalize_validator`, so the ``"pending"``
    placeholder older releases wrote is dropped here rather than being read as a
    reviewer who vouched for the memory.
    """
    if not validated_by:
        return []
    names: list[str] = []
    for part in str(validated_by).split(","):
        name = normalize_validator(part)
        if name and name not in names:
            names.append(name)
    return names


@dataclass(frozen=True, slots=True)
class SignOff:
    """Outcome of one human decision about one memory.

    ``accepted=False`` is a refusal, not an error: signing an archived memory,
    naming no reviewer, or withdrawing a signature that was never given are all
    things an operator can legitimately try, and each needs to say why nothing
    happened instead of raising into a CLI traceback.
    """

    memory_id: str
    accepted: bool
    #: Identity that acted, ``None`` when the request was rejected before one
    #: could be established.
    validated_by: str | None = None
    #: Trust level after the call, ``None`` when there was no record to act on.
    status: MemoryStatus | None = None
    #: The persisted trust transition, if the decision caused one. ``None`` for
    #: an idempotent repeat or a refusal.
    transition: TrustTransition | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection for ``--json``, events and the dashboard."""
        return {
            "memory_id": self.memory_id,
            "accepted": self.accepted,
            "validated_by": self.validated_by,
            "status": self.status.value if self.status is not None else None,
            "transition": self.transition.as_dict() if self.transition else None,
            "detail": self.detail,
        }


def sign_off(
    store: MemoryStore,
    memory_id: str,
    *,
    validated_by: str,
    note: str = "",
    actor: str | None = None,
) -> SignOff:
    """Record a human vouching for a memory, then let the gate act on it.

    This is section 5.2's ``active -> verified`` expert route and the mitigation
    section 13 relies on when the automatic gate is too strict: a real corpus can
    sit at ``candidate`` forever if its findings are all ``suspected``, because
    confirmation is exactly the evidence it lacks. Without a way to sign, the
    only response to that would be to lower the gate, which weakens the
    anti-self-contamination property for every memory in the store.

    Two steps, and the order matters. The signature is written first with
    :meth:`MemoryStore.revise`, because it is a claim about the record's
    provenance and needs its own audit row; the trust change is then produced by
    :func:`commit_gate` reading that signature back as evidence. Writing
    ``status = VERIFIED`` directly here would break section 7's rule that only the
    gate moves trust - and would also skip the contradictions check, letting a
    signature resurrect a conclusion the corpus has since refuted twice.

    Retired memories are refused for the same reason the gate refuses them:
    ``archived``/``superseded`` records stay out of recall, and a signature is
    not a way to undelete one.
    """
    identity = normalize_validator(validated_by)
    if identity is None:
        return SignOff(
            memory_id=memory_id,
            accepted=False,
            detail=(
                "no reviewer identity: a signature needs a real name, not "
                "'pending'/blank"
            ),
        )
    actor_name = actor or f"{EXPERT_ACTOR_PREFIX}{identity}"

    memory = store.load_curated(memory_id)
    if memory is None:
        return SignOff(
            memory_id=memory_id,
            accepted=False,
            validated_by=identity,
            detail="no such memory in this namespace",
        )
    if memory.status in RETIRED_STATUSES:
        return SignOff(
            memory_id=memory_id,
            accepted=False,
            validated_by=identity,
            status=memory.status,
            detail=f"{memory.status.value} memory cannot be signed back into recall",
        )

    names = _signatories(memory.provenance.validated_by)
    if identity in names and memory.status is MemoryStatus.VERIFIED:
        # Already vouched for by this person and already carrying the trust that
        # buys. Re-running must be a no-op, or a dashboard button pressed twice
        # would leave two audit rows claiming two decisions.
        return SignOff(
            memory_id=memory_id,
            accepted=True,
            validated_by=identity,
            status=memory.status,
            detail="already signed off by this reviewer",
        )
    if identity not in names:
        names = [*names, identity]

    reason = f"expert sign-off by {identity}"
    if note:
        reason = f"{reason}: {note}"
    signed = memory.model_copy(
        update={
            "provenance": memory.provenance.model_copy(
                update={"validated_by": _SIGNATORY_SEPARATOR.join(names)}
            )
        }
    )
    store.revise(signed, reason=reason, actor=actor_name)

    # Re-read rather than reuse ``signed``: the store owns ``updated_at``,
    # ``version`` and ``occurrences``, and the gate must judge the durable record.
    reloaded = store.load_curated(memory_id) or signed
    transition = commit_gate(store, reloaded, actor=actor_name)
    final = store.load_curated(memory_id) or reloaded
    return SignOff(
        memory_id=memory_id,
        accepted=True,
        validated_by=identity,
        status=final.status,
        transition=transition,
        detail=(
            transition.reason
            if transition is not None
            else f"signed; trust stays {final.status.value}"
        ),
    )


def revoke_sign_off(
    store: MemoryStore,
    memory_id: str,
    *,
    revoked_by: str,
    note: str = "",
    actor: str | None = None,
) -> SignOff:
    """Withdraw a human signature and let the evidence decide the trust level.

    A signature that cannot be taken back is a one-way ratchet: one mistaken
    review would leave a wrong conclusion permanently injected as a verified
    prior, with no path back short of editing the database by hand.

    The whole sign-off is cleared rather than one name removed from it, because
    ``verified`` rests on "a human vouches for this record", not on a quorum.
    Withdrawing one name while another remained would keep the trust level the
    withdrawal was meant to question.

    Clearing it is not enough on its own. ``verified`` is sticky in the gate -
    deliberately, so that losing a corroborating run cannot silently demote a
    reviewed memory - so re-gating the record as it stands would return
    ``verified`` again and the revocation would do nothing. The verdict is
    therefore recomputed from a ``candidate`` baseline, which asks the one
    question that matters: does this memory still earn its trust on evidence
    alone? A conclusion five independent runs agree on stays ``verified`` after
    the signature goes, which is the correct answer, and a single-run guess
    drops back to ``candidate``.
    """
    identity = normalize_validator(revoked_by)
    if identity is None:
        return SignOff(
            memory_id=memory_id,
            accepted=False,
            detail="no reviewer identity: a withdrawal needs a real name",
        )
    actor_name = actor or f"{EXPERT_ACTOR_PREFIX}{identity}"

    memory = store.load_curated(memory_id)
    if memory is None:
        return SignOff(
            memory_id=memory_id,
            accepted=False,
            validated_by=identity,
            detail="no such memory in this namespace",
        )
    if not _signatories(memory.provenance.validated_by):
        return SignOff(
            memory_id=memory_id,
            accepted=False,
            validated_by=identity,
            status=memory.status,
            detail="no human sign-off on record, nothing to withdraw",
        )

    reason = f"expert sign-off withdrawn by {identity}"
    if note:
        reason = f"{reason}: {note}"
    cleared = memory.model_copy(
        update={
            "provenance": memory.provenance.model_copy(update={"validated_by": None})
        }
    )
    store.revise(cleared, reason=reason, actor=actor_name)
    reloaded = store.load_curated(memory_id) or cleared

    baseline = (
        reloaded.model_copy(update={"status": MemoryStatus.CANDIDATE})
        if reloaded.status is MemoryStatus.VERIFIED
        else reloaded
    )
    corroboration = corroboration_for(store, memory_id)
    verdict = apply_trust_gate(
        baseline,
        contradictions=contradictions_for(store, memory_id),
        supporting_runs=corroboration.supporting_runs,
        confirmed_runs=corroboration.confirmed_runs,
        graded_pass=graded_pass_for(store, reloaded),
    )

    reason_changed = (verdict.status_reason or "") != (reloaded.status_reason or "")
    transition: TrustTransition | None = None
    if verdict.status is not reloaded.status or reason_changed:
        settled = f"sign-off withdrawn; evidence says: {verdict.status_reason or verdict.status.value}"
        if store.set_trust(
            memory_id, verdict.status, reason=settled, actor=actor_name
        ) and verdict.status is not reloaded.status:
            transition = TrustTransition(
                memory_id=memory_id,
                previous=reloaded.status,
                current=verdict.status,
                reason=settled,
                actor=actor_name,
            )

    final = store.load_curated(memory_id) or reloaded
    return SignOff(
        memory_id=memory_id,
        accepted=True,
        validated_by=identity,
        status=final.status,
        transition=transition,
        detail=(
            transition.reason
            if transition is not None
            else f"sign-off withdrawn; trust stays {final.status.value}"
        ),
    )


class Promoter:
    """Corpus-wide gate sweep, shared by the dreaming beat and the CLI."""

    def __init__(self, store: MemoryStore, *, actor: str = GATE_ACTOR) -> None:
        self._store = store
        self._actor = actor

    def review_episode(self, episode_id: str) -> list[TrustTransition]:
        """Re-gate every memory that cites one episode.

        This is what ``enqueue_gate_review`` ends up calling. It produces no
        candidates, so firing it immediately after consolidation cannot read as
        a second confirmation of the same run - the defect that made a single
        ``suspected`` finding look corroborated.
        """
        if not episode_id:
            return []
        transitions: list[TrustTransition] = []
        for memory in self._store.list_curated(active_only=False):
            if episode_id not in memory.source_episode_ids:
                continue
            transition = self._commit(memory)
            if transition is not None:
                transitions.append(transition)
        return transitions

    def sweep(self, *, scenario_type: str | None = None) -> list[TrustTransition]:
        """Re-gate the corpus.

        Promotion and demotion are the same operation here: the gate can archive
        a memory that accumulated contradictions since the last beat just as
        easily as it can activate one that finally earned a second run.
        """
        transitions: list[TrustTransition] = []
        for memory in self._store.list_curated(active_only=False):
            if scenario_type and memory.scenario_type != scenario_type:
                continue
            transition = self._commit(memory)
            if transition is not None:
                transitions.append(transition)
        return transitions

    def _commit(self, memory: CuratedMemory) -> TrustTransition | None:
        try:
            return commit_gate(self._store, memory, actor=self._actor)
        except Exception:
            # Governance is best-effort. One unreadable record must not abort a
            # beat that has hundreds of others still to review.
            return None


__all__ = [
    "EXPERT_ACTOR_PREFIX",
    "GATE_ACTOR",
    "Corroboration",
    "Promoter",
    "SignOff",
    "TrustTransition",
    "commit_gate",
    "confirmed_runs_for",
    "contradictions_for",
    "corroboration_for",
    "graded_pass_for",
    "revoke_sign_off",
    "sign_off",
    "supporting_runs_for",
]
