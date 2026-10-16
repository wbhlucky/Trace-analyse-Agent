"""Section 8.4 acceptance: the eval report can prove its trials were independent.

Namespace isolation stops a trial from reading the *production* corpus. It does
not stop trial two from reading what trial one wrote into the shared eval
namespace, because both trials live inside that namespace by design. These tests
pin the part that does:

* an audit that walks what actually reached a prompt, follows each record back to
  the episodes that minted it, and separates "unverified guess from this run"
  from "something this run got promoted" - the second being the worse finding,
  not the milder one;
* a report aggregate whose ``self_contamination_rate`` is the section 14 headline
  and whose ``clean`` flag refuses to be satisfied by a zero rate achieved
  through promotion;
* the ordering guarantee that the audit runs *before* the grade is fed back,
  because a grade can move trust and reclassifying afterwards would understate
  exactly the leak being measured.

Also pinned: the parsing of ``run.json.memory_trace`` never raises. The runner
reads it inside the trial's ``try`` block, so one malformed memory key would
otherwise reclassify a successful trial as an error.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from trace_agent.eval import EvalHarness, load_case
from trace_agent.eval.memory_audit import (
    MAX_REPORTED_ITEMS,
    MemoryProbe,
    record_trial_memory,
    summarize_memory,
)
from trace_agent.eval.models import (
    ContaminatedRecord,
    EvalRun,
    MemoryContamination,
    Trial,
    TrialMemory,
    TrialMemoryHit,
    TrialStatus,
)
from trace_agent.eval.report import summarize
from trace_agent.eval.runner import TrialRunner
from trace_agent.memory import MemoryRuntime, MemoryStatus
from trace_agent.memory.eval_probe import MemoryEvalProbe
from trace_agent.memory.lifecycle import audit_injection
from trace_agent.memory.models import EpisodicMemory

# Three claims, deliberately disjoint so consolidation keeps them apart: one
# corroborated inside the eval run, one from production, one single-run guess from
# the same eval run. Together they exercise every branch of the audit at once.
CLAIMS = {
    "same_run_active": ("Main thread blocked", "main thread blocked by sync IO"),
    "production": ("GPU process crashed", "GPU process never reported a frame"),
    "same_run_candidate": (
        "Window manager missed draw",
        "window manager never received the first draw callback",
    ),
    "same_run_guess": (
        "Surface flinger dropped the first frame",
        "surface flinger appears to have dropped the first frame",
    ),
}


def _episode(
    index: int,
    *,
    title: str,
    analysis: str,
    eval_run_id: str | None = None,
    finding_status: str = "confirmed",
) -> EpisodicMemory:
    run_id = f"run-{index}"
    return EpisodicMemory(
        episode_id=f"ep-{index}",
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        scenario_type="white_screen",
        scenario="cold start white screen",
        symptom="blank page after launch",
        summary=title,
        root_causes=[analysis],
        findings=[
            {
                "title": title,
                "severity": "high",
                "status": finding_status,
                "confidence": 0.8,
                "analysis": analysis,
                "recommendation": "",
                "evidence_ids": ["ev-1"],
            }
        ],
        eval_run_id=eval_run_id,
    )


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[MemoryRuntime, dict[str, str]]:
    """A corpus holding one memory of each provenance the audit distinguishes."""
    runtime = MemoryRuntime(tmp_path)
    seeds = (
        (1, "same_run_active", "eval-A", "confirmed"),
        (2, "same_run_active", "eval-A", "confirmed"),
        (3, "production", None, "confirmed"),
        (4, "same_run_candidate", "eval-A", "confirmed"),
        # A fifth episode whose only finding is a guess. Same experiment as
        # ep-1, but nothing in it was corroborated, so it is the record that has
        # to count against the section 14 rate rather than only against trial
        # independence - without it the two buckets cannot be told apart.
        (5, "same_run_guess", "eval-A", "suspected"),
    )
    for index, claim, eval_run_id, finding_status in seeds:
        title, analysis = CLAIMS[claim]
        runtime.remember_episode(
            _episode(
                index,
                title=title,
                analysis=analysis,
                eval_run_id=eval_run_id,
                finding_status=finding_status,
            ),
            consolidate=True,
        )
    by_topic = {memory.topic: memory.memory_id for memory in runtime.list_curated()}
    statuses = {
        memory.topic: memory.status for memory in runtime.list_curated()
    }
    # The seed is only meaningful if it produced the trust levels under test.
    assert statuses[CLAIMS["same_run_active"][0]] is MemoryStatus.ACTIVE
    assert statuses[CLAIMS["production"][0]] is MemoryStatus.CANDIDATE
    assert statuses[CLAIMS["same_run_candidate"][0]] is MemoryStatus.CANDIDATE
    assert statuses[CLAIMS["same_run_guess"][0]] is MemoryStatus.CANDIDATE
    return runtime, {claim: by_topic[title] for claim, (title, _) in CLAIMS.items()}


def _trace(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": True,
        "query": "cold start white screen",
        "namespace": "eval:eval-A",
        "store": "sqlite",
        "episode_id": "ep-9",
        "context_chars": 420,
        "budget_chars": 3000,
        "latency_ms": 12.5,
        "trust_mix": {"candidate": 1, "active": 1},
        "hits": [
            {
                "memory_id": "m1",
                "trust": "candidate",
                "kind": "pattern",
                "score": 0.42,
                "lexical": 0.4,
                "semantic": 0.02,
                "injected": True,
            },
            {
                "memory_id": "m2",
                "trust": "active",
                "kind": "fact",
                "score": 0.88,
                "lexical": 0.7,
                "semantic": 0.18,
                "injected": True,
            },
            {"memory_id": "m3", "trust": "active", "score": 0.1, "injected": False},
        ],
        "created": ["m1"],
        "trust_changed": [],
    }
    payload.update(overrides)
    return payload


class _RecordingProbe:
    """Stands in for :class:`MemoryEvalProbe` and logs the order of calls."""

    eval_run_id = "eval-A"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.payload = payload

    def mark_graded(
        self, *, episode_id: str, graded_pass: bool, graded_score: float | None = None
    ) -> bool:
        self.calls.append(("mark_graded", episode_id, graded_pass, graded_score))
        return True

    def review(self, episode_id: str) -> list[dict[str, Any]]:
        self.calls.append(("review", episode_id))
        return [
            {
                "memory_id": "m1",
                "from": "candidate",
                "to": "verified",
                "reason": "eval-graded pass",
                "actor": "quality-gate",
            }
        ]

    def audit(
        self, injected: Any, *, trust_by_id: Any = None
    ) -> dict[str, Any]:
        self.calls.append(("audit", tuple(injected), dict(trust_by_id or {})))
        return dict(self.payload or {})


class _ExplodingProbe(_RecordingProbe):
    def mark_graded(self, **kwargs: Any) -> bool:
        raise RuntimeError("database is locked")

    def review(self, episode_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("database is locked")

    def audit(self, injected: Any, *, trust_by_id: Any = None) -> dict[str, Any]:
        raise RuntimeError("database is locked")


# ------------------------------------------------------- manifest projection


def test_from_trace_projects_what_reached_the_prompt() -> None:
    memory = TrialMemory.from_trace(_trace())
    assert memory is not None
    # Only injected hits count: recall finding something is not the model seeing it.
    assert memory.injected_ids == ["m1", "m2"]
    assert memory.injected_trust_mix == {"candidate": 1, "active": 1}
    assert memory.trust_mix == {"candidate": 1, "active": 1}
    assert memory.episode_id == "ep-9"
    assert memory.context_chars == 420
    assert memory.latency_ms == pytest.approx(12.5)
    assert memory.created == ["m1"]
    assert len(memory.hits) == 3
    assert memory.hits[2].injected is False


def test_from_trace_falls_back_to_the_injected_id_list() -> None:
    """Older manifests flagged nothing per-hit; the id list still says what landed."""
    memory = TrialMemory.from_trace(
        {"hits": [{"memory_id": "m1", "injected": False}], "injected_ids": ["m1", "m2"]}
    )
    assert memory.injected_ids == ["m1", "m2"]

    flagged = TrialMemory.from_trace(_trace(injected_ids=["stale"]))
    assert flagged.injected_ids == ["m1", "m2"], "per-hit flags win over a stale list"


def test_from_trace_never_raises_on_junk() -> None:
    """Regression: a malformed memory key must not cost the trial its outcome.

    ``TrialRunner`` calls this inside the trial's ``try`` block, so a raise here
    reclassifies a completed, gradeable trial as an error - the failure mode the
    method's own docstring says it exists to prevent.
    """
    assert TrialMemory.from_trace(None) is None
    assert TrialMemory.from_trace("memory_trace") is None
    assert TrialMemory.from_trace([]) is None

    junk = TrialMemory.from_trace(
        {
            "hits": "not a list",
            "context_chars": "many",
            "budget_chars": None,
            "latency_ms": "slow",
            "trust_mix": [{"candidate": 1}],
            "created": {"m1": True},
            "episode_id": "",
        }
    )
    assert junk.hits == []
    assert junk.context_chars == 0
    assert junk.latency_ms == 0.0
    assert junk.trust_mix == {}
    assert junk.created == []
    assert junk.episode_id is None

    # Hits without an id are dropped rather than turning into blank rows.
    partial = TrialMemory.from_trace({"hits": [{"trust": "active"}, "junk", 7]})
    assert partial.hits == []


def test_out_of_range_components_are_clamped_not_fatal() -> None:
    """Regression: ``lexical``/``semantic`` are bounded ``[0, 1]`` on the model.

    The tolerant parser only floored at zero, so a trace carrying ``lexical: 2``
    raised a ``ValidationError`` and the contamination metric - the whole point of
    reading the trace - was lost along with the trial's status.
    """
    memory = TrialMemory.from_trace(
        {
            "hits": [
                {
                    "memory_id": "m1",
                    "lexical": 2.5,
                    "semantic": -1.0,
                    "score": "high",
                    "injected": True,
                }
            ]
        }
    )
    hit = memory.hits[0]
    assert hit.lexical == 1.0
    assert hit.semantic == 0.0
    assert hit.score == 0.0
    assert hit.injected is True


# ------------------------------------------------------------ audit ordering


def test_the_audit_runs_before_the_grade_can_move_trust() -> None:
    """Order is the correctness property, not an implementation detail."""
    memory = TrialMemory.from_trace(_trace())
    probe = _RecordingProbe()

    record_trial_memory(probe, memory, graded_pass=True, graded_score=0.83)

    assert [call[0] for call in probe.calls] == ["audit", "mark_graded", "review"]
    # The audit is told what the prompt said, per record, for injected hits only.
    _, injected, trust_by_id = probe.calls[0]
    assert injected == ("m1", "m2")
    assert trust_by_id == {"m1": "candidate", "m2": "active"}
    assert memory.graded_pass is True
    assert memory.graded_score == pytest.approx(0.83)
    assert memory.gate_transitions[0]["to"] == "verified"


def test_contamination_survives_the_derived_keys_in_the_payload() -> None:
    """Regression: ``same_run_total`` is a property on the report model, not a field.

    ``ContaminationAudit.as_dict`` emits it because ``run.json`` consumers want
    it, and ``StrictModel`` forbids extras, so validating the dict verbatim raised
    and every trial reported ``contamination=None`` - a silent zero on the one
    metric section 14 targets at zero.
    """
    payload = {
        "eval_run_id": "eval-A",
        "injected": 3,
        "contaminated": 1,
        "trusted_from_same_run": 1,
        "same_run_total": 2,
        "rate": 0.3333,
        "clean": False,
        "items": [
            {
                "record_id": "m1",
                "record_kind": "curated",
                "trust": "candidate",
                "episode_id": "ep-1",
                "reason": "unverified memory minted by eval run eval-A",
            }
        ],
    }
    memory = TrialMemory.from_trace(_trace())
    record_trial_memory(_RecordingProbe(payload), memory, graded_pass=True)

    audit = memory.contamination
    assert audit is not None
    assert audit.eval_run_id == "eval-A"
    assert audit.contaminated == 1
    assert audit.trusted_from_same_run == 1
    assert audit.same_run_total == 2, "recomputed on the report side"
    assert audit.rate == pytest.approx(0.3333)
    assert audit.clean is False
    assert audit.items[0].record_id == "m1"


def test_the_grade_is_recorded_even_with_no_probe() -> None:
    """Memory off still leaves the trial's own grade on its observation."""
    memory = TrialMemory.from_trace(_trace())
    record_trial_memory(None, memory, graded_pass=False, graded_score=0.2)
    assert memory.graded_pass is False
    assert memory.graded_score == pytest.approx(0.2)
    assert memory.contamination is None
    assert memory.gate_transitions == []


