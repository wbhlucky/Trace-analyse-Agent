"""P1 acceptance: the storage substrate, its index, and the migration between them.

The claims under test are the ones section 11 of the design doc lists as P1
exit criteria, plus the two properties that make the substrate safe to depend on:

* **Adapter parity.** ``TRACE_AGENT_MEMORY_STORE=json`` is advertised as a
  one-variable rollback. That is only true if the JSON scan and the SQLite index
  return the *same* hits for the *same* query - not merely similar ones. Every
  parity assertion here compares ids and scores exactly, with timestamps pinned so
  freshness cannot introduce drift.
* **Migration is boring.** Idempotent, backed up, count-verified, timestamp
  preserving. A migration that restamps ``updated_at`` silently reorders recall,
  which is exactly the kind of change nobody thinks to look for afterwards.

The scale test is last and deliberately expensive: it is the only place in the
suite that proves retrieval is indexed rather than linear.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trace_agent.memory.flags import MEMORY_STORE_ENV
from trace_agent.memory.migrate import (
    BUNDLE_VERSION,
    DEMOTION_REASON,
    MIGRATE_ACTOR,
    demote_unexplained_trust,
    export_bundle,
    import_bundle,
    migrate_corpus,
    migrate_store,
    write_export,
)
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    MemoryStatus,
    Provenance,
    SessionMemory,
)
from trace_agent.memory.ports import (
    MemoryQuery,
    MemoryRelation,
    store_supports_pushdown,
)
from trace_agent.memory.runtime import MemoryRuntime
from trace_agent.memory.store import (
    JsonStore,
    SqliteStore,
    build_memory_store,
    store_diagnostics,
)
from trace_agent.memory.store.rows import VersionResolver

# Pinned so both adapters hold byte-identical timestamps. Freshness is a ranking
# signal, so letting each store stamp "now" would make parity assertions flaky by
# a few hundred microseconds of decay.
FIXED = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

CORPUS: tuple[tuple[str, str, str, list[str], MemoryStatus, float], ...] = (
    ("mem-cold", "冷启动白屏", "main thread blocked by sync IO during cold start",
     ["冷启动", "白屏", "主线程"], MemoryStatus.ACTIVE, 0.82),
    ("mem-jank", "列表滑动掉帧", "excessive layout inflation in recycler view",
     ["掉帧", "列表", "布局"], MemoryStatus.ACTIVE, 0.71),
    ("mem-leak", "内存泄漏", "unbounded bitmap cache never evicted",
     ["内存", "泄漏", "缓存"], MemoryStatus.VERIFIED, 0.93),
    ("mem-guess", "冷启动白屏", "unverified single-run guess about network",
     ["冷启动", "网络"], MemoryStatus.CANDIDATE, 0.40),
)


def _memory(
    memory_id: str,
    topic: str,
    content: str,
    keywords: list[str],
    status: MemoryStatus,
    confidence: float,
    *,
    scenario_type: str = "cold_start",
    runs: int = 1,
) -> CuratedMemory:
    return CuratedMemory(
        memory_id=memory_id,
        kind=MemoryKind.PATTERN,
        scenario_type=scenario_type,
        topic=topic,
        content=content,
        keywords=keywords,
        confidence=confidence,
        status=status,
        provenance=Provenance(
            source_type="episode",
            references=[f"run:r-{memory_id}-{i}" for i in range(runs)]
            + [f"ep-{memory_id}"],
        ),
        created_at=FIXED,
        updated_at=FIXED,
    )


def _episode(
    episode_id: str,
    run_id: str,
    scenario: str,
    symptom: str,
    *,
    session_id: str | None = None,
    project_id: str | None = None,
) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=episode_id,
        run_id=run_id,
        trace_id="trace-1",
        scenario_type="cold_start",
        scenario=scenario,
        symptom=symptom,
        summary="main thread blocked",
        root_causes=["main thread blocked by sync IO"],
        findings=[
            {
                "title": "Main thread blocked by sync IO",
                "severity": "high",
                "status": "confirmed",
                "confidence": 0.82,
                "analysis": "sync file read on main thread during launch",
                "recommendation": "load asynchronously",
                "evidence_ids": ["ev-1"],
            }
        ],
        created_at=FIXED,
        session_id=session_id,
        project_id=project_id,
    )


def _session(
    session_id: str,
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
) -> SessionMemory:
    return SessionMemory(
        session_id=session_id,
        title="cold start investigation",
        topic="冷启动白屏",
        created_at=FIXED,
        updated_at=FIXED,
        agent_id=agent_id,
        user_id=user_id,
        project_id=project_id,
    )


def _seed(store: object) -> None:
    """Write the shared corpus through the timestamp-preserving importers.

    ``write_curated`` restamps ``updated_at`` on both adapters, which would make
    the parity tests compare two slightly different corpora. Going through
    ``import_*`` is also what migration itself does, so this exercises the real
    path.
    """
    for row in CORPUS:
        memory = _memory(*row)
        importer = getattr(store, "import_curated", None)
        if callable(importer):
            importer(memory)
        else:  # pragma: no cover - both adapters implement it
            store.write_curated(memory)
    store.write_episode(_episode("ep-1", "run-1", "冷启动白屏", "启动后长时间白屏"))
    store.write_episode(_episode("ep-2", "run-2", "列表滑动掉帧", "滑动时明显卡顿"))
    importer = getattr(store, "import_session", None)
    session = _session("sess-1")
    if callable(importer):
        importer(session)
    else:  # pragma: no cover - both adapters implement it
        store.write_session(session)


def _hit_keys(result: object) -> list[tuple[str, float]]:
    """Comparable identity for a recall result: which records, in what order, at what score."""
    keys: list[tuple[str, float]] = []
    for hit in result.hits:
        memory = hit.memory
        ident = getattr(memory, "memory_id", None) or getattr(memory, "episode_id", "")
        keys.append((str(ident), round(float(hit.score), 9)))
    return keys


QUERIES = (
    "冷启动白屏",
    "冷启动 白屏",
    "启动白屏问题排查",
    "白屏",
    "列表滑动掉帧 卡顿",
    "内存泄漏 缓存",
    "completely unrelated query about databases",
)


@pytest.fixture
def json_runtime(tmp_path: Path) -> MemoryRuntime:
    store = JsonStore(tmp_path / "memory")
    _seed(store)
    return MemoryRuntime(tmp_path / "memory", store=store)


@pytest.fixture
def sqlite_runtime(tmp_path: Path) -> MemoryRuntime:
    store = SqliteStore(tmp_path / "memory")
    _seed(store)
    return MemoryRuntime(tmp_path / "memory", store=store)


# --------------------------------------------------------------------- parity
def test_json_and_sqlite_recall_are_identical(
    json_runtime: MemoryRuntime, sqlite_runtime: MemoryRuntime
) -> None:
    """The rollback promise, stated as an executable claim.

    If these two ever diverge, ``TRACE_AGENT_MEMORY_STORE=json`` stops being a
    rollback and becomes a behaviour change made under incident pressure.
    """
    for query in QUERIES:
        expected = _hit_keys(
            json_runtime.recall(query, scenario_type="cold_start", allow_deep=False)
        )
        actual = _hit_keys(
            sqlite_runtime.recall(query, scenario_type="cold_start", allow_deep=False)
        )
        assert actual == expected, f"query {query!r}: sqlite={actual} json={expected}"


def test_pushdown_and_scan_agree_on_one_store(
    sqlite_runtime: MemoryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index must not change the answer, only how fast it is found.

    Same store, same corpus, two retrieval paths: the default relevance floor
    sits above ``PHRASE_WEIGHT`` so ``search`` is used, and a floor of zero forces
    the scan fallback. Identical results mean the FTS document covers every field
    the scorer reads.
    """
    store = sqlite_runtime.store
    assert store_supports_pushdown(store)
    indexed = _hit_keys(
        sqlite_runtime.recall("冷启动 白屏", scenario_type="cold_start", allow_deep=False)
    )
    assert indexed, "the indexed path returned nothing for a CJK query"

    monkeypatch.setenv("TRACE_AGENT_MEMORY_MIN_SCORE", "0")
    scan_runtime = MemoryRuntime(store.root, store=store)
    scanned = _hit_keys(
        scan_runtime.recall("冷启动 白屏", scenario_type="cold_start", allow_deep=False)
    )

    assert set(key for key, _ in indexed) <= set(key for key, _ in scanned)
    assert [key for key, _ in indexed] == [key for key, _ in scanned][: len(indexed)]


