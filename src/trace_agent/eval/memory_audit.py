"""Memory observability for eval runs: grade feedback and contamination rate.

Two sections of the memory design meet here. Section 8.5 wants a passed grade to
become *evidence* a memory can be verified on; section 8.4 wants the report to
state, per trial and in aggregate, whether the model was shown anything the same
experiment produced. Both need the eval side to talk to the memory store, and
neither should let it do more than that.

:class:`MemoryProbe` is therefore a three-method protocol rather than a
``MemoryRuntime`` hand-off. The harness grades and audits; it cannot consolidate,
promote, edit or delete. The concrete implementation lives in
:mod:`trace_agent.memory.eval_probe`, on the memory side of the boundary, so the
protocol here stays structural and the eval package never imports a store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from trace_agent.eval.models import (
    EvalRun,
    MemoryContamination,
    TrialMemory,
)

# Cap on how many contamination records the aggregate report lists. The rate is
# the metric; the items are the diagnosis, and ten is enough to see a pattern
# without turning ``eval-run.json`` into a dump of the corpus.
MAX_REPORTED_ITEMS = 10


@runtime_checkable
class MemoryProbe(Protocol):
    """What the harness may ask memory to do during an eval run."""

    @property
    def eval_run_id(self) -> str: ...

    def mark_graded(
        self,
        *,
        episode_id: str,
        graded_pass: bool,
        graded_score: float | None = None,
    ) -> bool: ...

    def review(self, episode_id: str) -> list[dict[str, Any]]: ...

    def audit(
        self,
        injected: Sequence[str] | None,
        *,
        trust_by_id: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...


def record_trial_memory(
    probe: MemoryProbe | None,
    memory: TrialMemory | None,
    *,
    graded_pass: bool | None,
    graded_score: float | None = None,
) -> None:
    """Audit one trial's injection, then feed its grade back into memory.

    The order is the point. The audit classifies each record by the trust the
    prompt actually carried, and a grade can move that trust - so auditing after
    ``review`` would report a candidate the model saw as an unverified guess
    under the softer "already trusted" heading and understate contamination.

    ``graded_pass=None`` means the trial was never graded - it errored or timed
    out before producing an analysable result. Such a trial still gets audited,
    because what it was *shown* is a fact about the run, but it stamps no grade:
    an infrastructure failure is not evidence that a conclusion was wrong, and
    writing ``False`` would put a lie into the episode record that the
    ``active -> verified`` channel later reads.

    Mutates ``memory`` in place; it is the trial's own observation object.
    """
    if memory is None:
        return
    memory.graded_pass = graded_pass
    memory.graded_score = graded_score
    if probe is None:
        return

    trust_by_id = {hit.memory_id: hit.trust for hit in memory.hits if hit.injected}
    try:
        payload = probe.audit(memory.injected_ids, trust_by_id=trust_by_id)
    except Exception:
        payload = {}
    if payload:
        memory.contamination = _parse_contamination(payload)

    episode_id = memory.episode_id
    if not episode_id or graded_pass is None:
        return
    try:
        probe.mark_graded(
            episode_id=episode_id,
            graded_pass=graded_pass,
            graded_score=graded_score,
        )
        memory.gate_transitions = list(probe.review(episode_id) or [])
    except Exception:
        # A grade that cannot be persisted costs one evidence channel; it must
        # not fail a trial that already produced a scoreable outcome.
        return


def _parse_contamination(payload: Mapping[str, Any]) -> MemoryContamination | None:
    """Project an audit payload onto the report model, dropping derived keys.

    ``ContaminationAudit.as_dict`` emits ``same_run_total`` because it is useful
    in ``run.json``, but on the report model that value is a computed property,
    not a field - and :class:`StrictModel` forbids extras. Validating the dict
    verbatim therefore raised, the ``except`` swallowed it, and the section 8.4
    headline metric silently arrived as ``None`` for every trial. Filtering to
    declared fields keeps both representations honest: the memory side may stay
    denormalised, and the report side keeps rejecting genuinely unknown keys
    inside the fields it does declare.
    """
    known = {
        key: value
        for key, value in payload.items()
        if key in MemoryContamination.model_fields
    }
    if not known:
        return None
    try:
        return MemoryContamination.model_validate(known)
    except Exception:
        return None


def summarize_memory(run: EvalRun) -> dict[str, Any]:
    """Aggregate every trial's memory observation into report metrics.

    ``self_contamination_rate`` is the section 14 headline: unverified records
    from this eval run divided by everything injected. ``clean`` folds in
    ``trusted_from_same_run``, because a zero rate achieved by promoting a
    same-run memory to ``active`` is not a clean run - it is a worse one.
    """
    trials = list(run.trials)
    observed = [trial for trial in trials if trial.memory is not None]
    memories = [trial.memory for trial in observed if trial.memory is not None]
    if not memories:
        return {
            "enabled": False,
            "trials": len(trials),
            "trials_with_memory": 0,
            "injected_total": 0,
            "self_contamination_rate": 0.0,
            "clean": True,
        }

    injected_total = 0
    contaminated = 0
    trusted_same_run = 0
    contaminated_trials = 0
    graded = 0
    graded_pass = 0
    transitions: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    trust_mix: dict[str, int] = {}

    for trial in trials:
        memory = trial.memory
        if memory is None:
            continue
        for key, count in memory.injected_trust_mix.items():
            trust_mix[key] = trust_mix.get(key, 0) + count
        injected_total += len(memory.injected_ids)
        if memory.graded_pass is not None:
            graded += 1
            if memory.graded_pass:
                graded_pass += 1
        for transition in memory.gate_transitions:
            transitions.append(
                {
                    "case_id": trial.case_id,
                    "trial_id": trial.trial_id,
                    **transition,
                }
            )
        audit = memory.contamination
        if audit is None:
            continue
        contaminated += audit.contaminated
        trusted_same_run += audit.trusted_from_same_run
        if audit.same_run_total:
            contaminated_trials += 1
        for item in audit.items:
            if len(items) >= MAX_REPORTED_ITEMS:
                break
            items.append(
                {
                    "case_id": trial.case_id,
                    "trial_id": trial.trial_id,
                    **item.model_dump(),
                }
            )

    rate = (
        round(contaminated / injected_total, 4) if injected_total else 0.0
    )
    return {
        "enabled": any(memory.enabled for memory in memories),
        "eval_run_id": next(
            (
                memory.contamination.eval_run_id
                for memory in memories
                if memory.contamination is not None and memory.contamination.eval_run_id
            ),
            "",
        ),
        "trials": len(trials),
        "trials_with_memory": len(memories),
        "injected_total": injected_total,
        "candidate_injected": trust_mix.get("candidate", 0),
        "injected_trust_mix": dict(sorted(trust_mix.items())),
        "contaminated": contaminated,
        "trusted_from_same_run": trusted_same_run,
        "contaminated_trials": contaminated_trials,
        "self_contamination_rate": rate,
        "clean": contaminated == 0 and trusted_same_run == 0,
        "episodes_graded": graded,
        "episodes_graded_pass": graded_pass,
        "gate_transitions": transitions,
        "items": items,
    }


__all__ = [
    "MAX_REPORTED_ITEMS",
    "MemoryProbe",
    "record_trial_memory",
    "summarize_memory",
]
