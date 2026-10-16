"""P0 regressions for the memory trust gate and CJK recall.

Each test here pins a defect that was reproduced against the previous
implementation, so a regression fails loudly instead of quietly degrading
diagnosis quality:

* one run (or one run replayed) could mint an ``active`` prior;
* Chinese queries returned nothing unless the sentence matched byte-for-byte;
* scores clamped to ``1.0`` so top-k ordering carried no information;
* a curated memory and the episode it came from both consumed prompt budget.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from trace_agent.memory import (
    MemoryRuntime,
    MemoryStatus,
    apply_trust_gate,
    evaluate_trust,
    freshness,
    resolve_memory_root,
    trust_prior,
    weighted_score,
)
from trace_agent.memory.deep_recall import DeepRecallService
from trace_agent.memory.flags import MEMORY_ROOT_ENV
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    Provenance,
)
from trace_agent.memory.lifecycle import (
    EXPERT_ACTOR_PREFIX,
    revoke_sign_off,
    sign_off,
)
from trace_agent.memory.ports import MemoryRelation
from trace_agent.memory.quality_gate import GateEvidence
from trace_agent.memory.rerank import (
    EPISODIC_TRUST_PRIOR,
    ScoreSignals,
    episode_is_corroborated,
    episode_trust,
    episode_trust_prior,
)
from trace_agent.models import utc_now


def _episode(
    *,
    episode_id: str,
    run_id: str,
    scenario_type: str = "white_screen",
    scenario: str = "cold start white screen",
    symptom: str = "blank page after launch",
    analysis: str = "sync file read on main thread during launch",
    status: str = "suspected",
    confidence: float = 0.45,
    severity: str = "high",
) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=episode_id,
        run_id=run_id,
        trace_id="trace-1",
        scenario_type=scenario_type,
        scenario=scenario,
        symptom=symptom,
        summary="main thread blocked",
        root_causes=["main thread blocked by sync IO"],
        findings=[
            {
                "title": "Main thread blocked by sync IO",
                "severity": severity,
                "status": status,
                "confidence": confidence,
                "analysis": analysis,
                "recommendation": "",
                "evidence_ids": ["ev-1"],
            }
        ],
    )


def _chinese_episode(episode_id: str, run_id: str) -> EpisodicMemory:
    return _episode(
        episode_id=episode_id,
        run_id=run_id,
        scenario_type="cold_start",
        scenario="冷启动白屏",
        symptom="应用冷启动后长时间白屏",
        analysis="冷启动阶段主线程同步读取配置文件，造成白屏",
        status="confirmed",
        confidence=0.8,
    )


# ---------------------------------------------------------------- quality gate


def test_gate_requires_runs_confidence_and_a_confirmed_source() -> None:
    """All three evidence conditions are needed; none alone is enough."""
    cases = [
        # distinct_runs, confidence, confirmed_runs -> expected
        (5, 0.99, 0, MemoryStatus.CANDIDATE),
        (1, 0.90, 1, MemoryStatus.CANDIDATE),
        (2, 0.30, 1, MemoryStatus.CANDIDATE),
        (2, 0.90, 1, MemoryStatus.ACTIVE),
    ]
    for runs, confidence, confirmed, expected in cases:
        evidence = GateEvidence(
            distinct_runs=runs,
            confidence=confidence,
            confirmed_runs=confirmed,
        )
        assert evaluate_trust(MemoryStatus.CANDIDATE, evidence)[0] is expected


def test_gate_archives_on_repeated_contradiction() -> None:
    evidence = GateEvidence(
        distinct_runs=5,
        confidence=0.99,
        confirmed_runs=3,
        contradictions=2,
    )
    status, reason = evaluate_trust(MemoryStatus.ACTIVE, evidence)
    assert status is MemoryStatus.ARCHIVED
    assert "contradicted" in reason


def test_gate_never_resurrects_retired_memory() -> None:
    evidence = GateEvidence(distinct_runs=9, confidence=1.0, confirmed_runs=9)
    for retired in (MemoryStatus.ARCHIVED, MemoryStatus.SUPERSEDED):
        assert evaluate_trust(retired, evidence)[0] is retired


def test_legacy_pending_sentinel_does_not_mean_verified() -> None:
    """Older records wrote ``validated_by="pending"`` as a placeholder.

    Read literally that is an expert sign-off, which would have promoted every
    legacy memory straight to ``verified``.
    """
    memory = CuratedMemory(
        memory_id="mem-legacy",
        kind=MemoryKind.PATTERN,
        topic="legacy",
        content="legacy content",
        confidence=0.95,
        provenance=Provenance(
            source_type="finding",
            references=["ep-legacy", "run:run-legacy"],
            validated_by="pending",
        ),
    )
    gated = apply_trust_gate(memory)
    assert gated.status is MemoryStatus.CANDIDATE


def test_real_sign_off_verifies() -> None:
    memory = CuratedMemory(
        memory_id="mem-signed",
        kind=MemoryKind.PATTERN,
        topic="signed",
        content="signed content",
        confidence=0.4,
        provenance=Provenance(
            source_type="finding",
            references=["ep-1", "run:run-1"],
            validated_by="reviewer@example.com",
        ),
    )
    gated = apply_trust_gate(memory)
    assert gated.status is MemoryStatus.VERIFIED
    assert gated.occurrences == gated.distinct_runs == 1


# ------------------------------------------------------- end-to-end write path


def test_single_run_survives_dream_beat_as_candidate(tmp_path: Path) -> None:
    """The exact post-run sequence from ``analyze._remember_episode``.

    Consolidate once, then let the dreaming beat run. Previously the beat
    consolidated the same episode again and the memory came out ``active`` with
    ``occurrences == 2``.
    """
    runtime = MemoryRuntime(tmp_path)
    episode = _episode(episode_id="ep-probe-1", run_id="run-probe-1")
    runtime.remember_episode(episode, consolidate=True)
    runtime.dreaming.enqueue_gate_review(episode.episode_id)

    batch = runtime.dreaming.run_once(limit=20)

    assert batch.processed == 1
    assert batch.errors == 0
    memory = runtime.list_curated()[0]
    assert memory.status is MemoryStatus.CANDIDATE
    assert memory.occurrences == 1
    assert memory.distinct_runs == 1


def test_replaying_one_run_never_promotes(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    for _ in range(4):
        runtime.remember_episode(
            _episode(episode_id="ep-1", run_id="run-1"),
            consolidate=True,
        )
    memory = runtime.list_curated()[0]
    assert memory.distinct_runs == 1
    assert memory.status is MemoryStatus.CANDIDATE


def test_deep_promote_cannot_bypass_the_gate(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=True,
    )
    runtime.dreaming.enqueue_deep_promote()
    runtime.dreaming.run_once(limit=20)
    assert runtime.list_curated()[0].status is MemoryStatus.CANDIDATE


def test_second_independent_run_promotes_to_active(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=True,
    )
    runtime.remember_episode(
        _episode(
            episode_id="ep-2",
            run_id="run-2",
            status="confirmed",
            confidence=0.85,
        ),
        consolidate=True,
    )
    memory = runtime.list_curated()[0]
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.distinct_runs == 2
    assert sorted(memory.confirmed_run_ids) == ["run-2"]


# ------------------------------------------------ expert sign-off (section 5.2)


def _one_guess(tmp_path: Path) -> tuple[MemoryRuntime, str]:
    """A corpus holding one single-run ``suspected`` conclusion: a candidate."""
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=True,
    )
    memory_id = runtime.list_curated()[0].memory_id
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.CANDIDATE
    return runtime, memory_id


def _corroborated(tmp_path: Path, runs: int = 5) -> tuple[MemoryRuntime, str]:
    """One conclusion confirmed by ``runs`` independent runs: verified already."""
    runtime = MemoryRuntime(tmp_path)
    for index in range(runs):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{index}",
                run_id=f"run-{index}",
                status="confirmed",
                confidence=0.9,
            ),
            consolidate=True,
        )
    memory_id = runtime.list_curated()[0].memory_id
    assert runtime.store.load_curated(memory_id).status is MemoryStatus.VERIFIED
    return runtime, memory_id


def test_expert_sign_off_lifts_a_single_run_guess_to_verified(tmp_path: Path) -> None:
    """Section 5.2's expert route, and section 13's answer to a strict gate.

    A real corpus full of ``suspected`` findings can never satisfy the automatic
    gate, because confirmation is exactly the evidence it lacks. Before this the
    only lever was lowering the thresholds, which weakens the anti-contamination
    property for every memory in the store at once.
    """
    runtime, memory_id = _one_guess(tmp_path)

    outcome = sign_off(runtime.store, memory_id, validated_by="alice@example.com")

    assert outcome.accepted
    assert outcome.status is MemoryStatus.VERIFIED
    assert outcome.transition is not None
    assert outcome.transition.previous is MemoryStatus.CANDIDATE
    assert outcome.transition.actor == f"{EXPERT_ACTOR_PREFIX}alice@example.com"
    signed = runtime.store.load_curated(memory_id)
    assert signed is not None
    assert signed.provenance.validated_by == "alice@example.com"
    assert signed.status_reason == "expert sign-off by alice@example.com"
    # Verified memory is an ordinary prior, so the signature has to reach recall.
    assert runtime.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    ).hits


def test_sign_off_leaves_an_audit_trail_naming_the_reviewer(tmp_path: Path) -> None:
    """Section 7 rule 2: the transition carries a reason and an actor.

    Two rows are expected, and asserting both is the point. The signature itself
    changes no indexed text - the SQL adapter only records a version when a
    tracked field moves - so without treating ``validated_by`` as one of those
    fields the reviewer's name would be missing from the trail on SQLite while
    present on JSON, and "who vouched for this" would depend on the adapter.
    """
    runtime, memory_id = _one_guess(tmp_path)

    sign_off(runtime.store, memory_id, validated_by="alice", note="device farm replay")

    rows = runtime.store.recent_versions(limit=20)
    assert any(
        row["trust"] == MemoryStatus.VERIFIED.value
        and row["actor"] == f"{EXPERT_ACTOR_PREFIX}alice"
        for row in rows
    )
    # The signature, recorded while the record was still a candidate.
    assert any(
        row["trust"] == MemoryStatus.CANDIDATE.value
        and "expert sign-off by alice" in (row["reason"] or "")
        for row in rows
    )
    assert any("device farm replay" in (row["reason"] or "") for row in rows)


@pytest.mark.parametrize("identity", ["", "   ", "pending", "None", "null"])
def test_sign_off_refuses_a_placeholder_identity(
    tmp_path: Path, identity: str
) -> None:
    """``"pending"`` is what older releases wrote as a placeholder.

    Accepted as a signature it would mint ``verified`` for every unreviewed
    legacy record - the exact failure the gate's sentinel normalisation exists to
    prevent, reintroduced through the write path instead of the read path.
    """
    runtime, memory_id = _one_guess(tmp_path)

    outcome = sign_off(runtime.store, memory_id, validated_by=identity)

    assert not outcome.accepted
    assert outcome.status is None
    stored = runtime.store.load_curated(memory_id)
    assert stored is not None
    assert stored.status is MemoryStatus.CANDIDATE
    assert not stored.provenance.validated_by


def test_sign_off_refuses_a_retired_memory(tmp_path: Path) -> None:
    """A signature is not a way to undelete an archived conclusion."""
    runtime, memory_id = _one_guess(tmp_path)
    retired = runtime.store.load_curated(memory_id)
    assert retired is not None
    runtime.store.write_curated(
        retired.model_copy(update={"status": MemoryStatus.ARCHIVED})
    )

    outcome = sign_off(runtime.store, memory_id, validated_by="alice")

    assert not outcome.accepted
    assert outcome.status is MemoryStatus.ARCHIVED
    stored = runtime.store.load_curated(memory_id)
    assert stored is not None
    assert stored.status is MemoryStatus.ARCHIVED
    assert not stored.provenance.validated_by


def test_repeating_a_sign_off_writes_nothing_new(tmp_path: Path) -> None:
    """Idempotent, so a dashboard button pressed twice is one decision."""
    runtime, memory_id = _one_guess(tmp_path)
    sign_off(runtime.store, memory_id, validated_by="alice")
    before = len(runtime.store.recent_versions(limit=100))

    repeat = sign_off(runtime.store, memory_id, validated_by="alice")

    assert repeat.accepted
    assert repeat.transition is None
    assert repeat.status is MemoryStatus.VERIFIED
    assert len(runtime.store.recent_versions(limit=100)) == before


def test_a_second_reviewer_joins_instead_of_overwriting(tmp_path: Path) -> None:
    """``validated_by`` is one field, so two names share it rather than fight."""
    runtime, memory_id = _one_guess(tmp_path)
    sign_off(runtime.store, memory_id, validated_by="alice")

    second = sign_off(runtime.store, memory_id, validated_by="bob")

    assert second.accepted
    stored = runtime.store.load_curated(memory_id)
    assert stored is not None
    assert stored.provenance.validated_by == "alice, bob"
    assert stored.status is MemoryStatus.VERIFIED
    # Trust did not move, so there is no transition to report.
    assert second.transition is None


def test_contradictions_outrank_a_signature(tmp_path: Path) -> None:
    """A human cannot vouch a refuted conclusion back into recall.

    Section 5.2 sends any trust level to ``archived`` once stronger counter-evidence
    exists, and that rule is checked before the signature is read for exactly this
    reason.
    """
    runtime, memory_id = _one_guess(tmp_path)
    for index in range(2):
        runtime.store.link(
            memory_id, f"mem-refutation-{index}", MemoryRelation.CONTRADICTS.value
        )

    outcome = sign_off(runtime.store, memory_id, validated_by="alice")

    assert outcome.status is MemoryStatus.ARCHIVED
    stored = runtime.store.load_curated(memory_id)
    assert stored is not None
    assert stored.status is MemoryStatus.ARCHIVED
    assert "contradicted" in (stored.status_reason or "")


def test_revoking_a_sign_off_hands_trust_back_to_the_evidence(tmp_path: Path) -> None:
    """A signature that cannot be withdrawn is a one-way ratchet.

    ``verified`` is sticky in the gate - deliberately, so losing a corroborating
    run cannot quietly demote a reviewed memory - so clearing the field alone
    would leave the trust level untouched and the revocation would do nothing.
    """
    runtime, memory_id = _one_guess(tmp_path)
    sign_off(runtime.store, memory_id, validated_by="alice")

    outcome = revoke_sign_off(runtime.store, memory_id, revoked_by="alice")

    assert outcome.accepted
    assert outcome.status is MemoryStatus.CANDIDATE
    assert outcome.transition is not None
    assert outcome.transition.previous is MemoryStatus.VERIFIED
    assert outcome.transition.actor == f"{EXPERT_ACTOR_PREFIX}alice"
    stored = runtime.store.load_curated(memory_id)
    assert stored is not None
    assert stored.provenance.validated_by is None
    assert "sign-off withdrawn" in (stored.status_reason or "")
    # Back out of the trusted set, so recall no longer injects it as a prior.
    assert all(
        not isinstance(hit.memory, CuratedMemory)
        for hit in runtime.recall(
            "main thread blocked sync IO", scenario_type="white_screen"
        ).hits
    )


def test_revoking_keeps_verified_when_the_evidence_still_earns_it(
    tmp_path: Path,
) -> None:
    """Revocation is not a demotion button, it is a hand-back to the evidence.

    Withdrawing the name of a reviewer who turned out to be wrong must not erase
    five independent runs that agree.
    """
    runtime, memory_id = _corroborated(tmp_path)
    sign_off(runtime.store, memory_id, validated_by="alice")

    outcome = revoke_sign_off(runtime.store, memory_id, revoked_by="alice")

    assert outcome.accepted
    assert outcome.status is MemoryStatus.VERIFIED
    assert outcome.transition is None
    stored = runtime.store.load_curated(memory_id)
    assert stored is not None
    assert stored.provenance.validated_by is None
    assert "independent runs agree" in (stored.status_reason or "")


def test_revoking_a_signature_that_was_never_given_is_refused(tmp_path: Path) -> None:
    """Otherwise a stray command would rewrite a record's trust for no reason."""
    runtime, memory_id = _one_guess(tmp_path)
    before = len(runtime.store.recent_versions(limit=100))

    outcome = revoke_sign_off(runtime.store, memory_id, revoked_by="alice")

    assert not outcome.accepted
    assert outcome.status is MemoryStatus.CANDIDATE
    assert len(runtime.store.recent_versions(limit=100)) == before


