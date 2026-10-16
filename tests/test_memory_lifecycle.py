"""P2 acceptance: memory governance end to end.

``test_memory_trust.py`` pins the gate's *rules*. This file pins what the rules
do to a real corpus once the components that produce evidence are running:

* two independent runs disagreeing is recorded, costs the weaker side half its
  confidence, and archives on the second refutation (section 5.2);
* two near-identical records merge, and the merge is what finally earns the
  survivor its second run (section 5.3);
* every trust transition leaves a version row carrying a reason and an actor,
  which is the property section 7 says must be checkable rather than assumed;
* retention archives a single-run guess nobody ever confirmed, and stamps then
  honours a deadline for everything else (section 5.4);
* the composer gives each trust level its own budget, so a long ``verified``
  record cannot starve ``active`` and an unverified guess cannot eat the prompt;
* both store adapters reach the same verdicts, because ``TRACE_AGENT_MEMORY_STORE=json``
  is documented as a rollback and a rollback that changes behaviour is not one.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

import pytest
from typer.testing import CliRunner

from trace_agent.memory import MemoryRuntime, MemoryStatus
from trace_agent.memory.cli import memory_app
from trace_agent.memory.context.composer import (
    CANDIDATE_ITEM_LIMIT,
    compose_memory,
)
from trace_agent.memory.flags import MEMORY_PROMPT_CHARS_ENV, memory_inline_governance
from trace_agent.memory.lifecycle import (
    CONFLICT_ACTOR,
    CONFLICT_CONFIDENCE_PENALTY,
    GATE_ACTOR,
    ConflictDetector,
    RetentionService,
)
from trace_agent.memory.lifecycle.quality_gate import DERIVED_DIGEST_KINDS
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    Provenance,
    RecallHit,
)
from trace_agent.memory.project import (
    PROJECT_ACTOR,
    PROJECT_CONFIDENCE,
    ProjectDigestService,
    project_memory_id,
)
from trace_agent.memory.store import JsonStore, SqliteStore
from trace_agent.memory.summarize import (
    SUMMARY_CONFIDENCE,
    SessionSummarizer,
    summary_memory_id,
)
from trace_agent.models import utc_now

# One claim, three mutually disjoint readings of it. Token sets barely overlap,
# so ``similarity`` stays far below the conflict ceiling and the pair is read as
# a disagreement rather than a paraphrase.
_READINGS = (
    "the main thread was blocked by a synchronous file read",
    "GPU process crashed and never reported a frame",
    "window manager never received the first draw callback",
)

# 13 tokens; appending one more gives 13/14 = 0.93 Jaccard, just over the 0.92
# duplicate floor while still hashing to a different content fingerprint.
_NEAR_DUPLICATE = (
    "the main thread was blocked by a synchronous file read during cold launch"
)

# One claim worded two ways: 12/14 = 0.86 Jaccard, inside the supports band
# ``[conflict ceiling 0.60, duplicate floor 0.92)`` - too alike to read as a
# disagreement, too different to fold into one record, so the sweep treats the
# pair as corroboration and links it ``supports``.
_AGREEMENT = (
    "the main thread was blocked by a synchronous file read during cold launch",
    "the main thread was blocked by a synchronous file read during warm launch",
)

# A real pair, trimmed from the warm-up corpus that exposed the defect: one
# finding, two runs, two phrasings of the same conclusion. 0.32 Jaccard - well
# below the conflict ceiling - but 0.50 overlap, i.e. corroboration. Read as a
# refutation it halved both records' confidence and kept both stuck at
# ``candidate`` forever, which is the exact opposite of what the evidence said.
_PARAPHRASE = (
    "inspect_frame_jank 在问题区间返回 available=false，区间内目标应用 actual 与 expect 帧计数均为 0，"
    "最后一帧比区间起点早约 365ms，按帧率规则必须报告指标不可用而不是低 FPS",
    "问题区间内目标应用没有任何 actual 或 expect 帧，工具直接返回 available=false，"
    "应用最后一帧位于区间起点之前约 365 毫秒，因此该区间报告为指标不可用而非低帧率",
)

# One claim, both verdicts. 0.64 Jaccard sits *above* the conflict ceiling, so a
# similarity-only sweep reads agreement here; the polarity signal is what catches
# it, and it is checked before the duplicate floor so the pair cannot be folded.
_POLARITY = (
    "主线程在窗口内处于可中断睡眠，同步 IO 阻塞可以排除",
    "主线程在窗口内处于可中断睡眠，根因确认为同步 IO 阻塞",
)

# Two mechanisms for one symptom sharing a boilerplate opener: 0.10 Jaccard and
# 0.18 overlap. This is what a genuine disagreement looks like, and the overlap
# floor is set above it so the corpus keeps its ability to retract.
_MECHANISMS = (
    "白屏根因指向主线程同步 IO 阻塞，主线程在窗口内长时间不可中断等待，磁盘读取耗时超过阈值",
    "白屏根因指向 GPU 进程崩溃，渲染进程在首帧产出之前退出且未被重新拉起，合成器无内容可提交",
)


# The subject line is the part of a finding an LLM most reliably rewords between
# runs, so a sweep that needs it to match verbatim never compares anything. These
# two titles share no fingerprint and still measure 0.56 overlap / 0.36 Jaccard -
# above both relatedness floors, i.e. recognisably the same subject.
_TOPIC_REWORDED = (
    "main thread blocked by synchronous IO during cold launch",
    "main thread stalled on a synchronous disk read during launch",
)

# Sharing a subject *word* is how blocking finds a pair; it is not evidence the
# two records are about the same thing. Three boilerplate tokens ("during",
# "cold", "launch") give 0.33 overlap, below the relatedness floor, while the
# contents flatly disagree - the pair that a content-only sweep would have read
# as a refutation and charged both records for.
_TOPIC_BOILERPLATE_ONLY = (
    _TOPIC_REWORDED[0],
    "audio focus was lost during the cold launch window",
)

# A two-word subject is contained in every record of a white-screen corpus:
# 1.00 overlap, 0.12 Jaccard. The overlap floor alone would call it the same
# subject and pair the terse record with the whole scope, which is exactly why
# relatedness requires the Jaccard floor as well.
_TOPIC_DEGENERATE = (
    "white screen",
    "white screen caused by the main thread blocking on synchronous IO "
    "during cold launch and never recovering",
)


def _episode(
    *,
    episode_id: str,
    run_id: str,
    analysis: str,
    title: str = "Main thread blocked",
    scenario_type: str = "white_screen",
    status: str = "confirmed",
    confidence: float = 0.8,
) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=episode_id,
        run_id=run_id,
        trace_id=f"trace-{run_id}",
        scenario_type=scenario_type,
        scenario="cold start white screen",
        symptom="blank page after launch",
        summary="main thread blocked",
        root_causes=[analysis],
        findings=[
            {
                "title": title,
                "severity": "high",
                "status": status,
                "confidence": confidence,
                "analysis": analysis,
                "recommendation": "",
                "evidence_ids": ["ev-1"],
            }
        ],
    )


def _curated(
    memory_id: str,
    *,
    status: MemoryStatus,
    content: str,
    topic: str = "main thread blocked",
    confidence: float = 0.9,
    kind: MemoryKind = MemoryKind.PATTERN,
) -> CuratedMemory:
    return CuratedMemory(
        memory_id=memory_id,
        kind=kind,
        scenario_type="white_screen",
        topic=topic,
        content=content,
        confidence=confidence,
        status=status,
        provenance=Provenance(source_type="finding", references=["run:seed"]),
    )


def _ids_by_run(runtime: MemoryRuntime) -> dict[str, str]:
    """Map run id -> memory id, captured *before* a sweep merges provenance."""
    out: dict[str, str] = {}
    for memory in runtime.list_curated():
        for run_id in memory.source_run_ids:
            out.setdefault(run_id, memory.memory_id)
    return out


def _seed_disagreement(runtime: MemoryRuntime, count: int) -> dict[str, str]:
    """Write ``count`` independent runs that share a topic but not a conclusion."""
    confidences = (0.9, 0.5, 0.7)
    for index in range(count):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{index + 1}",
                run_id=f"run-{index + 1}",
                analysis=_READINGS[index],
                confidence=confidences[index],
            ),
            consolidate=True,
        )
    return _ids_by_run(runtime)


def _seed_near_duplicate(runtime: MemoryRuntime) -> dict[str, str]:
    """Two runs that say almost the same thing, so the sweep merges them."""
    runtime.remember_episode(
        _episode(
            episode_id="ep-1",
            run_id="run-1",
            analysis=_NEAR_DUPLICATE,
            confidence=0.9,
        ),
        consolidate=True,
    )
    runtime.remember_episode(
        _episode(
            episode_id="ep-2",
            run_id="run-2",
            analysis=f"{_NEAR_DUPLICATE} again",
            confidence=0.5,
        ),
        consolidate=True,
    )
    return _ids_by_run(runtime)


def _seed_agreement(runtime: MemoryRuntime) -> dict[str, str]:
    """Two independent runs that reach the same conclusion in different words.

    Same finding title (so the topic fingerprint matches and the sweep compares
    them) but different analysis text inside the supports band, from disjoint
    runs. Neither folds into the other, so before the supports relation existed
    both stayed single-run ``candidate`` records forever.
    """
    return _seed_pair(runtime, _AGREEMENT)


def _seed_pair(
    runtime: MemoryRuntime,
    readings: tuple[str, str],
    *,
    confidence: float = 0.85,
    title: str = "Main thread blocked",
) -> dict[str, str]:
    """Two disjoint runs whose findings share a title but not their wording."""
    for index, analysis in enumerate(readings, start=1):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{index}",
                run_id=f"run-{index}",
                analysis=analysis,
                confidence=confidence,
                title=title,
            ),
            consolidate=True,
        )
    return _ids_by_run(runtime)


def _seed_sides(
    runtime: MemoryRuntime,
    sides: Sequence[dict[str, Any]],
) -> dict[str, str]:
    """One run per side, each free to word and grade its finding differently.

    ``_seed_pair`` gives both runs the same title, which is what let the exact
    topic fingerprint pass for a blocking function. Sides that differ in title,
    finding status *and* confidence are the shape a real corpus has.
    """
    for index, side in enumerate(sides, start=1):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{index}",
                run_id=f"run-{index}",
                **side,
            ),
            consolidate=True,
        )
    return _ids_by_run(runtime)


# ------------------------------------------------------------ conflict sweep


def test_first_contradiction_halves_only_the_weaker_side(tmp_path: Path) -> None:
    """One dissent is evidence, not a death sentence (section 5.2)."""
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_disagreement(runtime, 2)

    report = ConflictDetector(runtime.store).run()

    assert len(report.contradictions) == 1
    assert not report.duplicates
    assert report.demoted == [ids["run-2"]]

    strong = runtime.store.load_curated(ids["run-1"])
    weak = runtime.store.load_curated(ids["run-2"])
    assert strong is not None and weak is not None
    assert strong.confidence == pytest.approx(0.9)
    assert weak.confidence == pytest.approx(0.5 * CONFLICT_CONFIDENCE_PENALTY)
    # A single refutation leaves both recallable; only the gate may retire them.
    assert strong.status is MemoryStatus.CANDIDATE
    assert weak.status is MemoryStatus.CANDIDATE
    assert runtime.store.count_links(strong.memory_id, "contradicts") == 1
    assert runtime.store.count_links(weak.memory_id, "contradicts") == 1


def test_second_contradiction_archives_every_side(tmp_path: Path) -> None:
    """The corpus must be able to retract, not only accumulate."""
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_disagreement(runtime, 3)

    ConflictDetector(runtime.store).run()
    ConflictDetector(runtime.store).run()

    for run_id, memory_id in ids.items():
        memory = runtime.store.load_curated(memory_id)
        assert memory is not None, run_id
        assert memory.status is MemoryStatus.ARCHIVED, run_id
        assert runtime.store.count_links(memory_id, "contradicts") >= 2
        assert memory.status_reason and "contradicted" in memory.status_reason


def test_a_subject_shared_by_every_record_is_still_comparable(
    tmp_path: Path,
) -> None:
    """A document-frequency cap may cheapen the sweep, never blind it.

    Three records in one scope, all titled the same way: every salient token has
    a df of 3, above a 10% cap floored at 2, so indexing only under-cap tokens
    left the block list empty. The sweep then compared nothing, found no
    contradiction and reported success - the same silence the exact-fingerprint
    buckets produced, reached from the opposite direction, and the difference
    between a corpus that can retract a wrong conclusion and one that cannot.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_disagreement(runtime, 3)
    topics = {
        runtime.store.load_curated(memory_id).topic
        for memory_id in ids.values()
    }
    assert len(topics) == 1, "one subject shared by every record in the scope"

    report = ConflictDetector(runtime.store).run()

    assert report.scanned == 3
    assert report.groups, "the shared subject still produced a block"
    assert len(report.contradictions) == 3
    for memory_id in ids.values():
        assert runtime.store.count_links(memory_id, "contradicts") == 2
        memory = runtime.store.load_curated(memory_id)
        assert memory.status is MemoryStatus.ARCHIVED


