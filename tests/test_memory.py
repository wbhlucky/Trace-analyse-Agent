from __future__ import annotations

from pathlib import Path

from trace_agent.memory import (
    MemoryRuntime,
    build_episode,
    default_memory_runtime,
)
from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    MemoryStatus,
    Provenance,
    RecallHit,
)
from trace_agent.memory.recall import _apply_type_quota
from trace_agent.models import (
    AgentKind,
    AnalysisResult,
    AnalyzeRequest,
    Finding,
    FindingSeverity,
    FindingStatus,
    ScenarioType,
)


def _request() -> AnalyzeRequest:
    return AnalyzeRequest(
        trace_id="trace-1",
        trace_path=Path("/tmp/trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="open app",
        symptom="long white screen",
        output_dir=Path("/tmp/case"),
        target_process="com.example.app",
        device="phone",
        build="1.0.0",
        agent=AgentKind.LOCAL,
    )


def _analysis() -> AnalysisResult:
    return AnalysisResult(
        summary="cold start is slow",
        findings=[
            Finding(
                title="MainThread blocked",
                severity=FindingSeverity.HIGH,
                status=FindingStatus.CONFIRMED,
                confidence=0.9,
                evidence_ids=["ev-0001"],
                analysis="main thread blocked on IO",
                recommendation="move IO off main thread",
                verification="manual",
            )
        ],
    )


def test_episode_backend_roundtrip(tmp_path: Path) -> None:
    backend = JsonMemoryBackend(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    backend.write_episode(episode)
    loaded = backend.load_episode(episode.episode_id)
    assert loaded is not None
    assert loaded.run_id == "run-1"
    assert loaded.scenario_type == "cold-start"
    assert loaded.root_causes
    assert loaded.findings[0]["title"] == "MainThread blocked"


def test_consolidation_is_idempotent_for_one_run(tmp_path: Path) -> None:
    """Replaying one run must not look like a second, independent confirmation.

    This used to assert ``occurrences == 2`` and ``status is ACTIVE`` after
    consolidating the *same* episode twice, which is exactly the
    self-contamination bug: a single ``suspected`` guess became a trusted prior
    before any other run had seen it.
    """
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    first = runtime.remember_episode(episode, consolidate=True)
    second = runtime.remember_episode(episode, consolidate=True)
    # Consolidation now has two products: the finding-derived memory these
    # assertions are about, plus the ``profile`` record for the environment the
    # run reported (section 2.3's device/build facts). Filtering by kind keeps
    # the test about the finding instead of loosening its count.
    findings = [item for item in first if item.kind is not MemoryKind.PROFILE]
    assert len(findings) == 1
    assert [item.memory_id for item in first] == [
        item.memory_id for item in second
    ]
    replayed = [item for item in second if item.kind is not MemoryKind.PROFILE]
    assert replayed[0].occurrences == 1
    assert replayed[0].distinct_runs == 1
    assert replayed[0].status is MemoryStatus.CANDIDATE
    # The profile record obeys the same rule for the same reason: replaying one
    # run re-observes an environment, it does not corroborate it.
    profile = next(
        item for item in second if item.kind is MemoryKind.PROFILE
    )
    assert profile.distinct_runs == 1
    assert profile.status is MemoryStatus.CANDIDATE


def test_consolidation_promotes_across_independent_runs(tmp_path: Path) -> None:
    """Two different runs agreeing is real evidence and does earn ``active``."""
    runtime = MemoryRuntime(tmp_path)
    episodes = [
        build_episode(
            _request(),
            _analysis(),
            run_id=run_id,
            output_dir=str(tmp_path / "case"),
        )
        for run_id in ("run-1", "run-2")
    ]
    runtime.remember_episode(episodes[0], consolidate=True)
    promoted = runtime.remember_episode(episodes[1], consolidate=True)
    assert len(promoted) == 2
    finding = next(
        item for item in promoted if item.kind is not MemoryKind.PROFILE
    )
    assert finding.distinct_runs == 2
    assert finding.occurrences == 2
    assert finding.status is MemoryStatus.ACTIVE
    # Same environment, second run: the profile id is stable on the attribute
    # tuple rather than on the episode, so the two runs merge into one record
    # that earns ``active`` on its own evidence.
    profile = next(item for item in promoted if item.kind is MemoryKind.PROFILE)
    assert profile.distinct_runs == 2
    assert profile.status is MemoryStatus.ACTIVE


def test_recall_ranks_curated_and_episodic(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    # A curated memory is only recallable once it has earned trust, so the
    # fixture needs two independent runs rather than one.
    for run_id in ("run-1", "run-2"):
        runtime.remember_episode(
            build_episode(
                _request(),
                _analysis(),
                run_id=run_id,
                output_dir=str(tmp_path / "case"),
            ),
            consolidate=True,
        )
    result = runtime.recall(
        "main thread blocked cold start",
        scenario_type="cold-start",
    )
    assert result.hits
    assert "MainThread blocked" in result.context_prompt
    assert any(
        hit.memory.kind is MemoryKind.PROCEDURE
        for hit in result.hits
        if isinstance(hit.memory, CuratedMemory)
    )


def test_recall_is_empty_for_unrelated_query(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    runtime.remember_episode(episode, consolidate=True)
    result = runtime.recall("frame jank rendering fps")
    assert result.hits == []


def test_default_runtime_uses_project_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    runtime = default_memory_runtime(Path.cwd())
    assert runtime.backend.root == tmp_path / ".trace-agent" / "memory"


# ---------------------------------------------------------------- top-k quota


def _curated_hit(memory_id: str, kind: MemoryKind, score: float) -> RecallHit:
    return RecallHit(
        memory=CuratedMemory(
            memory_id=memory_id,
            kind=kind,
            topic=f"topic {memory_id}",
            content=f"content {memory_id}",
            confidence=0.8,
            status=MemoryStatus.ACTIVE,
            provenance=Provenance(source_type="finding", references=["run-1"]),
        ),
        score=score,
    )


_MIXED = (
    ("mem-p1", MemoryKind.PATTERN, 0.95),
    ("mem-p2", MemoryKind.PATTERN, 0.90),
    ("mem-p3", MemoryKind.PATTERN, 0.85),
    ("mem-f1", MemoryKind.FACT, 0.80),
    ("mem-r1", MemoryKind.PROCEDURE, 0.70),
)


def test_type_quota_gives_a_minority_type_a_slot() -> None:
    """Section 6.3: four near-identical patterns may not fill every slot.

    ``pattern`` and ``fact`` are both ``semantic``, so on score alone the top
    four are all semantic and the one ``procedural`` memory - the one that says
    how to investigate - is exactly what a diagnosis prompt needs and exactly
    what an unquota'd cut drops. The quota admits two per type first, then
    fills the remaining slot from the deferred hits.
    """
    hits = [_curated_hit(*item) for item in _MIXED]

    chosen = _apply_type_quota(hits, top_k=4, quota=2)

    assert [hit.memory.memory_id for hit in chosen] == [
        "mem-p1",
        "mem-p2",
        "mem-p3",
        "mem-r1",
    ]
    assert "mem-f1" not in [hit.memory.memory_id for hit in chosen]


def test_type_quota_never_shrinks_the_cut() -> None:
    """Diversity reorders top-k; it may not cost a recall its hits.

    A corpus whose relevant memories are genuinely all one type still returns
    ``top_k`` of them, because the second pass refills from the deferred hits.
    Without that pass the quota would read as a ceiling and a legitimate
    single-type recall would silently lose results. ``quota=0`` is the off
    switch and must reproduce the plain score-ordered cut.
    """
    same_type = [_curated_hit(*item) for item in _MIXED[:4]]

    filled = _apply_type_quota(same_type, top_k=4, quota=2)
    assert [hit.memory.memory_id for hit in filled] == [
        "mem-p1",
        "mem-p2",
        "mem-p3",
        "mem-f1",
    ]

    hits = [_curated_hit(*item) for item in _MIXED]
    assert [hit.memory.memory_id for hit in _apply_type_quota(hits, top_k=4, quota=0)] == [
        "mem-p1",
        "mem-p2",
        "mem-p3",
        "mem-f1",
    ]
    assert _apply_type_quota(hits, top_k=0, quota=2) == []
