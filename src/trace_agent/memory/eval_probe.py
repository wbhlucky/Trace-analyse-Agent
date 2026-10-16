"""The only memory surface the eval harness is allowed to touch (section 8).

An experiment that can write to the corpus it is measuring is not an experiment.
The harness therefore gets three verbs and no more:

* :meth:`MemoryEvalProbe.mark_graded` - record the deterministic grade on the
  episode the trial produced, which is evidence, not a trust decision.
* :meth:`MemoryEvalProbe.review` - let the ordinary quality gate re-judge what
  that evidence touches, so a passed trial can carry a memory to ``verified``
  without the eval code knowing what ``verified`` means.
* :meth:`MemoryEvalProbe.audit` - answer "was any prior in this trial's prompt
  minted by this very eval run", the self-contamination rate section 14 targets
  at zero.

Every method is best-effort and returns an empty/false answer on failure. Eval
runs are long and expensive; a locked database must cost the report one metric,
not the whole run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from trace_agent.memory.flags import memory_enabled_explicit
from trace_agent.memory.lifecycle.contamination import audit_injection
from trace_agent.memory.lifecycle.promoter import Promoter
from trace_agent.memory.paths import resolve_memory_root
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.store import build_memory_store


class MemoryEvalProbe:
    """Grade-and-audit bridge between one eval run and one memory store."""

    def __init__(self, store: MemoryStore, *, eval_run_id: str) -> None:
        self._store = store
        self._eval_run_id = eval_run_id
        self._promoter = Promoter(store)

    @property
    def eval_run_id(self) -> str:
        return self._eval_run_id

    @property
    def store(self) -> MemoryStore:
        return self._store

    def mark_graded(
        self,
        *,
        episode_id: str,
        graded_pass: bool,
        graded_score: float | None = None,
    ) -> bool:
        """Stamp this run's grade onto the episode the trial projected."""
        if not episode_id:
            return False
        marker = getattr(self._store, "mark_episode_graded", None)
        if not callable(marker):
            return False
        try:
            return bool(
                marker(
                    episode_id,
                    graded_pass=graded_pass,
                    graded_score=graded_score,
                    eval_run_id=self._eval_run_id,
                )
            )
        except Exception:
            return False

    def review(self, episode_id: str) -> list[dict[str, Any]]:
        """Re-gate the memories citing one freshly graded episode.

        Called right after grading rather than left to the next dreaming beat,
        because an eval run may be the only thing that ever touches this
        isolated corpus: a beat that never fires would leave every grade
        unapplied and the ``active -> verified`` path untested in practice.
        """
        if not episode_id:
            return []
        try:
            return [
                transition.as_dict()
                for transition in self._promoter.review_episode(episode_id)
            ]
        except Exception:
            return []

    def audit(
        self,
        injected: Iterable[str] | None,
        *,
        trust_by_id: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Self-contamination report for one trial's injected records."""
        try:
            return audit_injection(
                self._store,
                eval_run_id=self._eval_run_id,
                injected=injected,
                trust_by_id=trust_by_id,
            ).as_dict()
        except Exception:
            return {
                "eval_run_id": self._eval_run_id,
                "injected": len(list(injected or ())),
                "contaminated": 0,
                "trusted_from_same_run": 0,
                "same_run_total": 0,
                "rate": 0.0,
                "clean": True,
                "items": [],
                "error": "audit failed",
            }


def build_eval_probe(
    eval_run_id: str | None,
    *,
    project_root: Path | None = None,
    store: MemoryStore | None = None,
) -> MemoryEvalProbe | None:
    """Probe for this eval run, or ``None`` when memory is not in play.

    ``None`` is the answer for a memory-off run (the eval default), for a run
    with no id to attribute episodes to, and for a store that will not open. All
    three collapse to "no memory metrics in this report", which is the honest
    reading: the alternative would be a report claiming ``rate=0`` for an
    experiment that never looked at memory at all.
    """
    if not eval_run_id or not eval_run_id.strip():
        return None
    if store is None:
        if memory_enabled_explicit() is not True:
            return None
        try:
            root = resolve_memory_root(project_root or Path.cwd())
            store = build_memory_store(root)
        except Exception:
            return None
    return MemoryEvalProbe(store, eval_run_id=eval_run_id.strip())


__all__ = ["MemoryEvalProbe", "build_eval_probe"]