def test_near_duplicates_merge_and_earn_their_second_run(tmp_path: Path) -> None:
    """A duplicate merge is corroboration: the loser's runs move to the winner."""
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_near_duplicate(runtime)
    assert ids["run-1"] != ids["run-2"]

    report = ConflictDetector(runtime.store).run()

    assert len(report.duplicates) == 1
    assert not report.contradictions
    assert report.superseded == [ids["run-2"]]

    winner = runtime.store.load_curated(ids["run-1"])
    loser = runtime.store.load_curated(ids["run-2"])
    assert winner is not None and loser is not None
    assert winner.distinct_runs == 2
    assert winner.status is MemoryStatus.ACTIVE
    assert loser.status is MemoryStatus.SUPERSEDED
    assert loser.superseded_by == winner.memory_id
    relations = {(row["memory_id"], row["related_memory_id"], row["relation"])
                 for row in runtime.store.list_links()}
    assert (winner.memory_id, loser.memory_id, "supersedes") in relations
    assert (loser.memory_id, winner.memory_id, "duplicate-of") in relations


def test_conflict_sweep_is_idempotent(tmp_path: Path) -> None:
    """A beat fires on a timer; re-running it must not keep decaying records."""
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_disagreement(runtime, 2)
    detector = ConflictDetector(runtime.store)

    detector.run()
    after_first = {
        memory_id: runtime.store.load_curated(memory_id).confidence
        for memory_id in ids.values()
    }
    second = detector.run()
    after_second = {
        memory_id: runtime.store.load_curated(memory_id).confidence
        for memory_id in ids.values()
    }

    assert after_first == after_second
    assert second.demoted == []
    assert runtime.store.count_links(ids["run-2"], "contradicts") == 1