def test_a_probe_that_raises_costs_one_metric_not_the_trial() -> None:
    memory = TrialMemory.from_trace(_trace())
    record_trial_memory(_ExplodingProbe(), memory, graded_pass=True, graded_score=0.9)
    assert memory.contamination is None
    assert memory.gate_transitions == []
    assert memory.graded_pass is True


def test_no_episode_means_there_is_nothing_to_grade() -> None:
    memory = TrialMemory.from_trace(_trace(episode_id=None))
    probe = _RecordingProbe()
    record_trial_memory(probe, memory, graded_pass=True, graded_score=1.0)
    assert [call[0] for call in probe.calls] == ["audit"]
    assert memory.graded_pass is True


def test_an_ungraded_trial_is_audited_but_stamps_no_grade() -> None:
    """``graded_pass=None`` means "never graded", not "graded and failed".

    A trial that errored or timed out still has a prompt, and what was in it is a
    fact worth measuring - but its failure is not evidence about any conclusion,
    so it must not write ``False`` onto an episode the verified channel reads.
    """
    memory = TrialMemory.from_trace(_trace())
    probe = _RecordingProbe(
        {"eval_run_id": "eval-A", "injected": 2, "contaminated": 1, "rate": 0.5}
    )

    record_trial_memory(probe, memory, graded_pass=None)

    assert [call[0] for call in probe.calls] == ["audit"]
    assert memory.graded_pass is None
    assert memory.graded_score is None
    assert memory.gate_transitions == []
    assert memory.contamination is not None
    assert memory.contamination.contaminated == 1