def test_cjk_recall_survives_resegmentation(sqlite_runtime: MemoryRuntime) -> None:
    """Section 1.2 defect B, now answered by the index rather than by luck.

    A space inserted mid-phrase and a natural rewrite must both still find the
    memory whose topic contains the unbroken string.
    """
    for query in ("冷启动白屏", "冷启动 白屏", "启动白屏问题排查"):
        result = sqlite_runtime.recall(
            query, scenario_type="cold_start", allow_deep=False
        )
        ids = [
            getattr(hit.memory, "memory_id", None) or hit.memory.episode_id
            for hit in result.hits
        ]
        assert "mem-cold" in ids, f"query {query!r} missed mem-cold: {ids}"


def test_candidate_memories_are_not_recalled_by_default(
    sqlite_runtime: MemoryRuntime,
) -> None:
    """Trust separation has to hold on the indexed path too.

    The store filters by trust inside SQL now, so a bug here is invisible to the
    scan-based tests that covered it in P0.
    """
    result = sqlite_runtime.recall(
        "冷启动 白屏 网络", scenario_type="cold_start", allow_deep=False
    )
    ids = [
        getattr(hit.memory, "memory_id", None) or hit.memory.episode_id
        for hit in result.hits
    ]
    assert "mem-guess" not in ids


def test_search_filters_by_scenario_and_trust(sqlite_runtime: MemoryRuntime) -> None:
    """Filters are pushed into SQL, so they need their own assertion."""
    store = sqlite_runtime.store
    hits = store.search(
        MemoryQuery(
            text="白屏",
            scenario_type="cold_start",
            statuses=frozenset({MemoryStatus.ACTIVE}),
            limit=50,
        )
    )
    ids = {
        getattr(item.memory, "memory_id", None)
        or getattr(item.memory, "episode_id", "")
        for item in hits
    }
    assert "mem-cold" in ids
    assert "mem-leak" not in ids, "verified memory leaked through an active-only filter"
    assert "mem-guess" not in ids, "candidate memory leaked through an active-only filter"

    other = store.search(
        MemoryQuery(text="白屏", scenario_type="jank", limit=50)
    )
    assert all(
        getattr(item.memory, "scenario_type", "jank") in (None, "", "jank")
        for item in other
    )