# ------------------------------------------------------------ corroboration


def test_independent_agreement_links_supports_and_promotes(tmp_path: Path) -> None:
    """Two runs that agree in different words corroborate each other.

    They are too dissimilar to fold into one record, so before the supports band
    existed both stayed single-run ``candidate`` records forever and the
    ``candidate -> active`` threshold could only ever be met by byte-identical
    findings. The link lets the gate count each record's run toward the other's
    promotion without superseding either one.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_agreement(runtime)
    assert ids["run-1"] != ids["run-2"]
    # Each is a lone candidate before the sweep notices the agreement.
    for memory_id in ids.values():
        assert runtime.store.load_curated(memory_id).status is MemoryStatus.CANDIDATE

    report = ConflictDetector(runtime.store).run()

    assert len(report.supports) == 1
    assert not report.contradictions
    assert not report.duplicates
    assert not report.superseded
    assert not report.demoted

    # Both records survive - a supports link corroborates, it never folds - and
    # both are promoted on the strength of the other's run, with the reason
    # naming the link so the promotion is auditable.
    for memory_id in ids.values():
        memory = runtime.store.load_curated(memory_id)
        assert memory.status is MemoryStatus.ACTIVE
        assert memory.distinct_runs == 1
        assert memory.confidence == pytest.approx(0.85)
        assert memory.status_reason and "supports link" in memory.status_reason

    relations = {
        (row["memory_id"], row["related_memory_id"], row["relation"])
        for row in runtime.store.list_links()
    }
    assert (ids["run-1"], ids["run-2"], "supports") in relations
    assert (ids["run-2"], ids["run-1"], "supports") in relations


def test_supports_sweep_is_idempotent(tmp_path: Path) -> None:
    """A settled agreement re-scans cleanly: relinked, but never re-promoted."""
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_agreement(runtime)
    detector = ConflictDetector(runtime.store)

    detector.run()
    first = {
        memory_id: runtime.store.load_curated(memory_id).status
        for memory_id in ids.values()
    }
    second = detector.run()

    assert second.supports  # the pair is still recognised as corroboration
    assert not second.transitions  # but nothing moves again
    assert first == {
        memory_id: runtime.store.load_curated(memory_id).status
        for memory_id in ids.values()
    }
    assert runtime.store.count_links(ids["run-1"], "supports") == 1


def test_paraphrased_agreement_reads_as_corroboration(tmp_path: Path) -> None:
    """One conclusion, two runs, two phrasings: agreement, not refutation.

    This is the pair the real warm-up corpus produced. 0.32 Jaccard is below the
    conflict ceiling, so a similarity-only sweep called it a disagreement, halved
    both records' confidence and left both stuck at ``candidate`` - punishing the
    one thing the corpus had done right. The sweep now asks a second question: do
    the two share enough substance to be about the same mechanism, and does either
    side actually reject what the other asserts? Neither fires, so the link is
    ``supports`` and the gate promotes both on the strength of it.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_pair(runtime, _PARAPHRASE)
    assert ids["run-1"] != ids["run-2"]

    report = ConflictDetector(runtime.store).run()

    assert len(report.supports) == 1
    assert not report.contradictions
    assert not report.duplicates
    assert not report.demoted
    for memory_id in ids.values():
        memory = runtime.store.load_curated(memory_id)
        assert memory is not None
        assert memory.status is MemoryStatus.ACTIVE
        # Corroboration never costs confidence; only a refutation does.
        assert memory.confidence == pytest.approx(0.85)
        assert runtime.store.count_links(memory_id, "contradicts") == 0


