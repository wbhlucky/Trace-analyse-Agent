"""Section 8.5 / 5.2 acceptance: an eval grade is evidence, not a shortcut.

Before this work the ``graded_pass`` and ``eval_run_id`` columns existed in the
schema and nothing ever wrote or read them, and the gate had no ``active ->
verified`` path other than an expert signature nobody had. Two claims are pinned
here:

* **A grade can carry a memory to ``verified``** - but only once that memory has
  already earned ``active`` on corroboration. A single graded run stays a
  candidate, because "this conclusion was right about the trace it was scored
  against" says nothing about the next trace, and letting it skip the rung would
  reintroduce the self-contamination the gate exists to prevent under a stronger
  label.
* **The grade reaches the corpus through the store, identically on both
  adapters.** ``TRACE_AGENT_MEMORY_STORE=json`` is documented as a rollback; a
  rollback that silently drops eval evidence is not one.

Demotion is asserted alongside promotion: a graded memory that accumulates
enough independent contradictions still archives. Evidence that can only ever
raise trust is not evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trace_agent.memory import MemoryRuntime, MemoryStatus, build_episode
from trace_agent.memory.episode import apply_grade
from trace_agent.memory.eval_probe import MemoryEvalProbe, build_eval_probe
from trace_agent.memory.flags import (
    EVAL_RUN_ID_ENV,
    MEMORY_GATE_VERIFIED_RUNS_ENV,
    MEMORY_ROOT_ENV,
    memory_gate_verified_runs,
)
from trace_agent.memory.lifecycle import (
    GATE_ACTOR,
    MAX_CONTRADICTIONS,
    GateEvidence,
    Promoter,
    apply_trust_gate,
    evaluate_trust,
    graded_pass_for,
)
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    Provenance,
)
from trace_agent.memory.store import JsonStore, SqliteStore
from trace_agent.models import (
    AgentKind,
    AnalysisResult,
    AnalyzeRequest,
    Finding,
    FindingSeverity,
    FindingStatus,
    ScenarioType,
)

ANALYSIS = "sync file read on main thread during launch"


def _episode(
    index: int,
    *,
    status: str = "confirmed",
    confidence: float = 0.8,
    analysis: str = ANALYSIS,
    eval_run_id: str | None = None,
) -> EpisodicMemory:
    """One run's projection. Identical text on purpose, so runs fold together."""
    run_id = f"run-{index}"
    return EpisodicMemory(
        episode_id=f"ep-{index}",
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        scenario_type="white_screen",
        scenario="cold start white screen",
        symptom="blank page after launch",
        summary="main thread blocked",
        root_causes=[analysis],
        findings=[
            {
                "title": "Main thread blocked",
                "severity": "high",
                "status": status,
                "confidence": confidence,
                "analysis": analysis,
                "recommendation": "",
                "evidence_ids": ["ev-1"],
            }
        ],
        eval_run_id=eval_run_id,
    )


def _seed(runtime: MemoryRuntime, runs: int, **kwargs: object) -> str:
    """Consolidate ``runs`` independent runs and return the single memory id."""
    for index in range(1, runs + 1):
        runtime.remember_episode(_episode(index, **kwargs), consolidate=True)
    memories = runtime.list_curated()
    assert len(memories) == 1, "seed runs must fold into one record"
    return memories[0].memory_id