# ------------------------------------------------------------------ namespace
def test_namespaces_do_not_cross(tmp_path: Path) -> None:
    """Section 8's isolation guarantee, at the storage layer.

    Eval must not recall production memory. That used to be enforced by pointing
    eval at a different directory; it is now a column, so it needs a test that
    would fail if the column were dropped from a WHERE clause.
    """
    root = tmp_path / "memory"
    production = SqliteStore(root, namespace="project:default")
    _seed(production)
    isolated = SqliteStore(root, namespace="eval:run-7:case-1")

    assert isolated.stats().memories == 0
    assert isolated.search(MemoryQuery(text="冷启动白屏", limit=50)) == []
    assert isolated.list_curated() == []
    assert production.stats().memories == len(CORPUS)


# ------------------------------------------------------------------- migration
def test_migration_preserves_counts_and_is_idempotent(tmp_path: Path) -> None:
    """P1 acceptance: post-migration ``stats`` equals the source counts.

    Counts, kinds and timestamps travel verbatim. Trust does not, and that is
    section 12.4 rather than a regression: the seeded corpus is the defect-A
    shape - one run per record, no audit trail behind it, ``active``/``verified``
    statuses the current gate would not award from that evidence - so a governed
    migration demotes all three. "Preserves counts" and "preserves trust" are
    two different claims now; :func:`test_migration_keep_trust_preserves_legacy_statuses`
    covers the opt-out that keeps the second one.
    """
    root = tmp_path / "memory"
    source = JsonStore(root)
    _seed(source)
    before = source.stats()

    report = migrate_store(root, source_kind="json", target_kind="sqlite")
    assert report.ok, report.as_dict()
    assert report.memories == before.memories
    assert report.episodes == before.episodes
    assert report.sessions == before.sessions
    assert report.verified
    assert report.mismatch == []

    target = SqliteStore(root)
    after = target.stats()
    assert (after.memories, after.episodes, after.sessions) == (
        before.memories,
        before.episodes,
        before.sessions,
    )
    assert after.by_kind == before.by_kind
    # mem-cold/mem-jank were ``active`` and mem-leak ``verified``, each resting
    # on a single run with no version row to explain it; mem-guess was already a
    # candidate, so it has nothing to lose.
    assert report.demoted == 3
    assert after.by_trust == {"candidate": before.memories}
    demoted = target.load_curated("mem-cold")
    assert demoted is not None and demoted.status is MemoryStatus.CANDIDATE
    # What the source claimed is not thrown away: it stays on the record, so a
    # later promotion can be argued about with the original status in view.
    assert DEMOTION_REASON in (demoted.status_reason or "")
    assert "was active" in (demoted.status_reason or "")
    rows = [
        row
        for row in target.recent_versions(limit=100)
        if row["memory_id"] == "mem-cold"
    ]
    assert len(rows) == 1
    # Section 14 scores the corpus on attributable transitions, and a demotion
    # is one: it carries the migrate actor, not a blank.
    assert rows[0]["actor"] == MIGRATE_ACTOR
    assert rows[0]["reason"] == DEMOTION_REASON
    versions_before = len(target.recent_versions(limit=100))

    # Re-running must not duplicate rows or inflate the run evidence that the
    # quality gate reads. The *source* still says ``active``, so the report still
    # counts three demotions; what must not happen is a second version row for a
    # record that already landed where this run would put it.
    again = migrate_store(root, source_kind="json", target_kind="sqlite")
    assert again.ok, again.as_dict()
    assert again.demoted == report.demoted
    assert target.stats().memories == before.memories
    assert target.distinct_runs("mem-cold") == 1
    assert len(target.recent_versions(limit=100)) == versions_before


def test_migration_keep_trust_preserves_legacy_statuses(tmp_path: Path) -> None:
    """``--keep-trust`` is a real opt-out, not a rename of the default.

    The flag exists for the operator who is moving a corpus they have already
    governed by hand and does not want the gate second-guessing it mid-move.
    Asserting the counts alone would pass with the flag ignored, so this pins the
    statuses too - including ``verified``, the one level a silent demotion would
    hurt most, since it is the level recall injects as a prior.
    """
    root = tmp_path / "memory"
    source = JsonStore(root)
    _seed(source)
    before = source.stats()

    report = migrate_store(
        root, source_kind="json", target_kind="sqlite", keep_trust=True
    )
    assert report.ok, report.as_dict()
    assert report.demoted == 0

    target = SqliteStore(root)
    after = target.stats()
    assert after.by_trust == before.by_trust
    assert after.by_kind == before.by_kind
    carried = target.load_curated("mem-leak")
    assert carried is not None and carried.status is MemoryStatus.VERIFIED
    assert DEMOTION_REASON not in (carried.status_reason or "")