def test_reworded_topics_still_corroborate(tmp_path: Path) -> None:
    """Blocking may not depend on the subject line matching verbatim.

    Records used to be bucketed by an exact topic fingerprint. Every seeded pair
    in this file gave both runs the same title, so the buckets always matched and
    the suite stayed green while the real corpus - where the model rewrites the
    finding title on every run - produced 53 singleton buckets, zero comparisons
    and zero promotions, and reported success doing it. These two runs share a
    subject and nothing verbatim.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_sides(
        runtime,
        (
            {"analysis": _PARAPHRASE[0], "title": _TOPIC_REWORDED[0]},
            {"analysis": _PARAPHRASE[1], "title": _TOPIC_REWORDED[1]},
        ),
    )
    assert ids["run-1"] != ids["run-2"]
    topics = {
        runtime.store.load_curated(memory_id).topic
        for memory_id in ids.values()
    }
    assert len(topics) == 2, "the two runs worded their finding differently"

    report = ConflictDetector(runtime.store).run()

    assert report.scanned == 2
    assert report.groups, "salient-token blocking found the pair anyway"
    assert len(report.supports) == 1
    assert not report.contradictions
    assert not report.duplicates
    for memory_id in ids.values():
        memory = runtime.store.load_curated(memory_id)
        assert memory.status is MemoryStatus.ACTIVE
        assert "supports link" in (memory.status_reason or "")


def test_a_shared_boilerplate_word_is_not_a_relation(tmp_path: Path) -> None:
    """Blocking finds a pair; relatedness decides whether it means anything.

    Two records that both mention a cold launch and agree on nothing else, whose
    contents describe different mechanisms. Judged on content alone the pair is a
    refutation: both records would lose half their confidence and start the
    countdown to being archived for a contradiction nobody ever made.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_sides(
        runtime,
        (
            {"analysis": _MECHANISMS[0], "title": _TOPIC_BOILERPLATE_ONLY[0]},
            {"analysis": _MECHANISMS[1], "title": _TOPIC_BOILERPLATE_ONLY[1]},
        ),
    )

    report = ConflictDetector(runtime.store).run()

    assert report.groups, "the shared words did put the pair in a block"
    assert not report.supports
    assert not report.contradictions
    assert not report.demoted
    assert not runtime.store.list_links()
    for memory_id in ids.values():
        memory = runtime.store.load_curated(memory_id)
        assert memory.status is MemoryStatus.CANDIDATE
        assert memory.confidence == pytest.approx(0.8)