# ------------------------------------------------------------------ read path


def test_candidate_memory_is_not_recalled_by_default(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=True,
    )
    result = runtime.recall("main thread blocked sync IO", scenario_type="white_screen")
    # Neither channel may carry a single-run guess: the distilled prior is a
    # candidate, and the episode it came from is uncorroborated, so both stay out.
    assert all(
        not isinstance(hit.memory, CuratedMemory) for hit in result.hits
    )
    assert all(
        not isinstance(hit.memory, EpisodicMemory) for hit in result.hits
    )


def test_candidate_memory_is_labelled_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=True,
    )
    monkeypatch.setenv("TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE", "1")
    result = runtime.recall("main thread blocked sync IO", scenario_type="white_screen")
    curated = [
        hit.memory for hit in result.hits if isinstance(hit.memory, CuratedMemory)
    ]
    assert curated
    # Section 6.4: an unverified single-run guess must reach the model under a
    # heading that says so, with an explicit instruction to discard it when it
    # contradicts this run's evidence.
    assert "未验证候选" in result.context_prompt
    assert "无条件丢弃" in result.context_prompt
    assert "candidate" in result.context_prompt


# --------------------------------------------------- episode channel gating


def test_an_uncorroborated_episode_is_not_recalled_by_default(tmp_path: Path) -> None:
    """The raw episode is not a back door around the trust gate.

    Gating only curated memory left one open. Recall also returns episodes, and
    every episode used to carry the same mid-range prior and land under the
    "historical similar case" heading - so a run whose findings were all
    ``suspected`` still handed the next run its own guess as if it were a case,
    which is the self-contamination section 10 exists to prevent.
    """
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=False,
    )
    result = runtime.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    )
    assert result.hits == []
    assert result.context_prompt == ""
    # ``recall`` falls back to the deep channel when the fast one is empty, so an
    # empty result here also proves the deep path applies the same rule.
    assert runtime.deep_recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    ).source_ids == []