def test_a_missing_memory_observation_is_a_noop() -> None:
    probe = _RecordingProbe()
    record_trial_memory(probe, None, graded_pass=True)
    assert probe.calls == []


def test_the_memory_probe_satisfies_the_harness_protocol(tmp_path: Path) -> None:
    """The harness may only ever see three verbs; the store stays behind them."""
    runtime = MemoryRuntime(tmp_path)
    probe = MemoryEvalProbe(runtime.store, eval_run_id="eval-A")
    assert isinstance(probe, MemoryProbe)


# --------------------------------------------------------- contamination audit


def test_a_same_run_candidate_is_the_contamination_metric(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    runtime, ids = corpus
    audit = audit_injection(
        runtime.store,
        eval_run_id="eval-A",
        injected=[ids["same_run_candidate"], ids["production"]],
    )
    assert audit.contaminated == 1
    assert audit.trusted_from_same_run == 0
    assert audit.injected == 2
    assert audit.rate == pytest.approx(0.5)
    assert audit.clean is False
    item = audit.items[0]
    assert item.record_id == ids["same_run_candidate"]
    assert item.record_kind == "curated"
    assert item.trust == MemoryStatus.CANDIDATE.value
    assert item.episode_id == "ep-4"


def test_a_same_run_active_is_reported_as_worse_not_milder(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    """Zero candidates leaked is not a clean run if the gate promoted inside it."""
    runtime, ids = corpus
    audit = audit_injection(
        runtime.store, eval_run_id="eval-A", injected=[ids["same_run_active"]]
    )
    assert audit.contaminated == 0
    assert audit.trusted_from_same_run == 1
    assert audit.rate == 0.0, "the rate measures unverified leaks only"
    assert audit.clean is False, "but the run was still not independent"
    assert audit.items[0].trust == MemoryStatus.ACTIVE.value
    assert audit.items[0].episode_id in {"ep-1", "ep-2"}
    assert "not independent" in audit.items[0].reason


def test_a_production_memory_leaves_the_run_clean(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    runtime, ids = corpus
    audit = audit_injection(
        runtime.store, eval_run_id="eval-A", injected=[ids["production"]]
    )
    assert audit.same_run_total == 0
    assert audit.clean is True
    assert audit.rate == 0.0
    assert audit.items == ()


def test_an_injected_episode_is_contamination_too(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    """Handing the model another trial's whole case is the louder failure.

    Both buckets, because an episode carries a derived trust like any other
    record. An uncorroborated one reached the prompt as a guess and counts
    against the section 14 rate; a corroborated one reached it as a case, which
    does not move the rate but does mean the trials were not independent. The
    production episode is in the list too and must be ignored.
    """
    runtime, _ = corpus
    audit = audit_injection(
        runtime.store, eval_run_id="eval-A", injected=["ep-5", "ep-1", "ep-3"]
    )
    assert audit.injected == 3
    assert audit.contaminated == 1
    assert audit.trusted_from_same_run == 1
    # ``rate`` is rounded to four decimals so the report is stable as text.
    assert audit.rate == pytest.approx(0.3333)
    assert audit.clean is False

    guess, corroborated = audit.items
    assert guess.record_kind == "episodic"
    assert guess.record_id == "ep-5"
    assert guess.trust == MemoryStatus.CANDIDATE.value
    assert "unverified guess" in guess.reason
    assert corroborated.record_kind == "episodic"
    assert corroborated.record_id == "ep-1"
    assert corroborated.trust == MemoryStatus.ACTIVE.value
    assert "not independent" in corroborated.reason


def test_the_prompt_trust_wins_for_episodes_too(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    """An episode is classified by what the model was told, as curated is.

    ``trust_by_id`` comes from the memory trace, so it is the label the prompt
    actually carried. Deriving it again from the stored episode here would let a
    guess the model saw as a case - or the reverse - be reported as something it
    was not.
    """
    runtime, _ = corpus
    audit = audit_injection(
        runtime.store,
        eval_run_id="eval-A",
        injected=["ep-5"],
        trust_by_id={"ep-5": MemoryStatus.ACTIVE.value},
    )
    assert audit.contaminated == 0
    assert audit.trusted_from_same_run == 1
    assert audit.items[0].trust == MemoryStatus.ACTIVE.value


def test_the_trust_the_prompt_said_wins_over_the_stored_trust(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    """The gate may move a record after injection; the model saw the old label.

    Classifying by stored trust would file a candidate the model was shown as an
    unverified guess under the softer "already trusted" heading, which is precisely
    the understatement this audit exists to avoid - and the review that follows
    this call is allowed to promote the very record being classified.
    """
    runtime, ids = corpus
    audit = audit_injection(
        runtime.store,
        eval_run_id="eval-A",
        injected=[ids["same_run_active"]],
        trust_by_id={ids["same_run_active"]: MemoryStatus.CANDIDATE.value},
    )
    assert audit.contaminated == 1
    assert audit.trusted_from_same_run == 0
    assert audit.items[0].trust == MemoryStatus.CANDIDATE.value


def test_a_different_experiment_is_not_this_experiment(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    runtime, ids = corpus
    audit = audit_injection(
        runtime.store,
        eval_run_id="eval-B",
        injected=[ids["same_run_active"], ids["same_run_candidate"]],
    )
    assert audit.clean is True
    assert audit.same_run_total == 0


def test_no_experiment_or_nothing_injected_is_an_empty_audit(
    corpus: tuple[MemoryRuntime, dict[str, str]]
) -> None:
    """Production runs report ``rate=0`` rather than a missing key."""
    runtime, ids = corpus
    for eval_run_id, injected in ((None, [ids["same_run_active"]]), ("", ["ep-1"])):
        audit = audit_injection(
            runtime.store, eval_run_id=eval_run_id, injected=injected
        )
        assert audit.eval_run_id == ""
        assert audit.clean is True
        assert audit.rate == 0.0

    empty = audit_injection(runtime.store, eval_run_id="eval-A", injected=[])
    assert empty.injected == 0
    assert empty.rate == 0.0, "no division by zero, and no claim either way"
    assert empty.clean is True

    filtered = audit_injection(
        runtime.store, eval_run_id="eval-A", injected=["", None, ids["production"]]
    )
    assert filtered.injected == 1


def test_a_store_that_raises_degrades_to_not_found() -> None:
    """An audit that raises would take the whole eval report down with it."""

    class _Broken:
        def load_curated(self, record_id: str) -> Any:
            raise RuntimeError("disk gone")

        def load_episode(self, record_id: str) -> Any:
            raise RuntimeError("disk gone")

    audit = audit_injection(
        _Broken(), eval_run_id="eval-A", injected=["m1", "ep-1"]
    )
    assert audit.injected == 2
    assert audit.same_run_total == 0
    assert audit.clean is True

    class _NoLoaders:
        pass

    assert audit_injection(
        _NoLoaders(), eval_run_id="eval-A", injected=["m1"]
    ).clean is True


# ------------------------------------------------------------- report aggregate


def _memory(
    *,
    injected: tuple[tuple[str, str], ...] = (),
    contaminated: int = 0,
    trusted: int = 0,
    graded_pass: bool | None = None,
    graded_score: float | None = None,
    transitions: list[dict[str, Any]] | None = None,
    items: list[ContaminatedRecord] | None = None,
    eval_run_id: str = "eval-A",
) -> TrialMemory:
    hits = [
        TrialMemoryHit(memory_id=memory_id, trust=trust, injected=True)
        for memory_id, trust in injected
    ]
    audit = None
    if contaminated or trusted or items:
        total = max(len(injected), contaminated + trusted)
        audit = MemoryContamination(
            eval_run_id=eval_run_id,
            injected=total,
            contaminated=contaminated,
            trusted_from_same_run=trusted,
            rate=round(contaminated / total, 4) if total else 0.0,
            clean=(contaminated + trusted) == 0,
            items=items or [],
        )
    return TrialMemory(
        enabled=True,
        episode_id="ep-1",
        injected_ids=[memory_id for memory_id, _ in injected],
        hits=hits,
        graded_pass=graded_pass,
        graded_score=graded_score,
        gate_transitions=transitions or [],
        contamination=audit,
    )


def _trial(case_id: str, index: int, memory: TrialMemory | None) -> Trial:
    return Trial(trial_id=f"{case_id}#{index}", case_id=case_id, memory=memory)


def test_summarize_memory_aggregates_the_whole_run() -> None:
    run = EvalRun(
        run_id="eval-A",
        trials=[
            _trial(
                "c1",
                0,
                _memory(
                    injected=(("m1", "candidate"), ("m2", "active")),
                    contaminated=1,
                    graded_pass=True,
                    graded_score=0.8,
                    transitions=[
                        {
                            "memory_id": "m1",
                            "from": "candidate",
                            "to": "verified",
                            "reason": "eval-graded pass",
                            "actor": "quality-gate",
                        }
                    ],
                ),
            ),
            _trial(
                "c2",
                0,
                _memory(
                    injected=(("m3", "active"),),
                    trusted=1,
                    graded_pass=False,
                    graded_score=0.2,
                ),
            ),
            _trial("c3", 0, None),
        ],
    )
    summary = summarize_memory(run)

    assert summary["enabled"] is True
    assert summary["eval_run_id"] == "eval-A"
    assert summary["trials"] == 3
    assert summary["trials_with_memory"] == 2
    assert summary["injected_total"] == 3
    assert summary["candidate_injected"] == 1
    assert summary["injected_trust_mix"] == {"active": 2, "candidate": 1}
    assert summary["contaminated"] == 1
    assert summary["trusted_from_same_run"] == 1
    assert summary["contaminated_trials"] == 2
    assert summary["self_contamination_rate"] == pytest.approx(0.3333)
    assert summary["clean"] is False
    assert summary["episodes_graded"] == 2
    assert summary["episodes_graded_pass"] == 1
    # Transitions stay attributable to the trial that caused them.
    assert summary["gate_transitions"] == [
        {
            "case_id": "c1",
            "trial_id": "c1#0",
            "memory_id": "m1",
            "from": "candidate",
            "to": "verified",
            "reason": "eval-graded pass",
            "actor": "quality-gate",
        }
    ]


def test_a_clean_run_says_it_measured_zero() -> None:
    """"We checked and it is clean" is a claim; silence is not that claim."""
    run = EvalRun(
        run_id="eval-B",
        trials=[
            _trial(
                "c1",
                0,
                _memory(injected=(("m9", "active"),), graded_pass=True, graded_score=1.0),
            )
        ],
    )
    summary = summarize_memory(run)
    assert summary["enabled"] is True
    assert summary["self_contamination_rate"] == 0.0
    assert summary["clean"] is True
    assert summary["injected_total"] == 1
    assert summary["candidate_injected"] == 0


def test_a_memory_off_run_stays_distinguishable_from_a_clean_one() -> None:
    run = EvalRun(run_id="eval-C", trials=[_trial("c1", 0, None)])
    summary = summarize_memory(run)
    assert summary == {
        "enabled": False,
        "trials": 1,
        "trials_with_memory": 0,
        "injected_total": 0,
        "self_contamination_rate": 0.0,
        "clean": True,
    }
    assert summarize_memory(EvalRun(run_id="eval-D"))["trials"] == 0


def test_reported_items_are_capped_but_the_rate_is_not() -> None:
    """The rate is the metric; the items are the diagnosis. Ten is enough."""
    items = [ContaminatedRecord(record_id=f"m{index}") for index in range(25)]
    run = EvalRun(
        run_id="eval-E",
        trials=[_trial("c1", 0, _memory(injected=(("m1", "candidate"),), items=items))],
    )
    summary = summarize_memory(run)
    assert len(summary["items"]) == MAX_REPORTED_ITEMS
    assert summary["items"][0]["case_id"] == "c1"
    assert summary["items"][0]["record_id"] == "m0"


def test_the_cli_summary_always_carries_a_memory_section() -> None:
    """Consumers must not have to guess whether the key is absent or zero."""
    for trials in ([], [_trial("c1", 0, None)]):
        summary = summarize(EvalRun(run_id="eval-F", trials=trials))
        assert "memory" in summary
        assert summary["memory"]["enabled"] is False


# ---------------------------------------------------------- manifest round trip


def test_the_runner_reads_the_memory_trace_out_of_run_json(tmp_path: Path) -> None:
    """The write side and the read side agree on one file and one key."""
    output_dir = tmp_path / "trial"
    output_dir.mkdir()
    (output_dir / "run.json").write_text(
        json.dumps({"run_id": "run-1", "memory_trace": _trace()}), encoding="utf-8"
    )
    memory = TrialRunner._read_memory(output_dir)
    assert memory is not None
    assert memory.injected_ids == ["m1", "m2"]
    assert memory.episode_id == "ep-9"
    assert memory.namespace == "eval:eval-A"


def test_a_run_without_a_memory_trace_reads_as_none(tmp_path: Path) -> None:
    """``None`` means memory was off or the run died, not "recall found nothing"."""
    output_dir = tmp_path / "trial"
    output_dir.mkdir()
    (output_dir / "run.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
    assert TrialRunner._read_memory(output_dir) is None

    (output_dir / "run.json").write_text("{ truncated", encoding="utf-8")
    assert TrialRunner._read_memory(output_dir) is None

    assert TrialRunner._read_memory(tmp_path / "never-existed") is None


def test_a_failed_trial_still_carries_its_memory_trace(tmp_path: Path) -> None:
    """Regression: a run that died at validation still *recalled*.

    Reading the manifest only on the success path left such a trial with no
    memory observation at all, so the summary printed "Memory: off" for a run
    where memory was on and every injection before the failure went unaudited.
    """
    case = load_case(Path("evals/cases/cold_start_001/case.yaml"))

    class _DyingApp:
        async def run(self, request: Any) -> Any:
            (request.output_dir / "run.json").write_text(
                json.dumps({"memory_trace": _trace()}), encoding="utf-8"
            )
            raise RuntimeError("cold_start_result_required")

    runner = TrialRunner(
        application_factory=lambda: _DyingApp(), output_root=tmp_path
    )
    trial = asyncio.run(runner.run(case, index=0))

    assert trial.status is TrialStatus.ERROR
    assert trial.memory is not None
    assert trial.memory.injected_ids == ["m1", "m2"]
    assert (tmp_path / case.id / "trial-0" / "trial-error.txt").is_file()


def test_the_harness_audits_a_trial_that_never_graded(
    corpus: tuple[MemoryRuntime, dict[str, str]], tmp_path: Path
) -> None:
    """The contamination rate must not improve just because a trial blew up."""
    runtime, ids = corpus
    case = load_case(Path("evals/cases/cold_start_001/case.yaml"))
    trace = _trace(
        episode_id=None,
        hits=[
            {
                "memory_id": ids["same_run_candidate"],
                "trust": MemoryStatus.CANDIDATE.value,
                "injected": True,
                "score": 0.4,
            }
        ],
    )

    class _DyingApp:
        async def run(self, request: Any) -> Any:
            (request.output_dir / "run.json").write_text(
                json.dumps({"memory_trace": trace}), encoding="utf-8"
            )
            raise RuntimeError("cold_start_result_required")

    harness = EvalHarness(
        application_factory=lambda: _DyingApp(),
        output_root=tmp_path / "eval",
        trials=1,
        memory_probe=MemoryEvalProbe(runtime.store, eval_run_id="eval-A"),
    )
    run = asyncio.run(harness.run([case], run_id="eval-A"))

    trial = run.trials[0]
    assert trial.status is TrialStatus.ERROR
    assert trial.memory is not None
    assert trial.memory.contamination is not None
    assert trial.memory.contamination.contaminated == 1
    assert trial.memory.contamination.clean is False
    assert trial.memory.graded_pass is None

    summary = summarize_memory(run)
    assert summary["enabled"] is True
    assert summary["self_contamination_rate"] == pytest.approx(1.0)
    assert summary["clean"] is False
    assert summary["episodes_graded"] == 0


def test_a_full_memory_trace_round_trips_into_the_audit(
    corpus: tuple[MemoryRuntime, dict[str, str]], tmp_path: Path
) -> None:
    """Write side, read side and audit agree on what "injected" means."""
    runtime, ids = corpus
    trace = _trace(
        episode_id="ep-4",
        hits=[
            {
                "memory_id": ids["same_run_candidate"],
                "trust": MemoryStatus.CANDIDATE.value,
                "injected": True,
                "score": 0.4,
            },
            {
                "memory_id": ids["production"],
                "trust": MemoryStatus.CANDIDATE.value,
                "injected": True,
                "score": 0.3,
            },
        ],
    )
    output_dir = tmp_path / "trial"
    output_dir.mkdir()
    (output_dir / "run.json").write_text(
        json.dumps({"memory_trace": trace}), encoding="utf-8"
    )

    memory = TrialRunner._read_memory(output_dir)
    probe = MemoryEvalProbe(runtime.store, eval_run_id="eval-A")
    record_trial_memory(probe, memory, graded_pass=True, graded_score=0.9)

    assert memory.contamination is not None
    assert memory.contamination.contaminated == 1
    assert memory.contamination.trusted_from_same_run == 0
    assert memory.contamination.rate == pytest.approx(0.5)
    assert memory.contamination.clean is False
    # The grade then lifted the single-run candidate's neighbours, not the record
    # itself: one graded run is still one run, so it cannot skip the rung.
    assert runtime.store.load_curated(ids["same_run_candidate"]).status is (
        MemoryStatus.CANDIDATE
    )
    assert summarize_memory(
        EvalRun(run_id="eval-A", trials=[_trial("c1", 0, memory)])
    )["self_contamination_rate"] == pytest.approx(0.5)