def test_a_two_word_subject_does_not_match_the_whole_scope(tmp_path: Path) -> None:
    """Containment is not relatedness, or a terse topic pairs with everything.

    ``"white screen"`` is a substring of every subject in a white-screen corpus,
    so its overlap coefficient against the verbose record is a perfect 1.00. Only
    the Jaccard floor - 0.12 here - says the two subjects are not substantially
    alike.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_sides(
        runtime,
        (
            {"analysis": _MECHANISMS[0], "title": _TOPIC_DEGENERATE[0]},
            {"analysis": _MECHANISMS[1], "title": _TOPIC_DEGENERATE[1]},
        ),
    )

    report = ConflictDetector(runtime.store).run()

    assert report.groups
    assert not report.supports
    assert not report.contradictions
    assert not runtime.store.list_links()
    for memory_id in ids.values():
        assert (
            runtime.store.load_curated(memory_id).status is MemoryStatus.CANDIDATE
        )


def test_confirmation_on_either_side_of_a_supports_link_counts(
    tmp_path: Path,
) -> None:
    """One run confirmed the cause, the other suspected it. Same conclusion.

    Section 5.2 asks that at least one *source run* carried a confirmed finding.
    Once a supports link has established that two records are one conclusion
    reached independently, both records' runs are sources of it - so counting a
    neighbour's runs toward the run threshold while ignoring its confirmation
    split the group in half: the confirmed row promoted and the row that agreed
    from a ``suspected`` finding never did, on identical evidence for the claim.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_sides(
        runtime,
        (
            {
                "analysis": _PARAPHRASE[0],
                "title": _TOPIC_REWORDED[0],
                "status": "suspected",
                "confidence": 0.7,
            },
            {
                "analysis": _PARAPHRASE[1],
                "title": _TOPIC_REWORDED[1],
                "status": "confirmed",
                "confidence": 0.7,
            },
        ),
    )
    suspected = runtime.store.load_curated(ids["run-1"])
    assert not suspected.confirmed_run_ids
    assert suspected.status is MemoryStatus.CANDIDATE

    report = ConflictDetector(runtime.store).run()

    assert len(report.supports) == 1
    assert len(report.transitions) == 2, "both sides of one conclusion move"
    for memory_id in ids.values():
        memory = runtime.store.load_curated(memory_id)
        assert memory.status is MemoryStatus.ACTIVE
        assert "1 confirmed" in (memory.status_reason or "")
    # Borrowing the confirmation does not rewrite provenance: the suspected
    # record still cites no confirmed run of its own, so an auditor reading the
    # row can tell where the evidence actually came from.
    assert not runtime.store.load_curated(ids["run-1"]).confirmed_run_ids


def test_a_lone_suspected_run_still_cannot_promote(tmp_path: Path) -> None:
    """Aggregating confirmation must not let one guess mint its own prior.

    The other half of the rule above. Nothing corroborates this record, so the
    group is one run of one ``suspected`` finding at 0.9 confidence - exactly the
    self-contamination the gate exists to stop, and still a ``candidate``.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_sides(
        runtime,
        (
            {
                "analysis": _PARAPHRASE[0],
                "title": _TOPIC_REWORDED[0],
                "status": "suspected",
                "confidence": 0.9,
            },
        ),
    )

    report = ConflictDetector(runtime.store).run()

    assert not report.supports
    assert not report.transitions
    memory = runtime.store.load_curated(ids["run-1"])
    assert memory.status is MemoryStatus.CANDIDATE
    assert "gate not met" in (memory.status_reason or "")


def test_polarity_conflict_contradicts_above_the_similarity_ceiling(
    tmp_path: Path,
) -> None:
    """One side rules the cause out, the other asserts it. That is a refutation.

    0.64 Jaccard sits above the conflict ceiling, so similarity alone reads
    agreement here. The detector is built with a duplicate floor *below* the pair's
    score on purpose: the polarity check has to win before the merge, otherwise
    folding the two records into one deletes the disagreement from the corpus and
    no later evidence can bring it back.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_pair(runtime, _POLARITY)

    report = ConflictDetector(runtime.store, duplicate_similarity=0.6).run()

    assert len(report.contradictions) == 1
    assert not report.duplicates
    assert not report.supports
    assert "opposite sides" in report.contradictions[0].reason
    assert report.demoted, "the weaker side of a refutation pays for it"
    for memory_id in ids.values():
        assert runtime.store.count_links(memory_id, "contradicts") == 1


def test_different_mechanisms_still_contradict(tmp_path: Path) -> None:
    """Reading agreement as the default must not cost the corpus its retraction.

    Two mechanisms for one symptom, sharing a boilerplate opener and almost
    nothing else. No polarity signal - both sides assert - so this is the overlap
    floor doing the work, which is why it sits above 0.18 and not at zero.
    """
    runtime = MemoryRuntime(tmp_path)
    ids = _seed_pair(runtime, _MECHANISMS)

    report = ConflictDetector(runtime.store).run()

    assert len(report.contradictions) == 1
    assert not report.supports
    assert "different mechanisms" in report.contradictions[0].reason
    assert report.demoted


def test_inline_governance_replaces_the_missing_beat(tmp_path: Path) -> None:
    """A CLI run has no dashboard beat, so it has to sweep for itself.

    ``_remember_episode`` consolidates, enqueues a gate review and exits. In a
    deployment with no background process nothing ever read that queue - and, the
    half that actually mattered, nothing ever ran the sweep that produces
    ``supports`` links - so a corpus built from the command line stayed entirely
    ``candidate`` no matter how many runs agreed with each other. ``drain_inline``
    is the same two passes the beat runs, and the queue drain alone is not enough:
    the gate re-reads evidence, it does not create the links that evidence needs.
    """
    runtime = MemoryRuntime(tmp_path)
    for index, analysis in enumerate(_AGREEMENT, start=1):
        episode = _episode(
            episode_id=f"ep-{index}",
            run_id=f"run-{index}",
            analysis=analysis,
        )
        runtime.remember_episode(episode, consolidate=True)
        runtime.dreaming.enqueue_gate_review(episode.episode_id)

    queue_only = runtime.dreaming.drain_inline(maintain=False)

    assert not queue_only.transitions
    assert all(
        memory.status is MemoryStatus.CANDIDATE
        for memory in runtime.store.list_curated(active_only=False)
    )

    batch = runtime.dreaming.drain_inline()

    assert batch.supports == 1
    assert {item.current for item in batch.transitions} == {MemoryStatus.ACTIVE}
    assert all(
        memory.status is MemoryStatus.ACTIVE
        for memory in runtime.store.list_curated(active_only=False)
    )
    # The queue was already empty, so the second call reports no phantom jobs.
    assert batch.processed == 1