def test_demotion_rule_table() -> None:
    """The ways a trusted status survives section 12.4, and the one that does not.

    ``demote_unexplained_trust`` is the whole of the migration's trust policy, so
    its branches are pinned one row at a time rather than through a corpus: a
    corpus-level test can pass with a branch silently widened, because the other
    rows still add up to the expected totals.
    """

    def record(
        memory_id: str,
        status: MemoryStatus,
        *,
        kind: MemoryKind = MemoryKind.PATTERN,
        runs: int = 1,
        confidence: float = 0.8,
    ) -> CuratedMemory:
        return _memory(
            memory_id,
            "冷启动白屏",
            "main thread blocked by sync IO during cold start",
            ["冷启动", "白屏"],
            status,
            confidence,
            runs=runs,
        ).model_copy(
            update={
                "kind": kind,
                "confirmed_run_ids": [f"run-{i}" for i in range(runs)],
            }
        )

    # One run, no audit trail, nothing the current gate would accept: the
    # defect-A shape, and the only row that loses its status.
    demoted_record, demoted = demote_unexplained_trust(
        record("mem-legacy", MemoryStatus.ACTIVE)
    )
    assert demoted is True
    assert demoted_record.status is MemoryStatus.CANDIDATE
    assert DEMOTION_REASON in (demoted_record.status_reason or "")
    assert "was active" in (demoted_record.status_reason or "")

    # A version row showing the transition into ``verified`` is evidence enough:
    # a corpus somebody already governed must migrate verbatim.
    earned = record("mem-earned", MemoryStatus.VERIFIED)
    kept, demoted = demote_unexplained_trust(earned, {"mem-earned": {"verified"}})
    assert demoted is False and kept is earned

    # Two confirmed independent runs at a confident score: the gate would award
    # ``active`` from exactly this evidence today, so the status is not a lie.
    evidenced = record("mem-evidenced", MemoryStatus.ACTIVE, runs=2)
    kept, demoted = demote_unexplained_trust(evidenced)
    assert demoted is False and kept is evidenced

    # Derived digests are gate-exempt; demoting one here would strand a record
    # the gate itself keeps ``active``.
    for kind in (MemoryKind.SUMMARY, MemoryKind.PROJECT):
        digest = record(f"mem-{kind.value}", MemoryStatus.ACTIVE, kind=kind)
        kept, demoted = demote_unexplained_trust(digest)
        assert demoted is False and kept is digest

    # A candidate has nothing to lose and an archived record is retired rather
    # than distrusted, so neither is in scope.
    for status in (MemoryStatus.CANDIDATE, MemoryStatus.ARCHIVED):
        plain = record(f"mem-{status.value}", status)
        kept, demoted = demote_unexplained_trust(plain)
        assert demoted is False and kept is plain


def test_migration_writes_a_backup_first(tmp_path: Path) -> None:
    """The rollback artefact has to exist before the copy, not after."""
    root = tmp_path / "memory"
    source = JsonStore(root)
    _seed(source)

    report = migrate_store(root, source_kind="json", target_kind="sqlite")
    assert report.backup is not None
    assert report.backup.is_file()
    assert report.backup.parent.name == "backups"
    bundle = export_bundle(source)
    assert len(bundle["memories"]) == report.memories


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """A dry run reports what would happen without changing the corpus.

    The empty database file *is* created, and that is asserted rather than
    overlooked: ``build_memory_store`` probes the schema to find out whether FTS5
    exists, and probing needs a connection. What must not happen is records
    appearing, or a backup being taken for a migration that never ran.
    """
    root = tmp_path / "memory"
    source = JsonStore(root)
    _seed(source)
    before = source.stats()

    report = migrate_store(root, source_kind="json", target_kind="sqlite", dry_run=True)
    assert report.dry_run
    assert report.memories == len(CORPUS)
    assert report.backup is None
    assert report.collapsed_runs == 0

    target = SqliteStore(root)
    assert target.stats().memories == 0
    assert target.stats().episodes == 0
    assert target.stats().sessions == 0
    assert source.stats().memories == before.memories


def test_migration_preserves_updated_at(tmp_path: Path) -> None:
    """Freshness is a ranking signal, so migration must not restamp it."""
    root = tmp_path / "memory"
    source = JsonStore(root)
    _seed(source)
    migrate_store(root, source_kind="json", target_kind="sqlite")

    target = SqliteStore(root)
    migrated = target.load_curated("mem-cold")
    assert migrated is not None
    assert migrated.updated_at == FIXED


def test_export_import_roundtrip_between_adapters(tmp_path: Path) -> None:
    """SQLite -> JSON is a real direction: it is how the rollback gets populated."""
    root = tmp_path / "memory"
    source = SqliteStore(root)
    _seed(source)

    bundle_path = tmp_path / "snapshot.json"
    write_export(source, bundle_path)

    other_root = tmp_path / "restored"
    restored = JsonStore(other_root)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    report = import_bundle(restored, bundle)
    assert not report.errors
    assert report.memories == len(CORPUS)
    assert restored.load_curated("mem-cold").updated_at == FIXED
    assert restored.stats().memories == source.stats().memories


def test_collapsed_runs_are_reported_not_hidden(tmp_path: Path) -> None:
    """Two projections of one run become one row, and the report says so.

    ``episodes`` carries ``UNIQUE(namespace, run_id)``. Silently dropping a row
    would make the count verification look like data loss, so the collapse is
    counted and subtracted from the expectation instead.
    """
    root = tmp_path / "memory"
    source = JsonStore(root)
    source.write_episode(_episode("ep-a", "run-same", "冷启动白屏", "白屏"))
    source.write_episode(_episode("ep-b", "run-same", "冷启动白屏", "白屏再次"))

    report = migrate_store(root, source_kind="json", target_kind="sqlite")
    assert report.episodes == 2
    assert report.collapsed_runs == 1
    assert report.ok, report.as_dict()
    assert SqliteStore(root).stats().episodes == 1