def _curated(
    *,
    runs: int = 5,
    confidence: float = 0.9,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> CuratedMemory:
    """A memory whose run count comes from provenance, as the model requires.

    ``distinct_runs`` and ``source_run_ids`` are derived from
    ``provenance.references`` rather than stored, so the seed has to be built
    the way the consolidator builds it: ``run:<id>`` entries for runs and bare
    ids for episodes.
    """
    run_ids = [f"run-{index}" for index in range(1, runs + 1)]
    return CuratedMemory(
        memory_id="mem-seed",
        kind=MemoryKind.PATTERN,
        scenario_type="white_screen",
        topic="main thread blocked",
        content=ANALYSIS,
        confidence=confidence,
        status=status,
        confirmed_run_ids=list(run_ids),
        provenance=Provenance(
            source_type="finding",
            references=[f"run:{run_id}" for run_id in run_ids]
            + [f"ep-{index}" for index in range(1, runs + 1)],
        ),
    )


# ------------------------------------------------------- gate rules, pure form


def test_graded_pass_verifies_a_corroborated_memory() -> None:
    """The section 5.2 path: grade + corroboration -> ``verified``."""
    evidence = GateEvidence(
        distinct_runs=2, confidence=0.8, confirmed_runs=2, graded_pass=True
    )
    status, reason = evaluate_trust(MemoryStatus.ACTIVE, evidence)
    assert status is MemoryStatus.VERIFIED
    assert "eval-graded" in reason


def test_grade_alone_cannot_skip_the_corroboration_rung() -> None:
    """A right answer about one trace is not a law about the next one."""
    single = GateEvidence(
        distinct_runs=1, confidence=0.95, confirmed_runs=1, graded_pass=True
    )
    assert evaluate_trust(MemoryStatus.CANDIDATE, single)[0] is MemoryStatus.CANDIDATE

    # Enough runs but too little confidence, and runs with no confirmed source:
    # both still fail ``active``, so neither may borrow the grade channel.
    unconfident = GateEvidence(
        distinct_runs=4, confidence=0.2, confirmed_runs=1, graded_pass=True
    )
    unconfirmed = GateEvidence(
        distinct_runs=4, confidence=0.9, confirmed_runs=0, graded_pass=True
    )
    assert evaluate_trust(MemoryStatus.CANDIDATE, unconfident)[0] is (
        MemoryStatus.CANDIDATE
    )
    assert evaluate_trust(MemoryStatus.CANDIDATE, unconfirmed)[0] is (
        MemoryStatus.CANDIDATE
    )


def test_corroborated_runs_reach_verified_without_any_grade() -> None:
    """The other section 5.2 path: ``distinct_runs >= 5`` with zero dissent."""
    threshold = memory_gate_verified_runs()
    assert threshold == 5
    evidence = GateEvidence(
        distinct_runs=threshold, confidence=0.8, confirmed_runs=threshold
    )
    status, reason = evaluate_trust(MemoryStatus.ACTIVE, evidence)
    assert status is MemoryStatus.VERIFIED
    assert str(threshold) in reason

    one_short = GateEvidence(
        distinct_runs=threshold - 1, confidence=0.8, confirmed_runs=threshold - 1
    )
    assert evaluate_trust(MemoryStatus.ACTIVE, one_short)[0] is MemoryStatus.ACTIVE


def test_verified_threshold_is_configurable_and_never_below_min_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ops can lower the bar; they cannot lower it below ``active``'s."""
    monkeypatch.setenv(MEMORY_GATE_VERIFIED_RUNS_ENV, "3")
    assert memory_gate_verified_runs() == 3
    evidence = GateEvidence(distinct_runs=3, confidence=0.8, confirmed_runs=3)
    assert evaluate_trust(MemoryStatus.ACTIVE, evidence)[0] is MemoryStatus.VERIFIED

    # A misconfigured "1" must not turn every single-run guess into verified.
    monkeypatch.setenv(MEMORY_GATE_VERIFIED_RUNS_ENV, "1")
    assert memory_gate_verified_runs() == 1
    weak = GateEvidence(distinct_runs=1, confidence=0.9, confirmed_runs=1)
    assert evaluate_trust(MemoryStatus.CANDIDATE, weak)[0] is MemoryStatus.CANDIDATE


def test_contradiction_archives_even_a_graded_memory() -> None:
    """Retraction outranks every promotion channel, grade included."""
    evidence = GateEvidence(
        distinct_runs=5,
        confidence=0.95,
        confirmed_runs=5,
        contradictions=MAX_CONTRADICTIONS,
        graded_pass=True,
    )
    status, reason = evaluate_trust(MemoryStatus.VERIFIED, evidence)
    assert status is MemoryStatus.ARCHIVED
    assert "contradicted" in reason

    gated = apply_trust_gate(
        _curated(), contradictions=MAX_CONTRADICTIONS, graded_pass=True
    )
    assert gated.status is MemoryStatus.ARCHIVED


# ----------------------------------------------------- gate rules, real corpus


def test_two_runs_land_on_active_and_a_grade_lifts_them_to_verified(
    tmp_path: Path,
) -> None:
    """End to end through consolidation and the promoter's audited write."""
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 2)
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.ACTIVE

    assert runtime.store.mark_episode_graded(
        "ep-1", graded_pass=True, graded_score=0.86, eval_run_id="eval-A"
    )
    transitions = Promoter(runtime.store).sweep()

    assert [
        (item.memory_id, item.previous, item.current) for item in transitions
    ] == [(memory_id, MemoryStatus.ACTIVE, MemoryStatus.VERIFIED)]
    promoted = runtime.store.load_curated(memory_id)
    assert promoted.status is MemoryStatus.VERIFIED
    assert "eval-graded" in promoted.status_reason
    # Section 7: the transition is auditable, not merely applied. ``recent_versions``
    # is newest-first, so row zero is the promotion this sweep just made.
    rows = [
        row
        for row in runtime.store.recent_versions(limit=50)
        if row["memory_id"] == memory_id
    ]
    assert rows, "a trust transition must leave a version row behind"
    assert rows[0]["trust"] == MemoryStatus.VERIFIED.value
    assert rows[0]["reason"] == promoted.status_reason
    assert rows[0]["actor"] == GATE_ACTOR