def test_inline_governance_is_on_by_default_and_switchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default on, because the deployments that need it cannot ask for it."""
    monkeypatch.delenv("TRACE_AGENT_MEMORY_INLINE_GOVERNANCE", raising=False)
    assert memory_inline_governance() is True
    monkeypatch.setenv("TRACE_AGENT_MEMORY_INLINE_GOVERNANCE", "0")
    assert memory_inline_governance() is False


# ------------------------------------------------------------- audit trail


def test_gate_promotion_is_attributed_to_the_gate(tmp_path: Path) -> None:
    """A promotion earned by evidence names the gate, not whatever wrote last."""
    runtime = MemoryRuntime(tmp_path)
    for index, run_id in enumerate(("run-1", "run-2"), start=1):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{index}",
                run_id=run_id,
                analysis=_READINGS[0],
                confidence=0.85,
            ),
            consolidate=True,
        )

    memory = runtime.list_curated()[0]
    versions = runtime.store.recent_versions(limit=100)

    assert memory.status is MemoryStatus.ACTIVE
    assert versions
    assert all(str(row.get("reason") or "").strip() for row in versions)
    gate_rows = [row for row in versions if row.get("actor") == GATE_ACTOR]
    assert gate_rows, versions
    assert any("independent runs agree" in str(row["reason"]) for row in gate_rows)


def test_conflict_edits_are_attributed_to_the_conflict_detector(
    tmp_path: Path,
) -> None:
    """Section 7's rule, checked against the version table rather than trusted."""
    runtime = MemoryRuntime(tmp_path)
    _seed_near_duplicate(runtime)

    report = ConflictDetector(runtime.store).run()
    versions = runtime.store.recent_versions(limit=100)

    assert report.transitions, "a merge that promotes must report the transition"
    assert versions
    assert all(str(row.get("reason") or "").strip() for row in versions)
    assert CONFLICT_ACTOR in {str(row.get("actor") or "") for row in versions}
    for transition in report.transitions:
        assert transition.reason
        assert transition.actor


# ----------------------------------------------------------------- retention


def _aged(memory: CuratedMemory, days: float) -> CuratedMemory:
    return memory.model_copy(update={"updated_at": utc_now() - timedelta(days=days)})


def test_retention_archives_an_unconfirmed_single_run_guess(
    tmp_path: Path,
) -> None:
    """The most dangerous kind of memory gets the shortest leash."""
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(
            episode_id="ep-1",
            run_id="run-1",
            analysis=_READINGS[0],
            status="suspected",
            confidence=0.45,
        ),
        consolidate=True,
    )
    memory = runtime.list_curated()[0]
    assert memory.status is MemoryStatus.CANDIDATE
    runtime.store.import_curated(_aged(memory, 400))

    report = RetentionService(
        runtime.store,
        retention_days=365,
        candidate_ttl_days=180,
        min_runs=2,
    ).run()

    assert report.stale_candidates == [memory.memory_id]
    retired = runtime.store.load_curated(memory.memory_id)
    assert retired is not None
    assert retired.status is MemoryStatus.ARCHIVED
    assert "retention" in (retired.status_reason or "")


def test_retention_stamps_a_deadline_then_honours_it(tmp_path: Path) -> None:
    """Corroborated memory is stamped first and archived only past the deadline."""
    runtime = MemoryRuntime(tmp_path)
    for index, run_id in enumerate(("run-1", "run-2"), start=1):
        runtime.remember_episode(
            _episode(
                episode_id=f"ep-{index}",
                run_id=run_id,
                analysis=_READINGS[0],
                confidence=0.85,
            ),
            consolidate=True,
        )
    memory = runtime.list_curated()[0]
    assert memory.status is MemoryStatus.ACTIVE
    service = RetentionService(
        runtime.store,
        retention_days=365,
        candidate_ttl_days=180,
        min_runs=2,
    )

    fresh = service.run()
    assert fresh.stamped == [memory.memory_id]
    assert fresh.expired == 0
    stamped = runtime.store.load_curated(memory.memory_id)
    assert stamped is not None and stamped.expires_at is not None
    assert stamped.status is MemoryStatus.ACTIVE

    # Stamping again is a no-op: a periodic sweep must not push every deadline
    # a further year out, or retention would never retain anything.
    assert service.run().stamped == []

    runtime.store.import_curated(_aged(stamped, 400))
    expired = service.run()
    assert expired.expired == 1
    retired = runtime.store.load_curated(memory.memory_id)
    assert retired is not None
    assert retired.status is MemoryStatus.ARCHIVED
    assert retired.archived_reason == "expired"


# --------------------------------------------------------- derived digests


def test_session_digest_is_active_deterministic_and_idempotent(
    tmp_path: Path,
) -> None:
    """A digest is a projection of stored turns, so it needs no corroboration."""
    runtime = MemoryRuntime(tmp_path)
    runtime.append_session_message("sess-1", "冷启动白屏，先看主线程", role="user")
    runtime.append_session_message(
        "sess-1", "已确认主线程同步读取配置文件", role="assistant"
    )

    summarizer = SessionSummarizer(runtime.store)
    first = summarizer.summarize("sess-1")
    assert first is not None and first.changed

    memory = runtime.store.load_curated(summary_memory_id("sess-1"))
    assert memory is not None
    assert memory.kind is MemoryKind.SUMMARY
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.confidence == pytest.approx(SUMMARY_CONFIDENCE)
    assert "冷启动白屏" in memory.content or "冷启" in memory.keywords

    session = runtime.store.load_session("sess-1")
    assert session is not None and session.summary == memory.content

    second = summarizer.summarize("sess-1")
    assert second is not None and not second.changed
    assert summarizer.summarize("missing") is None