def test_an_uncorroborated_episode_is_disclaimed_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled, a guess is still labelled as one - and stays out of ``active``."""
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=False,
    )
    monkeypatch.setenv("TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE", "1")
    result = runtime.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    )
    episodes = [
        hit for hit in result.hits if isinstance(hit.memory, EpisodicMemory)
    ]
    assert [hit.memory.episode_id for hit in episodes] == ["ep-1"]
    assert MemoryStatus.CANDIDATE.value in episodes[0].why
    assert "未验证候选" in result.context_prompt
    assert "历史相似案例" not in result.context_prompt


def test_a_corroborated_episode_is_still_recalled_as_a_prior(tmp_path: Path) -> None:
    """The gate costs nothing for an episode that actually took evidence."""
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(
            episode_id="ep-1",
            run_id="run-1",
            status="confirmed",
            confidence=0.85,
        ),
        consolidate=False,
    )
    result = runtime.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    )
    assert [hit.memory.episode_id for hit in result.hits] == ["ep-1"]
    assert MemoryStatus.ACTIVE.value in result.hits[0].why
    assert "历史相似案例" in result.context_prompt
    assert "未验证候选" not in result.context_prompt


def test_a_graded_pass_episode_counts_as_corroborated(tmp_path: Path) -> None:
    """Section 8.5: a grader verdict is evidence from outside the run itself."""
    runtime = MemoryRuntime(tmp_path)
    graded = _episode(episode_id="ep-1", run_id="run-1").model_copy(
        update={"graded_pass": True, "eval_run_id": "eval-A"}
    )
    runtime.remember_episode(graded, consolidate=False)
    result = runtime.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    )
    assert [hit.memory.episode_id for hit in result.hits] == ["ep-1"]


def test_episode_trust_follows_the_evidence_it_carries() -> None:
    guess = _episode(episode_id="ep-1", run_id="run-1")
    observed = _episode(episode_id="ep-2", run_id="run-2", status="observed")
    confirmed = _episode(episode_id="ep-3", run_id="run-3", status="confirmed")
    graded = guess.model_copy(update={"graded_pass": True})
    failed = guess.model_copy(update={"graded_pass": False})

    assert episode_is_corroborated(guess) is False
    assert episode_trust(guess) is MemoryStatus.CANDIDATE
    assert episode_trust(observed) is MemoryStatus.ACTIVE
    assert episode_trust(confirmed) is MemoryStatus.ACTIVE
    assert episode_trust(graded) is MemoryStatus.ACTIVE
    # ``graded_pass=False`` is a verdict *against* the conclusion, so it must not
    # read as corroboration. ``None`` (never graded) likewise changes nothing.
    assert episode_trust(failed) is MemoryStatus.CANDIDATE

    # An episode never earns ``verified``: section 5.2 makes ``active`` the rung
    # every automatic verification stands on, and one run cannot skip it.
    assert episode_trust(confirmed) is not MemoryStatus.VERIFIED

    assert episode_trust_prior(confirmed) == EPISODIC_TRUST_PRIOR
    assert episode_trust_prior(guess) == trust_prior(MemoryStatus.CANDIDATE)
    assert episode_trust_prior(confirmed) > episode_trust_prior(guess)


def test_deep_recall_gates_episodes_the_same_way(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The widened path may broaden relevance, not permissions."""
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1"),
        consolidate=False,
    )
    runtime.remember_episode(
        _episode(
            episode_id="ep-2",
            run_id="run-2",
            status="confirmed",
            confidence=0.85,
        ),
        consolidate=False,
    )
    service = DeepRecallService(runtime.store)

    assert service.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    ).source_ids == ["ep-2"]

    monkeypatch.setenv("TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE", "1")
    widened = service.recall(
        "main thread blocked sync IO", scenario_type="white_screen"
    )
    assert set(widened.source_ids) == {"ep-1", "ep-2"}
    # Enabled but still labelled, so a guess cannot read like a case.
    assert "unverified candidate" in widened.context_prompt