def test_five_runs_verify_themselves_with_no_grade_at_all(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 5)
    memory = runtime.store.load_curated(memory_id)
    assert memory.status is MemoryStatus.VERIFIED
    assert not graded_pass_for(runtime.store, memory)


def test_four_runs_stay_active_below_the_verified_threshold(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 4)
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.ACTIVE


def test_a_single_graded_run_is_still_a_candidate(tmp_path: Path) -> None:
    """The defect this whole design exists to prevent, restated as a test."""
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 1)
    runtime.store.mark_episode_graded(
        "ep-1", graded_pass=True, graded_score=1.0, eval_run_id="eval-A"
    )
    assert Promoter(runtime.store).sweep() == []
    memory = runtime.store.load_curated(memory_id)
    assert memory.status is MemoryStatus.CANDIDATE
    assert "gate not met" in memory.status_reason


def test_a_grade_cannot_rescue_an_unconfirmed_guess(tmp_path: Path) -> None:
    """Two ``suspected`` runs at 0.45 stay candidate even once graded."""
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 2, status="suspected", confidence=0.45)
    runtime.store.mark_episode_graded(
        "ep-1", graded_pass=True, graded_score=0.9, eval_run_id="eval-A"
    )
    Promoter(runtime.store).sweep()
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.CANDIDATE


def test_grading_one_episode_verifies_the_memory_citing_it(tmp_path: Path) -> None:
    """``graded_pass_for`` reads through provenance, so any source counts."""
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 2)
    runtime.store.mark_episode_graded("ep-2", graded_pass=True, eval_run_id="eval-A")
    memory = runtime.store.load_curated(memory_id)
    assert graded_pass_for(runtime.store, memory)


def test_a_failed_grade_is_not_evidence_for_promotion(tmp_path: Path) -> None:
    """``graded_pass=False`` must be distinguishable from "never graded"."""
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 2)
    runtime.store.mark_episode_graded(
        "ep-1", graded_pass=False, graded_score=0.2, eval_run_id="eval-A"
    )
    episode = runtime.store.load_episode("ep-1")
    assert episode.graded_pass is False
    assert episode.graded_score == pytest.approx(0.2)
    assert not graded_pass_for(runtime.store, runtime.store.load_curated(memory_id))
    assert Promoter(runtime.store).sweep() == []
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.ACTIVE


# -------------------------------------------------------------- apply_grade


def test_apply_grade_preserves_existing_eval_provenance() -> None:
    """A later manual grading must not erase which experiment minted it."""
    episode = _episode(1, eval_run_id="eval-A")
    graded = apply_grade(episode, graded_pass=True, graded_score=0.7)
    assert graded.eval_run_id == "eval-A"
    assert graded.graded_pass is True
    assert graded.graded_score == pytest.approx(0.7)
    # Unchanged source object: the helper returns a copy.
    assert episode.graded_pass is None

    restamped = apply_grade(graded, graded_pass=False, eval_run_id="eval-B")
    assert restamped.eval_run_id == "eval-B"
    assert restamped.graded_pass is False


def test_ungraded_episode_keeps_grade_absent_not_false() -> None:
    """``None`` means "never graded"; ``False`` means "graded and failed"."""
    episode = _episode(1)
    assert episode.graded_pass is None
    assert episode.eval_run_id is None