def test_project_digest_is_active_deterministic_and_idempotent(
    tmp_path: Path,
) -> None:
    """The namespace digest is a projection of the corpus, so it needs no corroboration.

    Same contract as the session digest one level up: where a summary projects one
    session's turns, this projects every run and memory in the namespace, which is
    what lets a fresh analysis recall that the last twelve runs all landed on the
    same device and the same two root causes.
    """
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(episode_id="ep-1", run_id="run-1", analysis=_READINGS[0]),
        consolidate=True,
    )

    service = ProjectDigestService(runtime.store)
    first = service.digest()
    assert first is not None and first.changed
    memory_id = project_memory_id(str(runtime.store.namespace))
    assert first.memory_id == memory_id

    memory = runtime.store.load_curated(memory_id)
    assert memory is not None
    assert memory.kind is MemoryKind.PROJECT
    # Gate-exempt like ``SUMMARY``: no second run can corroborate a projection of
    # all the runs, so demanding one would strand it as a permanent candidate.
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.confidence == pytest.approx(PROJECT_CONFIDENCE)
    assert memory.scenario_type is None
    assert memory.provenance.created_by == PROJECT_ACTOR

    # The digest counts the corpus, never itself: including its own row would make
    # the second pass see a different corpus than the first and never settle.
    assert "memories: 1" in memory.content
    assert memory_id not in memory.content

    versions = len(runtime.store.recent_versions(limit=100))
    updated_at = memory.updated_at

    # An unchanged corpus writes nothing. Without this the beat would churn
    # ``updated_at`` forever, and freshness is a ranking input - a digest that
    # restamps itself outranks the corpus it is a digest of.
    second = service.digest()
    assert second is not None and not second.changed
    reread = runtime.store.load_curated(memory_id)
    assert reread is not None
    assert reread.content == memory.content
    assert reread.updated_at == updated_at
    assert len(runtime.store.recent_versions(limit=100)) == versions

    # A corpus that moved produces a different digest, on the same record: the id
    # is derived from the namespace, so refreshes update rather than pile up
    # near-duplicates for the conflict detector to sweep.
    runtime.remember_episode(
        _episode(episode_id="ep-2", run_id="run-2", analysis=_READINGS[1]),
        consolidate=True,
    )
    third = service.digest()
    assert third is not None and third.changed
    refreshed = runtime.store.load_curated(memory_id)
    assert refreshed is not None
    assert refreshed.content != memory.content
    assert "runs: 2" in refreshed.content
    # Both runs are cited, so the digest's provenance explains its own content.
    assert "run:run-1" in refreshed.provenance.references
    assert "run:run-2" in refreshed.provenance.references

    # An empty corpus says nothing at all: a digest whose every line reads ``0``
    # is a worse prior than no prior.
    empty = ProjectDigestService(MemoryRuntime(tmp_path / "empty").store)
    assert empty.digest() is None


# ------------------------------------------------------------------ composer


def test_composer_partitions_do_not_starve_each_other() -> None:
    """An oversized ``verified`` record must not consume the ``active`` budget."""
    hits = [
        RecallHit(
            memory=_curated(
                "mem-v", status=MemoryStatus.VERIFIED, content="甲" * 200
            ),
            score=0.9,
        ),
        RecallHit(
            memory=_curated(
                "mem-a", status=MemoryStatus.ACTIVE, content="乙" * 20
            ),
            score=0.8,
        ),
    ]

    composed = compose_memory(hits, budget=600)

    assert "mem-v" not in composed.injected_ids
    assert "mem-a" in composed.injected_ids
    assert composed.truncated
    assert compose_memory(hits, budget=0).text == ""
    assert compose_memory([], budget=600).text == ""


def test_composer_labels_every_trust_level_and_caps_candidates() -> None:
    """Three headings, three instructions, and at most two unverified guesses."""
    hits = [
        RecallHit(
            memory=_curated("mem-v", status=MemoryStatus.VERIFIED, content="v" * 10),
            score=0.9,
        ),
        RecallHit(
            memory=_curated("mem-a", status=MemoryStatus.ACTIVE, content="a" * 10),
            score=0.8,
        ),
    ]
    hits.extend(
        RecallHit(
            memory=_curated(
                f"mem-c{index}",
                status=MemoryStatus.CANDIDATE,
                content=f"c{index}" * 5,
            ),
            score=0.5,
        )
        for index in range(CANDIDATE_ITEM_LIMIT + 2)
    )

    composed = compose_memory(hits, budget=4000)

    text = composed.text
    assert text.index("已验证规律") < text.index("历史相似案例")
    assert text.index("历史相似案例") < text.index("未验证候选")
    assert "无条件丢弃" in text
    candidates = [
        item for item in composed.injected_ids if item.startswith("mem-c")
    ]
    assert len(candidates) == CANDIDATE_ITEM_LIMIT
    # Every rendered line carries its trust marker, so a reader - human or model
    # - can tell a corroborated pattern from one run's guess.
    for memory_id in composed.injected_ids:
        assert memory_id in text
    assert "| verified |" in text and "| candidate |" in text