def test_curated_hit_supersedes_its_source_episode(tmp_path: Path) -> None:
    """Same-source dedupe: the distilled memory wins over its own episodes."""
    runtime = MemoryRuntime(tmp_path)
    for run_id in ("run-1", "run-2"):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{run_id}",
                run_id=run_id,
                status="confirmed",
                confidence=0.85,
            ),
            consolidate=True,
        )
    result = runtime.recall(
        "main thread blocked sync IO",
        scenario_type="white_screen",
    )
    assert len(result.hits) == 1
    assert isinstance(result.hits[0].memory, CuratedMemory)


def test_identical_episodes_collapse_instead_of_filling_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs over one case may not spend both candidate slots on one sentence.

    The candidate partition is capped at two lines. Re-running the same case
    writes an episode per run and every one of them renders to the same text, so
    the cap was reached by a single claim repeated and a genuinely different
    observation never made it into the prompt. Dedupe already covered a curated
    memory shadowing its own source episode; it did not cover two episodes
    saying the same thing.

    Nothing is lost by the collapse. "Seen in N independent runs" is the gate's
    output, and it reaches the prompt as a curated memory's ``runs=``, which is
    a stronger form of the same signal than printing the episode twice.
    """
    runtime = MemoryRuntime(tmp_path)
    monkeypatch.setenv("TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE", "1")
    first = _episode(episode_id="ep-1", run_id="run-1")
    runtime.remember_episode(first, consolidate=False)
    runtime.remember_episode(
        first.model_copy(update={"episode_id": "ep-2", "run_id": "run-2"}),
        consolidate=False,
    )
    runtime.remember_episode(
        first.model_copy(
            update={
                "episode_id": "ep-3",
                "run_id": "run-3",
                "root_causes": ["GPU process exited before the first frame"],
            }
        ),
        consolidate=False,
    )

    result = runtime.recall(
        "main thread blocked sync IO",
        scenario_type="white_screen",
        top_k=5,
    )
    episodes = [
        hit.memory.episode_id
        for hit in result.hits
        if isinstance(hit.memory, EpisodicMemory)
    ]
    # One of the identical pair survives - which one is a freshness tie-break,
    # not a contract - and the different conclusion keeps its slot.
    assert len([eid for eid in episodes if eid in {"ep-1", "ep-2"}]) == 1
    assert "ep-3" in episodes
    assert result.context_prompt.count("main thread blocked by sync IO") == 1
    assert "GPU process exited before the first frame" in result.context_prompt


@pytest.mark.parametrize(
    "query",
    ["冷启动白屏", "冷启动 白屏", "启动白屏问题排查", "白屏", "冷启动阶段白屏怎么排查"],
)
def test_chinese_recall_survives_rewording(tmp_path: Path, query: str) -> None:
    """CJK queries used to need a byte-identical sentence to match anything."""
    runtime = MemoryRuntime(tmp_path)
    for run_id in ("run-c1", "run-c2"):
        runtime.remember_episode(
            _chinese_episode(f"ep-{run_id}", run_id),
            consolidate=True,
        )
    result = runtime.recall(query, scenario_type="cold_start")
    assert result.hits, f"query {query!r} recalled nothing"


def test_unrelated_chinese_query_still_recalls_nothing(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    for run_id in ("run-c1", "run-c2"):
        runtime.remember_episode(
            _chinese_episode(f"ep-{run_id}", run_id),
            consolidate=True,
        )
    assert runtime.recall("帧率卡顿掉帧", scenario_type="cold_start").hits == []


# ------------------------------------------------------------------- scoring


def test_scores_are_bounded_without_saturating(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    for run_id in ("run-1", "run-2"):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{run_id}",
                run_id=run_id,
                status="confirmed",
                confidence=0.85,
            ),
            consolidate=True,
        )
    result = runtime.recall(
        "main thread blocked sync IO launch",
        scenario_type="white_screen",
    )
    assert result.hits
    # The old scorer clamped with min(score, 1.0) and produced many exact ties,
    # which made top-k ordering meaningless.
    assert all(0.0 < hit.score < 1.0 for hit in result.hits)


def test_rerank_orders_by_trust_then_confidence() -> None:
    base = ScoreSignals(lexical=0.8, importance=0.5, freshness=1.0)
    verified = weighted_score(
        replace(base, confidence=0.9, trust_prior=trust_prior(MemoryStatus.VERIFIED))
    )
    active = weighted_score(
        replace(base, confidence=0.9, trust_prior=trust_prior(MemoryStatus.ACTIVE))
    )
    candidate = weighted_score(
        replace(base, confidence=0.9, trust_prior=trust_prior(MemoryStatus.CANDIDATE))
    )
    weak = weighted_score(
        replace(base, confidence=0.2, trust_prior=trust_prior(MemoryStatus.ACTIVE))
    )
    assert verified > active > candidate
    assert active > weak
    assert verified < 1.0


def test_freshness_uses_a_true_half_life() -> None:
    now = utc_now()
    assert freshness(now, now=now, half_life_days=90) == pytest.approx(1.0)
    aged = now - timedelta(days=90)
    assert freshness(aged, now=now, half_life_days=90) == pytest.approx(0.5)
    ancient = now - timedelta(days=900)
    assert freshness(ancient, now=now, half_life_days=90) < 0.01


# ------------------------------------------------------- root / eval isolation


def test_memory_root_env_overrides_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MEMORY_ROOT_ENV, str(tmp_path / "custom"))
    assert resolve_memory_root(Path("Z:/elsewhere")) == (tmp_path / "custom").resolve()

    monkeypatch.delenv(MEMORY_ROOT_ENV)
    project = tmp_path / "project"
    assert resolve_memory_root(project) == project.resolve() / ".trace-agent" / "memory"


def test_eval_memory_is_off_by_default_and_isolated_when_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eval must not read or write the production corpus.

    With memory on by default, a failed trial's guess was recalled by the next
    trial, so the score measured self-contamination rather than agent skill.
    """
    from trace_agent.eval.cli import _prepare_eval_memory

    monkeypatch.delenv("TRACE_AGENT_MEMORY", raising=False)
    monkeypatch.delenv(MEMORY_ROOT_ENV, raising=False)
    assert _prepare_eval_memory(tmp_path) is False

    monkeypatch.setenv("TRACE_AGENT_MEMORY", "1")
    assert _prepare_eval_memory(tmp_path) is True
    root = Path(os.environ[MEMORY_ROOT_ENV])
    assert root.parent == tmp_path / "memory"
    assert root.name.startswith("eval-")

    # An explicit corpus wins, so A/B runs can benchmark accumulated memory.
    chosen = tmp_path / "corpus"
    monkeypatch.setenv(MEMORY_ROOT_ENV, str(chosen))
    assert _prepare_eval_memory(tmp_path) is True
    assert Path(os.environ[MEMORY_ROOT_ENV]) == chosen