def test_migration_into_a_target_already_in_use_verifies_clean(tmp_path: Path) -> None:
    """Verification has to survive a target that has been live since P1.

    This is the production shape, not a hypothetical one: ``memory.sqlite3`` had
    taken 180 episodes through the runtime while 46 curated memories and 70
    episodes still sat in the legacy JSON tree nobody had migrated. Comparing the
    target's counts against what this run copied then reported
    ``episodes: target=250 copied=70``, and the CLI turned that into "do not
    switch store" - a migration condemned for having already succeeded once.
    Reading records back by id passes, because a populated target is simply a
    target with extra rows, and it stays a real check: a record that landed under
    a different id is still missing here, whereas counting would wave it through
    whenever something else balanced it.
    """
    root = tmp_path / "memory"

    # The legacy tree: what migration is being asked to bring over.
    source = JsonStore(root)
    source.import_curated(
        _memory(
            "mem-legacy",
            "冷启动白屏",
            "sync file read on main thread during launch",
            ["白屏", "冷启动"],
            MemoryStatus.CANDIDATE,
            0.45,
        )
    )
    source.write_episode(_episode("ep-legacy", "run-legacy", "冷启动白屏", "启动后长时间白屏"))
    source.import_session(_session("sess-legacy"))

    # Written through SQLite by the running agent, so absent from the source.
    live = SqliteStore(root)
    live.import_curated(
        _memory(
            "mem-live",
            "内存泄漏",
            "unbounded bitmap cache never evicted",
            ["泄漏", "内存"],
            MemoryStatus.ACTIVE,
            0.8,
        )
    )
    live.write_episode(_episode("ep-live", "run-live", "列表滑动掉帧", "滑动时明显卡顿"))
    before = live.stats()
    assert before.memories == 1 and before.episodes == 1 and before.sessions == 0

    report = migrate_store(root, source_kind="json", target_kind="sqlite")
    assert report.ok, report.as_dict()
    assert report.verified
    assert report.mismatch == []
    assert (report.memories, report.episodes, report.sessions) == (1, 1, 1)

    after = live.stats()
    assert (after.memories, after.episodes, after.sessions) == (2, 2, 1)
    # The merge is additive: nothing the live corpus held was displaced.
    assert live.load_curated("mem-live") is not None
    assert live.load_curated("mem-legacy") is not None
    assert live.load_session("sess-legacy") is not None


def test_refuses_to_migrate_across_namespaces(tmp_path: Path) -> None:
    """Copying one isolation domain into another would defeat the point of both."""
    root = tmp_path / "memory"
    source = SqliteStore(root, namespace="project:a")
    target = SqliteStore(root, namespace="project:b")
    with pytest.raises(ValueError, match="namespace"):
        migrate_corpus(source=source, target=target)


# -------------------------------------------------------------------- rollback
def test_store_environment_variable_selects_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TRACE_AGENT_MEMORY_STORE`` is the rollback switch; it has to actually work."""
    monkeypatch.setenv(MEMORY_STORE_ENV, "json")
    store = build_memory_store(tmp_path / "memory")
    assert isinstance(store, JsonStore)
    assert store.store_kind == "json"
    assert not store_supports_pushdown(store)

    monkeypatch.setenv(MEMORY_STORE_ENV, "sqlite")
    indexed = build_memory_store(tmp_path / "memory")
    assert isinstance(indexed, SqliteStore)
    assert store_supports_pushdown(indexed)


def test_unusable_sqlite_degrades_to_files_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host without FTS5 must not end up with a silently empty corpus.

    The probe in ``build_memory_store`` exists for this. Without it the store
    constructs fine and every write is swallowed by a best-effort handler, so
    recall returns nothing forever and nothing logs why.
    """
    monkeypatch.setenv(MEMORY_STORE_ENV, "sqlite")

    def _explode(self: object) -> int:
        raise RuntimeError("no such module: fts5")

    monkeypatch.setattr(SqliteStore, "version", property(_explode))
    store = build_memory_store(tmp_path / "memory")
    assert isinstance(store, JsonStore)
    assert store.degraded_reason
    assert "fts5" in store.degraded_reason
    assert store_diagnostics(store)["degraded_reason"] == store.degraded_reason