def test_composer_reports_an_episode_confidence_not_its_rank_score() -> None:
    """``conf`` has to mean the same thing on both channels.

    An episode stores no confidence of its own, so its line derives one from its
    findings and reports the rerank result separately as ``score``. Printing the
    blended score under the ``conf`` label - which the first cut of section 6.4
    did - turned a retrieval artefact into a confidence claim: a production
    smoke run showed ``conf=0.66`` for an episode whose findings averaged 0.50,
    and the number in the prompt disagreed with the number the gate used.
    """
    episode = _episode(
        episode_id="ep-1",
        run_id="run-1",
        analysis=_READINGS[0],
        confidence=0.8,
    )

    text = compose_memory([RecallHit(memory=episode, score=0.42)], budget=2000).text

    assert "conf=0.80" in text
    assert "score=0.42" in text
    assert "conf=0.42" not in text


def test_budget_pressure_reaches_the_recall_result_as_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``truncated`` survives the trip from composer to the caller's result.

    Section 9 asks for the flag because "the right memory was recalled" and "the
    right memory was injected" are different claims, and only the second one can
    change a conclusion. The composer has always known it dropped items; the
    question is whether a caller reading ``RecallResult`` can tell, so this
    shrinks the prompt budget under a fixed corpus and checks the flag - and
    checks the roomy case does not set it, since a flag that is always true
    carries no information either.
    """
    runtime = MemoryRuntime(tmp_path)
    content = "main thread blocked by synchronous IO during cold start, " * 3
    for index in range(4):
        runtime.store.write_curated(
            _curated(
                f"mem-{index}",
                status=MemoryStatus.ACTIVE,
                topic=f"冷启动白屏 variant {index}",
                content=content.strip(),
            )
        )
    query = "冷启动白屏 main thread blocked by sync IO"

    roomy = runtime.recall(query, scenario_type="white_screen")

    assert len(roomy.hits) == 4
    assert roomy.truncated is False
    assert len(roomy.injected_ids) == len(roomy.hits)

    monkeypatch.setenv(MEMORY_PROMPT_CHARS_ENV, "140")
    tight = runtime.recall(query, scenario_type="white_screen")

    # Ranking does not read the prompt budget, so the hit list is unchanged and
    # only the rendered half of the answer shrinks.
    assert len(tight.hits) == len(roomy.hits)
    assert tight.truncated is True
    assert len(tight.injected_ids) < len(tight.hits)


# ------------------------------------------------------- beat, parity, CLI


def test_maintain_runs_the_whole_chain_once(tmp_path: Path) -> None:
    """``maintain()`` is the on-demand form of the beat, not a subset of it."""
    runtime = MemoryRuntime(tmp_path)
    runtime.remember_episode(
        _episode(
            episode_id="ep-1",
            run_id="run-1",
            analysis=_READINGS[0],
            status="suspected",
            confidence=0.45,
        ),
        consolidate=True,
    )
    runtime.append_session_message("sess-1", "冷启动白屏排查记录", role="user")

    batch = runtime.dreaming.maintain()

    assert batch.errors == 0
    assert batch.summarized == ["sess-1"]
    assert summary_memory_id("sess-1") in batch.created
    # The single-run guess stays a candidate: governance ran and promoted nothing.
    # The derived digests are filtered out because they are exempt from the gate's
    # corroboration rule and land ``active`` by design - a projection of the corpus
    # has no second independent run that could ever corroborate it.
    guess = [
        memory
        for memory in runtime.list_curated()
        if memory.kind not in DERIVED_DIGEST_KINDS
    ]
    assert guess and all(
        memory.status is MemoryStatus.CANDIDATE for memory in guess
    )
    # The batch is what lands in events and run.json, so it must serialise.
    json.dumps(batch.as_dict(), ensure_ascii=False)


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_governance_reaches_the_same_verdict_on_both_adapters(
    tmp_path: Path, kind: str
) -> None:
    """The JSON adapter is a rollback path, so it must govern identically."""
    root = tmp_path / "memory"
    store = SqliteStore(root) if kind == "sqlite" else JsonStore(root)
    runtime = MemoryRuntime(root, store=store)
    ids = _seed_disagreement(runtime, 2)

    ConflictDetector(runtime.store).run()

    verdicts = {
        run_id: (
            runtime.store.load_curated(memory_id).status.value,
            round(runtime.store.load_curated(memory_id).confidence, 4),
            runtime.store.count_links(memory_id, "contradicts"),
        )
        for run_id, memory_id in ids.items()
    }
    assert verdicts["run-1"] == ("candidate", 0.9, 1)
    assert verdicts["run-2"] == (
        "candidate",
        round(0.5 * CONFLICT_CONFIDENCE_PENALTY, 4),
        1,
    )
    assert all(
        str(row.get("reason") or "").strip()
        for row in runtime.store.recent_versions(limit=50)
    )


def test_maintain_command_reports_the_batch(tmp_path: Path) -> None:
    """The CLI is the same code path as the beat, and says what it changed."""
    root = tmp_path / "memory"
    runtime = MemoryRuntime(root)
    runtime.remember_episode(
        _episode(
            episode_id="ep-1",
            run_id="run-1",
            analysis=_READINGS[0],
            status="suspected",
            confidence=0.45,
        ),
        consolidate=True,
    )
    runner = CliRunner()

    as_json = runner.invoke(
        memory_app, ["maintain", "--root", str(root), "--json"]
    )
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.output)
    assert payload["errors"] == 0
    assert payload["stats"]["memories"] >= 1

    human = runner.invoke(memory_app, ["maintain", "--root", str(root)])
    assert human.exit_code == 0, human.output
    assert "治理完成" in human.output
    assert "信任跃迁" in human.output