def test_eval_namespace_is_run_scoped_and_isolates_the_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 8.1's second half: retrieval never crosses a namespace.

    An isolated store *root* already keeps an eval run off the production corpus,
    but the root is the escape hatch an A/B benchmark deliberately points at a
    persistent corpus. The namespace is what still makes that safe, so this pins
    both the shape of the id and the isolation it buys - and that an operator's
    explicit namespace is respected rather than overwritten.

    A fresh runtime per namespace is not test scaffolding: both adapters resolve
    the namespace once at construction, on purpose, so a mid-process change
    cannot split one corpus across two isolation domains.
    """
    from trace_agent.eval.cli import _eval_namespace
    from trace_agent.memory.flags import MEMORY_NAMESPACE_ENV

    monkeypatch.delenv(MEMORY_NAMESPACE_ENV, raising=False)
    assert _eval_namespace("eval-abc123") == "eval:eval-abc123"
    monkeypatch.setenv(MEMORY_NAMESPACE_ENV, "project:mine")
    assert _eval_namespace("eval-abc123") is None

    monkeypatch.setenv(MEMORY_NAMESPACE_ENV, "eval:eval-abc123")
    experiment = MemoryRuntime(tmp_path)
    experiment.remember_episode(
        _chinese_episode("ep-ns", "run-ns"),
        consolidate=True,
    )
    assert experiment.recall("冷启动白屏", scenario_type="cold_start").hits

    monkeypatch.setenv(MEMORY_NAMESPACE_ENV, "project:mine")
    production = MemoryRuntime(tmp_path)
    assert production.recall("冷启动白屏", scenario_type="cold_start").hits == []


def test_eval_memory_env_stamps_and_restores_the_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leaked ``eval:*`` namespace is worse than no isolation at all.

    Everything written after the run - a dashboard refresh, the next test in the
    session - would land in a namespace production recall never reads, and the
    corpus would look like it had simply stopped learning. Hence the assertion on
    the way out, and the same assertion after an exception: a failed eval run is
    the case most likely to skip cleanup written as a trailing statement.
    """
    from trace_agent.eval.cli import _eval_memory_env
    from trace_agent.memory.flags import EVAL_RUN_ID_ENV, MEMORY_NAMESPACE_ENV

    monkeypatch.delenv(MEMORY_NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(EVAL_RUN_ID_ENV, raising=False)

    # Memory off: nothing is stamped and there is no probe to grade with.
    with _eval_memory_env(
        "eval-off", memory_enabled=False, project_root=tmp_path
    ) as (probe, namespace):
        assert probe is None
        assert namespace == ""
        assert MEMORY_NAMESPACE_ENV not in os.environ
        assert EVAL_RUN_ID_ENV not in os.environ

    monkeypatch.setenv("TRACE_AGENT_MEMORY", "1")
    monkeypatch.setenv(MEMORY_ROOT_ENV, str(tmp_path / "corpus"))
    with _eval_memory_env(
        "eval-on", memory_enabled=True, project_root=tmp_path
    ) as (probe, namespace):
        assert probe is not None
        assert probe.eval_run_id == "eval-on"
        assert namespace == "eval:eval-on"
        assert os.environ[MEMORY_NAMESPACE_ENV] == "eval:eval-on"
        assert os.environ[EVAL_RUN_ID_ENV] == "eval-on"
    assert MEMORY_NAMESPACE_ENV not in os.environ
    assert EVAL_RUN_ID_ENV not in os.environ

    with pytest.raises(RuntimeError):
        with _eval_memory_env(
            "eval-boom", memory_enabled=True, project_root=tmp_path
        ):
            raise RuntimeError("trial exploded")
    assert MEMORY_NAMESPACE_ENV not in os.environ
    assert EVAL_RUN_ID_ENV not in os.environ