def test_build_episode_inherits_the_eval_run_id_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var is the only channel reaching the application layer."""
    request = AnalyzeRequest(
        trace_id="trace-1",
        trace_path=tmp_path / "trace.htrace",
        scenario_type=ScenarioType.COLD_START,
        scenario="open app",
        symptom="long white screen",
        output_dir=tmp_path / "case",
        agent=AgentKind.LOCAL,
    )
    analysis = AnalysisResult(
        summary="cold start is slow",
        findings=[
            Finding(
                title="MainThread blocked",
                severity=FindingSeverity.HIGH,
                status=FindingStatus.CONFIRMED,
                confidence=0.9,
                evidence_ids=["ev-1"],
                analysis="main thread blocked on IO",
                recommendation="move IO off main thread",
                verification="manual",
            )
        ],
    )

    monkeypatch.delenv(EVAL_RUN_ID_ENV, raising=False)
    assert build_episode(request, analysis, run_id="run-1").eval_run_id is None

    monkeypatch.setenv(EVAL_RUN_ID_ENV, "eval-xyz")
    assert build_episode(request, analysis, run_id="run-2").eval_run_id == "eval-xyz"
    # An explicit argument still wins over the process-wide env.
    assert (
        build_episode(request, analysis, run_id="run-3", eval_run_id="eval-manual")
        .eval_run_id
        == "eval-manual"
    )


# ------------------------------------------------------------- store parity


@pytest.mark.parametrize("store_cls", [JsonStore, SqliteStore])
def test_mark_episode_graded_is_identical_on_both_adapters(
    tmp_path: Path, store_cls: type
) -> None:
    """The rollback switch must not silently drop eval evidence."""
    store = store_cls(tmp_path / "memory")
    store.write_episode(_episode(1, eval_run_id="eval-A"))

    assert store.mark_episode_graded("ep-missing", graded_pass=True) is False
    assert store.mark_episode_graded(
        "ep-1", graded_pass=True, graded_score=0.81, eval_run_id="eval-A"
    )

    loaded = store.load_episode("ep-1")
    assert loaded.graded_pass is True
    assert loaded.graded_score == pytest.approx(0.81)
    assert loaded.eval_run_id == "eval-A"

    # Re-grading is a no-op on identity, not a duplicate episode.
    assert store.mark_episode_graded("ep-1", graded_pass=False, graded_score=0.1)
    assert [item.episode_id for item in store.list_episodes()] == ["ep-1"]
    regressed = store.load_episode("ep-1")
    assert regressed.graded_pass is False
    assert regressed.graded_score == pytest.approx(0.1)


def test_grade_survives_a_store_reopen(tmp_path: Path) -> None:
    """Grades are durable evidence, not a process-local annotation."""
    root = tmp_path / "memory"
    store = SqliteStore(root)
    store.write_episode(_episode(1, eval_run_id="eval-A"))
    store.mark_episode_graded("ep-1", graded_pass=True, graded_score=0.9)
    store.close()

    reopened = SqliteStore(root)
    loaded = reopened.load_episode("ep-1")
    assert loaded.graded_pass is True
    assert loaded.eval_run_id == "eval-A"
    reopened.close()


# ------------------------------------------------------------------ the probe


def test_probe_grades_then_regates_in_one_call(tmp_path: Path) -> None:
    """``review`` exists because a beat may never fire on an isolated corpus."""
    runtime = MemoryRuntime(tmp_path)
    memory_id = _seed(runtime, 2, eval_run_id="eval-A")
    probe = MemoryEvalProbe(runtime.store, eval_run_id="eval-A")

    assert probe.mark_graded(episode_id="ep-1", graded_pass=True, graded_score=0.9)
    transitions = probe.review("ep-1")

    assert [item["to"] for item in transitions] == [MemoryStatus.VERIFIED.value]
    assert transitions[0]["memory_id"] == memory_id
    assert transitions[0]["actor"] == GATE_ACTOR
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.VERIFIED


def test_probe_degrades_quietly_on_bad_input(tmp_path: Path) -> None:
    """An eval run is expensive; a locked database costs one metric, not the run."""
    runtime = MemoryRuntime(tmp_path)
    _seed(runtime, 2)
    probe = MemoryEvalProbe(runtime.store, eval_run_id="eval-A")

    assert probe.mark_graded(episode_id="", graded_pass=True) is False
    assert probe.mark_graded(episode_id="ep-nope", graded_pass=True) is False
    assert probe.review("") == []
    assert probe.review("ep-nope") == []


def test_build_eval_probe_returns_none_without_memory_or_an_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` means "this report has no memory section", not "rate is zero"."""
    monkeypatch.setenv(MEMORY_ROOT_ENV, str(tmp_path / "memory"))
    monkeypatch.delenv("TRACE_AGENT_MEMORY", raising=False)
    assert build_eval_probe("eval-A", project_root=tmp_path) is None
    assert build_eval_probe(None, project_root=tmp_path) is None
    assert build_eval_probe("   ", project_root=tmp_path) is None

    monkeypatch.setenv("TRACE_AGENT_MEMORY", "1")
    probe = build_eval_probe("  eval-B  ", project_root=tmp_path)
    assert isinstance(probe, MemoryEvalProbe)
    assert probe.eval_run_id == "eval-B"

    # An explicitly injected store bypasses the flag: the caller owns the choice.
    store = SqliteStore(tmp_path / "injected")
    assert build_eval_probe("eval-C", store=store) is not None