def test_json_rollback_still_recalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rolling back must leave memory working, just slower."""
    monkeypatch.setenv(MEMORY_STORE_ENV, "json")
    runtime = MemoryRuntime(tmp_path / "memory")
    _seed(runtime.store)
    result = runtime.recall("冷启动 白屏", scenario_type="cold_start", allow_deep=False)
    curated = [
        hit.memory.memory_id
        for hit in result.hits
        if isinstance(hit.memory, CuratedMemory)
    ]
    assert curated[:1] == ["mem-cold"]


# --------------------------------------------------------------- maintenance
def test_reindex_rebuilds_the_full_text_index(tmp_path: Path) -> None:
    """The recovery path after the tokenizer or the indexed field set changes."""
    store = SqliteStore(tmp_path / "memory")
    _seed(store)
    assert store.search(MemoryQuery(text="内存泄漏", limit=50))

    indexed = store.reindex()
    assert indexed == len(CORPUS) + 2  # memories plus episodes

    hits = store.search(MemoryQuery(text="内存泄漏", limit=50))
    ids = {getattr(item.memory, "memory_id", "") for item in hits}
    assert "mem-leak" in ids


def test_reindex_is_a_noop_on_the_file_adapter(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "memory")
    _seed(store)
    assert store.reindex() == 0


def test_governance_counters_agree_across_adapters(tmp_path: Path) -> None:
    """``distinct_runs`` is what the quality gate reads, on either adapter.

    Adding the same run twice must not count twice: that inflation is the
    section-1.2 defect-A mechanism, and it would be easy to reintroduce on one
    adapter while the other stays correct.
    """
    sql = SqliteStore(tmp_path / "sql")
    files = JsonStore(tmp_path / "json")
    for store in (sql, files):
        _seed(store)
        assert store.add_source("mem-cold", run_id="run-new", episode_id="ep-9") is True
        assert store.add_source("mem-cold", run_id="run-new", episode_id="ep-9") is False
        assert store.distinct_runs("mem-cold") == 2


def test_ownership_columns_survive_a_roundtrip(tmp_path: Path) -> None:
    """``session_id`` / ``project_id`` / ``agent_id`` are columns, not decoration.

    Section 4 adds them so an episode can be traced back to the session that
    produced it and a session to the project it belongs to. A column present in
    the DDL but never read back is indistinguishable from one that was never
    written, and the failure is silent: recall keeps working, and the join that
    was supposed to answer "which run taught us this" returns nothing.
    """
    episode = _episode(
        "ep-owned", "run-owned", "冷启动白屏", "白屏",
        session_id="sess-owned", project_id="proj-owned",
    )
    session = _session(
        "sess-owned", agent_id="agent-7", user_id="user-9", project_id="proj-owned"
    )
    for store in (SqliteStore(tmp_path / "sql"), JsonStore(tmp_path / "json")):
        store.write_episode(episode)
        store.import_session(session)

        stored_episode = store.load_episode("ep-owned")
        assert stored_episode is not None
        assert stored_episode.session_id == "sess-owned"
        assert stored_episode.project_id == "proj-owned"
        # The listing path reads different columns than the by-id path on the SQL
        # adapter, so both have to be checked.
        listed = {item.episode_id: item for item in store.list_episodes()}
        assert listed["ep-owned"].session_id == "sess-owned"

        stored_session = store.load_session("sess-owned")
        assert stored_session is not None
        assert stored_session.agent_id == "agent-7"
        assert stored_session.user_id == "user-9"
        assert stored_session.project_id == "proj-owned"


def test_governance_metadata_is_read_back_from_the_index(tmp_path: Path) -> None:
    """``version`` and ``last_accessed_at`` are store-owned, so the store must return them.

    Both are ``Field(exclude=True)`` on :class:`CuratedMemory`: keeping them out
    of the payload is what lets the file adapter write byte-identical JSON to the
    one it wrote before P1. The price is that reading them back is entirely the
    SQL adapter's job, and nothing else would notice if it stopped.

    The file adapter is asserted to *not* have them, which is also a real claim:
    it has no ``last_accessed_at`` column to stamp, so ``touch`` is a no-op there
    rather than a silent lie about when a memory was used.
    """
    sql = SqliteStore(tmp_path / "sql")
    _seed(sql)
    assert sql.set_trust(
        "mem-cold", MemoryStatus.VERIFIED, reason="expert-signed", actor="alice"
    )

    reloaded = sql.load_curated("mem-cold")
    assert reloaded is not None
    assert reloaded.status is MemoryStatus.VERIFIED
    assert reloaded.version == 2, "the seed wrote v1 and set_trust wrote v2"
    assert reloaded.last_accessed_at is None, "a write is not an access"

    sql.touch("mem-cold")
    touched = sql.load_curated("mem-cold")
    assert touched is not None and touched.last_accessed_at is not None
    assert touched.version == reloaded.version, "touch must not look like corroboration"
    assert touched.updated_at == reloaded.updated_at, "touch must not refresh freshness"
    assert touched.status_reason == reloaded.status_reason

    listed = {item.memory_id: item for item in sql.list_curated()}
    assert listed["mem-cold"].version == 2
    assert listed["mem-cold"].last_accessed_at is not None

    files = JsonStore(tmp_path / "json")
    _seed(files)
    files.set_trust("mem-cold", MemoryStatus.VERIFIED, reason="expert-signed")
    files.touch("mem-cold")
    from_files = files.load_curated("mem-cold")
    assert from_files is not None and from_files.status is MemoryStatus.VERIFIED
    assert from_files.version is None
    assert from_files.last_accessed_at is None


def test_audit_trail_travels_with_the_bundle(tmp_path: Path) -> None:
    """A migration that copies memories but drops their history is lossy.

    Section 7's rule is that every trust transition is recorded with a reason and
    an actor. If ``export_bundle`` did not carry ``memory_versions`` and
    ``memory_links``, the target would be unable to answer "who vouched for this,
    and when" - and would look like a corpus that had never been governed at all.
    """
    source = SqliteStore(tmp_path / "source")
    _seed(source)
    source.set_trust(
        "mem-cold", MemoryStatus.VERIFIED, reason="expert-signed", actor="alice"
    )
    source.link(
        "mem-cold", "mem-guess", MemoryRelation.CONTRADICTS, evidence='{"run":"r-1"}'
    )

    bundle = export_bundle(source)
    assert bundle["version"] == BUNDLE_VERSION
    assert bundle["versions"], "the audit trail was not exported"
    assert bundle["links"], "the edges were not exported"
    # ``recent_versions`` is newest first; the bundle must be chronological so the
    # target's ordinals come out in the order the transitions happened.
    reasons = [row["reason"] for row in bundle["versions"] if row["memory_id"] == "mem-cold"]
    assert reasons[-1] == "expert-signed"

    target = JsonStore(tmp_path / "target")
    report = import_bundle(target, bundle)
    assert not report.errors
    assert report.versions == len(bundle["versions"])
    assert report.links == len(bundle["links"])

    carried = target.load_curated("mem-cold")
    assert carried is not None and carried.status is MemoryStatus.VERIFIED
    assert target.list_links(relation=MemoryRelation.CONTRADICTS.value)
    assert [row["reason"] for row in target.recent_versions(limit=100)]


def test_replaying_a_bundle_does_not_grow_the_audit_trail(tmp_path: Path) -> None:
    """Re-running a migration must add nothing, in *both* directions.

    The SQL -> file direction was already idempotent because the sidecar dedups on
    the row's own identity. The file -> SQL direction was not: sidecar rows carry
    no ordinal, and deriving one as ``MAX(version) + 1`` produces a *different*
    ordinal on every replay, so ``UNIQUE(namespace, memory_id, version)`` never
    fired and each re-run appended a duplicate of the entire history. A migration
    is exactly the operation an operator re-runs after an interruption.
    """
    files = JsonStore(tmp_path / "json")
    files.write_curated(_memory(*CORPUS[0]))
    files.set_trust("mem-cold", MemoryStatus.VERIFIED, reason="expert-signed")
    files.link("mem-cold", "mem-guess", MemoryRelation.CONTRADICTS)
    assert all(row.get("version") is None for row in files.recent_versions(limit=50))

    bundle = export_bundle(files)
    sql = SqliteStore(tmp_path / "sql")
    first = import_bundle(sql, bundle)
    assert first.versions == len(bundle["versions"]) > 0

    rows_after_first = len(sql.recent_versions(limit=1000))
    for _ in range(2):
        replay = import_bundle(sql, bundle)
        assert replay.versions == 0, "an ordinal-less row was renumbered and re-added"
        assert replay.links == 0
    assert len(sql.recent_versions(limit=1000)) == rows_after_first

    # The other direction, for the same property.
    restored = JsonStore(tmp_path / "restored")
    sql_bundle = export_bundle(sql)
    assert import_bundle(restored, sql_bundle).versions > 0
    after = len(restored.recent_versions(limit=1000))
    assert import_bundle(restored, sql_bundle).versions == 0
    assert len(restored.recent_versions(limit=1000)) == after


def test_reimporting_an_unchanged_memory_records_no_version(tmp_path: Path) -> None:
    """A copy is not an edit.

    ``import_bundle`` loads the audit trail *before* the records precisely so
    ``_put_curated`` can recognise a re-import: the history it just loaded already
    ends at the state being written. Without that check every migration restamps
    the corpus it was supposed to carry over untouched, and section 14's
    "trust 跃迁可审计率 100%" turns into an audit trail full of copy artefacts.
    """
    for store in (SqliteStore(tmp_path / "sql"), JsonStore(tmp_path / "json")):
        _seed(store)
        store.set_trust("mem-cold", MemoryStatus.VERIFIED, reason="expert-signed")
        baseline = len(store.recent_versions(limit=1000))

        for _ in range(2):
            store.import_curated(store.load_curated("mem-cold"))
        assert len(store.recent_versions(limit=1000)) == baseline

        # A genuine change still records, so the check is not just "never write".
        store.set_trust("mem-cold", MemoryStatus.ARCHIVED, reason="contradicted")
        assert len(store.recent_versions(limit=1000)) == baseline + 1


def test_version_resolver_reuses_the_ordinal_it_already_handed_out() -> None:
    """The rule that makes an ordinal-less bundle import idempotent, stated alone.

    Both SQL adapters derive a missing ``version`` from this class rather than
    from ``MAX(version) + 1``, and the derivation has to be a pure function of the
    row's identity for ``ON CONFLICT DO NOTHING`` to have anything to conflict
    with. Testing it here keeps the property visible when an adapter is rewritten.
    """
    existing = [
        ("m1", 1, "active", "gate", "2026-03-01T12:00:00+00:00"),
        ("m1", 2, "verified", "expert", "2026-03-02T12:00:00+00:00"),
        ("m2", 5, "active", "gate", "2026-03-01T12:00:00+00:00"),
    ]
    resolver = VersionResolver(existing)

    # A row already in the table comes back on the ordinal it has, so the insert
    # conflicts instead of renumbering.
    assert resolver.ordinal(
        memory_id="m1", trust="active", reason="gate",
        created_at="2026-03-01T12:00:00+00:00",
    ) == 1
    assert resolver.ordinal(
        memory_id="m1", trust="verified", reason="expert",
        created_at="2026-03-02T12:00:00+00:00",
    ) == 2

    # A genuinely new transition continues that memory's own sequence, not the
    # table's - ordinals are per memory.
    assert resolver.ordinal(
        memory_id="m1", trust="archived", reason="contradicted",
        created_at="2026-03-03T12:00:00+00:00",
    ) == 3
    assert resolver.ordinal(
        memory_id="m2", trust="verified", reason="expert",
        created_at="2026-03-04T12:00:00+00:00",
    ) == 6

    # Asking twice inside one import answers twice the same way: two identical rows
    # in a single bundle collapse to one, matching the file adapter's sidecar dedup.
    row = {
        "memory_id": "m3", "trust": "active", "reason": "gate",
        "created_at": "2026-03-05T12:00:00+00:00",
    }
    first = resolver.ordinal(**row)
    assert resolver.ordinal(**row) == first

    # An explicit ordinal always wins, so a SQL -> SQL copy lands on the row it
    # came from.
    assert resolver.ordinal(memory_id="m1", version=42, trust="active") == 42

    # Driver-tolerant: ``Decimal``/``str`` ordinals and NULL columns are ordinary.
    loose = VersionResolver([("m9", None, None, None, None)])
    assert loose.ordinal(memory_id="m9", trust="active", reason="gate") == 1
    assert VersionResolver([("m9", "3", "active", "gate", "t")]).ordinal(
        memory_id="m9", trust="active", reason="gate", created_at="t"
    ) == 3


def test_a_write_after_a_sidecar_import_clears_the_trails_ordinal(
    tmp_path: Path,
) -> None:
    """The audit trail can run *ahead* of the record, and a write must notice.

    ``import_governance`` derives an ordinal for an ordinal-less sidecar row from
    the trail's own maximum, and deliberately leaves the ``memories`` row alone:
    a copied transition is history, not a new state for the record. So after a
    migration replays a sidecar, ``MAX(memory_versions.version)`` can exceed
    ``memories.version``.

    Deriving the next ordinal from the row alone on that path collides with
    ``UNIQUE(namespace, memory_id, version)``, which raises inside a
    consolidation and costs the run its memory write - the exact failure the
    insert branch of ``_put_curated`` was already written to avoid. Asserted on
    SQLite here because the logic is shared; the PostgreSQL half is the live test
    in ``tests/test_memory_postgres.py``.
    """
    store = SqliteStore(tmp_path / "sql")
    store.write_curated(_memory(*CORPUS[0]))
    store.set_trust("mem-cold", MemoryStatus.VERIFIED, reason="expert-signed")
    assert store.load_curated("mem-cold").version == 2

    # An ordinal-less row, the shape ``JsonStore``'s sidecar produces. It lands on
    # 3 while the record stays on 2, so the trail is now the higher of the two.
    report = import_bundle(
        store,
        {
            "version": BUNDLE_VERSION,
            "store": "json",
            "namespace": store.namespace,
            "episodes": [],
            "memories": [],
            "sessions": [],
            "links": [],
            "versions": [
                {
                    "memory_id": "mem-cold",
                    "topic": CORPUS[0][1],
                    "content": CORPUS[0][2],
                    "from_trust": "candidate",
                    "trust": "active",
                    "confidence": 0.5,
                    "reason": "sidecar-from-elsewhere",
                    "actor": None,
                    "created_at": "2026-03-02T12:00:00+00:00",
                }
            ],
        },
    )
    assert report.versions == 1, report.as_dict()
    assert store.load_curated("mem-cold").version == 2, "the import restamped the record"

    # The next genuine transition has to clear ordinal 3, not reuse it.
    store.set_trust("mem-cold", MemoryStatus.ARCHIVED, reason="contradicted")
    assert {row["version"] for row in store.recent_versions(limit=100)} == {1, 2, 3, 4}
    assert store.load_curated("mem-cold").version == 4


# ---------------------------------------------------------------------- scale
@pytest.fixture(scope="module")
def large_store(tmp_path_factory: pytest.TempPathFactory) -> SqliteStore:
    """A 10k-memory corpus, built once for the whole module.

    Seeding through ``import_bundle`` uses one transaction; per-record commits
    cost roughly a millisecond each and would dominate the test rather than the
    thing it measures.
    """
    root = tmp_path_factory.mktemp("memory-scale") / "memory"
    store = SqliteStore(root)
    symptoms = (
        "冷启动白屏", "启动耗时过长", "主线程卡顿", "内存泄漏", "列表滑动掉帧",
        "页面加载缓慢", "网络请求超时", "首帧渲染延迟", "IO 阻塞", "锁竞争",
    )
    causes = (
        "main thread blocked by sync IO",
        "excessive layout inflation",
        "unbounded bitmap cache",
        "network retry storm",
        "lock contention in binder",
    )
    bundle = {"episodes": [], "sessions": [], "memories": []}
    for index in range(10_000):
        symptom = symptoms[index % len(symptoms)]
        cause = causes[index % len(causes)]
        bundle["memories"].append(
            _memory(
                f"mem-{index:06d}",
                f"{symptom} #{index}",
                f"{symptom}: {cause} (case {index})",
                [symptom, cause.split()[0]],
                MemoryStatus.ACTIVE if index % 3 else MemoryStatus.CANDIDATE,
                round(0.4 + (index % 60) / 100, 2),
                scenario_type="cold_start" if index % 2 else "jank",
            ).model_dump(mode="json")
        )
    report = import_bundle(store, bundle)
    assert not report.errors
    assert store.stats().memories == 10_000
    yield store
    store.close()


def test_recall_latency_at_ten_thousand_memories(large_store: SqliteStore) -> None:
    """P1 acceptance: retrieval is indexed, not a scan.

    The pre-P1 path deserialised every JSON file in the corpus on every recall,
    so latency grew linearly with memory size. The threshold is the design doc's
    150 ms with a wide margin - the measured p95 is an order of magnitude below
    it - because a CI machine is not a developer laptop and a flaky perf test is
    worse than a loose one.
    """
    random.seed(7)
    symptoms = ("冷启动白屏", "主线程卡顿", "内存泄漏", "列表滑动掉帧", "网络请求超时")
    causes = ("sync IO", "layout inflation", "bitmap cache", "retry storm", "binder")
    queries = [
        f"{random.choice(symptoms)} {random.choice(causes)}" for _ in range(120)
    ]

    for query in queries[:10]:  # warm the page cache and the query planner
        large_store.search(MemoryQuery(text=query, limit=200))

    latencies: list[float] = []
    for query in queries:
        started = time.perf_counter()
        large_store.search(MemoryQuery(text=query, limit=200))
        latencies.append((time.perf_counter() - started) * 1000)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 150.0, f"p95 recall latency {p95:.1f} ms at 10k memories"
