"""P4 acceptance for the two operator commands: ``memory namespaces`` and ``memory backup``.

Both exist because the guarantees elsewhere in the design are only enforceable if
they are inspectable. Section 8's namespace isolation is a claim about what an
operator cannot see across, so they have to be able to see which domains exist
and how big each got; section 13's rollback plan is a claim about recoverability,
so a backup has to state what it captured and how to put it back.

The property these tests hold onto is that a backup is *verifiable*, not merely
produced: the SQLite snapshot is opened again as a live corpus and its records
read back, the manifest is parsed and checked against the store's own ``stats``,
and the artifact names are checked to survive two backups inside one second -
``VACUUM INTO`` refuses to overwrite, so a collision there is a failed backup.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trace_agent.memory import cli
from trace_agent.memory.backup import backup_corpus
from trace_agent.memory.cli import memory_app
from trace_agent.memory.flags import (
    MEMORY_DSN_ENV,
    MEMORY_NAMESPACE_ENV,
    MEMORY_STORE_ENV,
)
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    MemoryStatus,
    Provenance,
    SessionMemory,
)
from trace_agent.memory.store import JsonStore, SqliteStore, build_memory_store

PRIMARY = "project:demo"
SECONDARY = "eval:run-9:case-1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command takes ``--store``/``--namespace`` explicitly; the env must agree."""
    for name in (MEMORY_STORE_ENV, MEMORY_NAMESPACE_ENV, MEMORY_DSN_ENV):
        monkeypatch.delenv(name, raising=False)


def _memory(memory_id: str, *, topic: str = "冷启动白屏") -> CuratedMemory:
    return CuratedMemory(
        memory_id=memory_id,
        kind=MemoryKind.PATTERN,
        scenario_type="cold_start",
        topic=topic,
        content="main thread blocked by sync IO during cold start",
        keywords=["冷启动", "白屏", "主线程"],
        status=MemoryStatus.ACTIVE,
        confidence=0.8,
        provenance=Provenance(source_type="finding", references=[f"run:{memory_id}"]),
    )


def _seed(store: Any) -> None:
    store.write_curated(_memory("mem-1"))
    store.write_episode(
        EpisodicMemory(
            episode_id="ep-1",
            run_id="run-1",
            trace_id="trace-1",
            scenario_type="cold_start",
            scenario="冷启动白屏",
            symptom="启动后长时间白屏",
            summary="main thread blocked by sync IO",
            root_causes=["main thread blocked by sync IO"],
        )
    )
    store.write_session(SessionMemory(session_id="sess-1", title="cold start", topic="冷启动白屏"))


def _corpus(tmp_path: Path, kind: str) -> Path:
    """A root holding two populated isolation domains."""
    root = tmp_path / "memory"
    factory = SqliteStore if kind == "sqlite" else JsonStore
    for namespace in (PRIMARY, SECONDARY):
        store = factory(root, namespace=namespace)
        _seed(store)
        store.close()
    return root


def _invoke(args: list[str]) -> Any:
    result = CliRunner().invoke(memory_app, args)
    assert result.exit_code == 0, result.output
    return result


def _run(args: list[str]) -> Any:
    """Invoke without asserting success, for the paths that must refuse."""
    return CliRunner().invoke(memory_app, args)


# ------------------------------------------------------------------ namespaces
@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_namespaces_lists_every_domain_with_its_counts(
    tmp_path: Path, kind: str
) -> None:
    root = _corpus(tmp_path, kind)
    result = _invoke(
        ["namespaces", "--root", str(root), "--store", kind, "--namespace", PRIMARY]
    )

    assert PRIMARY in result.output
    assert SECONDARY in result.output
    # The current domain is the one an operator is about to migrate or wipe, so
    # it has to be the one marked.
    assert any(
        line.startswith("*") and PRIMARY in line for line in result.output.splitlines()
    )
    assert not any(
        line.startswith("*") and SECONDARY in line
        for line in result.output.splitlines()
    )
    assert "2 个隔离域 / 2 条记忆" in result.output


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_namespaces_json_output_is_machine_readable(
    tmp_path: Path, kind: str
) -> None:
    root = _corpus(tmp_path, kind)
    result = _invoke(
        [
            "namespaces",
            "--root",
            str(root),
            "--store",
            kind,
            "--namespace",
            PRIMARY,
            "--json",
        ]
    )
    payload = json.loads(result.output)

    assert payload["store"] == kind
    assert payload["current"] == PRIMARY
    by_name = {row["namespace"]: row for row in payload["namespaces"]}
    assert set(by_name) == {PRIMARY, SECONDARY}
    for row in payload["namespaces"]:
        assert (row["memories"], row["episodes"], row["sessions"]) == (1, 1, 1)
        assert row["last_updated"]
    assert by_name[PRIMARY]["current"] is True
    assert by_name[SECONDARY]["current"] is False


def test_namespaces_on_an_empty_corpus_says_so(tmp_path: Path) -> None:
    """An empty corpus is a normal state, not an error an operator has to decode."""
    root = tmp_path / "memory"
    result = _invoke(["namespaces", "--root", str(root), "--store", "sqlite"])
    assert "没有可列出的隔离域" in result.output


def test_namespaces_does_not_leak_across_domains(tmp_path: Path) -> None:
    """Listing is metadata; it must not print another domain's records."""
    root = _corpus(tmp_path, "sqlite")
    result = _invoke(
        ["namespaces", "--root", str(root), "--store", "sqlite", "--namespace", PRIMARY]
    )
    assert "main thread blocked" not in result.output
    assert "mem-1" not in result.output


# ---------------------------------------------------------------------- backup
@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_backup_reports_what_it_captured(tmp_path: Path, kind: str) -> None:
    root = _corpus(tmp_path, kind)
    out = tmp_path / "backup"
    result = _invoke(
        [
            "backup",
            "--root",
            str(root),
            "--store",
            kind,
            "--namespace",
            PRIMARY,
            "--out",
            str(out),
        ]
    )

    assert "备份完成" in result.output
    assert "恢复方式" in result.output
    manifests = list(out.glob("manifest-*.json"))
    assert len(manifests) == 1
    report = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert report["store_kind"] == kind
    assert report["namespace"] == PRIMARY
    assert report["stats"]["memories"] == 1
    assert report["stats"]["episodes"] == 1
    for artifact in report["artifacts"]:
        assert Path(artifact["path"]).is_file() or Path(artifact["path"]).is_dir()
        assert artifact["sha256"]
        assert artifact["records"] >= 3


def test_backup_sqlite_snapshot_restores_into_a_live_corpus(tmp_path: Path) -> None:
    """The claim a backup makes is recoverability, so recover from it."""
    root = _corpus(tmp_path, "sqlite")
    out = tmp_path / "backup"
    report = backup_corpus(build_memory_store(root, namespace=PRIMARY), out)
    snapshot = next(
        item.path for item in report.artifacts if item.kind == "sqlite-snapshot"
    )

    restored_root = tmp_path / "restored"
    restored_root.mkdir()
    shutil.copyfile(snapshot, restored_root / "memory.sqlite3")
    restored = SqliteStore(restored_root, namespace=PRIMARY)
    try:
        assert restored.load_curated("mem-1") is not None
        assert restored.load_episode("ep-1") is not None
        assert restored.load_session("sess-1") is not None
        # The other namespace came along, because one SQLite file holds every
        # domain - which is exactly what the CLI note tells the operator.
        assert any(
            row["namespace"] == SECONDARY for row in restored.list_namespaces()
        )
        assert restored.corpus_epoch() > 0
    finally:
        restored.close()


def test_backup_json_tree_holds_the_namespace_records(tmp_path: Path) -> None:
    root = _corpus(tmp_path, "json")
    out = tmp_path / "backup"
    store = build_memory_store(root, namespace=PRIMARY, kind="json")
    try:
        report = backup_corpus(store, out)
    finally:
        store.close()

    tree = next(item.path for item in report.artifacts if item.kind == "file-tree")
    assert tree.is_dir()
    # Only the requested domain was copied: an eval corpus must not ride along
    # inside a production backup.
    assert (tree / "curated" / "mem-1.json").is_file()
    assert not (tree / "ns").exists()
    names = {path.name for path in tree.rglob("*.json")}
    assert "ep-1.json" in names and "sess-1.json" in names
    # The copy *is* the namespace directory (``JsonStore`` resolves
    # ``<root>/ns/<namespace>`` itself), so restoring points a store's root at it.
    restored = JsonStore(tree)
    try:
        assert restored.load_curated("mem-1") is not None
    finally:
        restored.close()


def test_backup_notes_that_a_sqlite_snapshot_is_whole_database(tmp_path: Path) -> None:
    """``records`` counts every namespace; ``stats`` counts one. Say so."""
    root = _corpus(tmp_path, "sqlite")
    result = _invoke(
        [
            "backup",
            "--root",
            str(root),
            "--store",
            "sqlite",
            "--namespace",
            PRIMARY,
            "--out",
            str(tmp_path / "backup"),
        ]
    )
    assert "整库副本" in result.output
    assert "其他命名空间" in result.output


def test_backup_bundle_flag_adds_a_portable_snapshot(tmp_path: Path) -> None:
    root = _corpus(tmp_path, "sqlite")
    out = tmp_path / "backup"
    result = _invoke(
        [
            "backup",
            "--root",
            str(root),
            "--store",
            "sqlite",
            "--namespace",
            PRIMARY,
            "--out",
            str(out),
            "--bundle",
            "--json",
        ]
    )
    report = json.loads(result.output)
    kinds = [item["kind"] for item in report["artifacts"]]
    assert kinds == ["sqlite-snapshot", "bundle"]
    assert report["manifest"] and Path(report["manifest"]).is_file()

    bundle = next(
        item["path"] for item in report["artifacts"] if item["kind"] == "bundle"
    )
    payload = json.loads(Path(bundle).read_text(encoding="utf-8"))
    assert len(payload["memories"]) == 1
    assert payload["namespace"] == PRIMARY
    assert "mem-1" in {item["memory_id"] for item in payload["memories"]}


def test_two_backups_inside_one_second_do_not_collide(tmp_path: Path) -> None:
    """``VACUUM INTO`` refuses to overwrite, so a repeated backup must rename."""
    root = _corpus(tmp_path, "sqlite")
    out = tmp_path / "backup"
    store = build_memory_store(root, namespace=PRIMARY)
    try:
        first = backup_corpus(store, out)
        second = backup_corpus(store, out)
    finally:
        store.close()

    paths = {item.path for item in first.artifacts} | {
        item.path for item in second.artifacts
    }
    assert len(paths) == 2
    assert all(path.is_file() for path in paths)
    assert first.manifest != second.manifest


def test_backup_json_output_matches_the_human_one(tmp_path: Path) -> None:
    root = _corpus(tmp_path, "sqlite")
    out = tmp_path / "backup"
    result = _invoke(
        [
            "backup",
            "--root",
            str(root),
            "--store",
            "sqlite",
            "--namespace",
            PRIMARY,
            "--out",
            str(out),
            "--json",
        ]
    )
    report = json.loads(result.output)
    assert report["store_kind"] == "sqlite"
    assert report["destination"] == str(out)
    assert report["artifacts"] and report["notes"]


def test_backup_creates_a_missing_destination(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "nested" / "backup"
    root = _corpus(tmp_path, "json")
    result = _invoke(
        [
            "backup",
            "--root",
            str(root),
            "--store",
            "json",
            "--namespace",
            PRIMARY,
            "--out",
            str(out),
        ]
    )
    assert out.is_dir()
    assert "备份完成" in result.output


# --------------------------------------------------------------------- migrate
def test_migrate_into_a_used_corpus_reports_success(tmp_path: Path) -> None:
    """The operator-facing half of the production mis-verdict.

    ``memory migrate`` told an operator not to switch store after a migration
    that had worked, purely because the target already held rows. The read-back
    fix lives in ``migrate._verify``; this pins the CLI, which is where the wrong
    verdict was actually read and where it would have been obeyed.
    """
    root = tmp_path / "memory"
    source = JsonStore(root, namespace=PRIMARY)
    _seed(source)
    source.close()

    # Written through SQLite by the running agent, so absent from the JSON source.
    live = SqliteStore(root, namespace=PRIMARY)
    live.write_curated(_memory("mem-live", topic="列表滑动掉帧"))
    live.close()

    result = _invoke(
        [
            "migrate",
            "--from",
            "json",
            "--to",
            "sqlite",
            "--root",
            str(root),
            "--namespace",
            PRIMARY,
        ]
    )
    assert "全部命中" in result.output
    assert "校验未通过" not in result.output
    assert "请勿切换" not in result.output
    # sqlite is the default, so the honest advice is that nothing needs changing.
    assert "无需切换" in result.output
    assert SqliteStore(root, namespace=PRIMARY).load_curated("mem-1") is not None


def test_migrate_to_a_non_default_store_still_says_how_to_switch(tmp_path: Path) -> None:
    """The switch instruction is only suppressed when it would be a no-op."""
    root = _corpus(tmp_path, "sqlite")
    result = _invoke(
        [
            "migrate",
            "--from",
            "sqlite",
            "--to",
            "json",
            "--root",
            str(root),
            "--namespace",
            PRIMARY,
        ]
    )
    assert "TRACE_AGENT_MEMORY_STORE=json" in result.output
    assert "无需切换" not in result.output
    assert JsonStore(root, namespace=PRIMARY).load_curated("mem-1") is not None


# ---------------------------------------------------------------------- verify


def _verify_args(root: Path, kind: str, *extra: str) -> list[str]:
    return [
        "verify",
        "mem-1",
        "--root",
        str(root),
        "--store",
        kind,
        "--namespace",
        PRIMARY,
        *extra,
    ]


def _reload(root: Path, kind: str, memory_id: str = "mem-1") -> CuratedMemory:
    factory = SqliteStore if kind == "sqlite" else JsonStore
    store = factory(root, namespace=PRIMARY)
    try:
        loaded = store.load_curated(memory_id)
    finally:
        store.close()
    assert loaded is not None
    return loaded


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_verify_signs_a_memory_and_names_the_reviewer(
    tmp_path: Path, kind: str
) -> None:
    """Section 5.2's expert route has to be reachable by an operator, not just by code.

    Section 13 lists it as the mitigation for a gate that is too strict, and a
    mitigation nobody can invoke is not one: a corpus of ``suspected`` findings
    would otherwise have no path to ``verified`` except lowering the thresholds
    for every memory at once.
    """
    root = _corpus(tmp_path, kind)

    result = _invoke(
        _verify_args(
            root, kind, "--by", "alice@example.com", "--note", "设备农场复核通过"
        )
    )

    assert "信任跃迁 : active -> verified" in result.output
    assert "expert:alice@example.com" in result.output
    # The justification is echoed because it lands in a version row the operator
    # cannot see from the terminal.
    assert "设备农场复核通过" in result.output
    signed = _reload(root, kind)
    assert signed.status is MemoryStatus.VERIFIED
    assert signed.provenance.validated_by == "alice@example.com"


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_verify_json_output_is_machine_readable(tmp_path: Path, kind: str) -> None:
    root = _corpus(tmp_path, kind)

    result = _invoke(_verify_args(root, kind, "--by", "alice", "--json"))

    payload = json.loads(result.output)
    assert payload["accepted"] is True
    assert payload["validated_by"] == "alice"
    assert payload["status"] == "verified"
    assert payload["transition"]["from"] == "active"
    assert payload["transition"]["to"] == "verified"
    assert payload["transition"]["actor"] == "expert:alice"


@pytest.mark.parametrize("identity", ["", "  ", "pending"])
def test_verify_refuses_a_placeholder_identity(
    tmp_path: Path, identity: str
) -> None:
    """``pending`` is the sentinel older releases wrote, not a reviewer."""
    root = _corpus(tmp_path, "sqlite")

    result = _run(_verify_args(root, "sqlite", "--by", identity))

    assert result.exit_code == 1
    assert "未生效" in result.output
    untouched = _reload(root, "sqlite")
    assert untouched.status is MemoryStatus.ACTIVE
    assert not untouched.provenance.validated_by


def test_verify_revoke_hands_trust_back_to_the_evidence(tmp_path: Path) -> None:
    """A signature that cannot be withdrawn is a one-way ratchet.

    ``mem-1`` carries one run, so once the name is gone the gate has nothing left
    to stand on and the record returns to ``candidate`` - out of the trusted set,
    which is the whole point of the withdrawal.
    """
    root = _corpus(tmp_path, "sqlite")
    _invoke(_verify_args(root, "sqlite", "--by", "alice"))

    result = _invoke(_verify_args(root, "sqlite", "--by", "alice", "--revoke"))

    assert "信任跃迁 : verified -> candidate" in result.output
    revoked = _reload(root, "sqlite")
    assert revoked.provenance.validated_by is None
    assert revoked.status is MemoryStatus.CANDIDATE
    assert "sign-off withdrawn" in (revoked.status_reason or "")


def test_verify_refuses_to_revoke_a_signature_that_was_never_given(
    tmp_path: Path,
) -> None:
    root = _corpus(tmp_path, "sqlite")

    result = _run(_verify_args(root, "sqlite", "--by", "alice", "--revoke"))

    assert result.exit_code == 1
    assert _reload(root, "sqlite").status is MemoryStatus.ACTIVE


def test_verify_does_not_cross_namespaces(tmp_path: Path) -> None:
    """Section 8.1, applied to a write: one domain's operator cannot touch another's."""
    root = _corpus(tmp_path, "sqlite")

    _invoke(_verify_args(root, "sqlite", "--by", "alice"))

    other = SqliteStore(root, namespace=SECONDARY)
    try:
        isolated = other.load_curated("mem-1")
    finally:
        other.close()
    assert isolated is not None
    assert isolated.status is MemoryStatus.ACTIVE
    assert not isolated.provenance.validated_by


def test_verify_reports_a_missing_memory(tmp_path: Path) -> None:
    root = _corpus(tmp_path, "sqlite")

    result = _run(
        [
            "verify",
            "mem-does-not-exist",
            "--by",
            "alice",
            "--root",
            str(root),
            "--store",
            "sqlite",
            "--namespace",
            PRIMARY,
        ]
    )

    assert result.exit_code == 1
    assert "未生效" in result.output


# --------------------------------------------------------------- vector-index
class _VectorStore:
    """A stand-in for ``PostgresStore``: the only adapter with an ANN index.

    ``vector_index`` is a Postgres-only method, so the two interesting CLI paths
    (a report to echo, and a report that must fail the command) cannot be reached
    through the sqlite/json corpora the rest of this file uses.
    """

    store_kind = "postgres"

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.dims: list[int] = []
        self.closed = False

    def vector_index(self, dim: int) -> dict[str, Any]:
        self.dims.append(int(dim))
        return dict(self.report)

    def close(self) -> None:
        self.closed = True


_BUILT = {
    "pgvector": True,
    "dim": 256,
    "narrowed": ["memories", "episodes"],
    "indexes": ["memories", "episodes"],
    "errors": [],
}


def _patch_store(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(cli, "build_memory_store", lambda *a, **k: fake)


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_vector_index_explains_an_adapter_that_has_no_ann_index(
    tmp_path: Path, kind: str
) -> None:
    """Not a failure: an exact Python cosine needs no index, and exit 0 keeps the
    command usable in provisioning that runs against whichever adapter was picked."""
    root = _corpus(tmp_path, kind)
    result = _invoke(
        ["vector-index", "--root", str(root), "--store", kind, "--namespace", PRIMARY]
    )

    assert "说明" in result.output
    assert "不需要 ANN 索引" in result.output
    # ``pgvector : 否`` on an adapter with no vector column at all would read as
    # "the extension is missing" rather than "not applicable here".
    assert "pgvector" not in result.output


def test_vector_index_json_reports_an_unsupported_adapter(tmp_path: Path) -> None:
    root = _corpus(tmp_path, "sqlite")
    result = _invoke(
        [
            "vector-index",
            "--root",
            str(root),
            "--store",
            "sqlite",
            "--namespace",
            PRIMARY,
            "--json",
        ]
    )
    payload = json.loads(result.output)

    assert payload["supported"] is False
    assert payload["dim"] > 0


def test_vector_index_defaults_to_the_configured_recipe_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The width must be the one the recipe that fills the column produces.

    A default taken from anywhere else narrows the column to a width the next
    ``memory embed`` cannot write into.
    """
    monkeypatch.setenv("TRACE_AGENT_MEMORY_EMBEDDING", "local")
    monkeypatch.setenv("TRACE_AGENT_MEMORY_EMBEDDING_DIM", "320")
    fake = _VectorStore(_BUILT)
    _patch_store(monkeypatch, fake)

    _invoke(["vector-index", "--root", str(tmp_path), "--store", "postgres"])

    assert fake.dims == [320]


def test_vector_index_passes_an_explicit_dim_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _VectorStore(_BUILT)
    _patch_store(monkeypatch, fake)

    _invoke(
        ["vector-index", "--root", str(tmp_path), "--store", "postgres", "--dim", "8"]
    )

    assert fake.dims == [8]
    assert fake.closed is True


def test_vector_index_reports_a_built_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _VectorStore(_BUILT)
    _patch_store(monkeypatch, fake)

    result = _invoke(
        ["vector-index", "--root", str(tmp_path), "--store", "postgres", "--json"]
    )
    payload = json.loads(result.output)

    assert payload["supported"] is True
    assert payload["indexes"] == ["memories", "episodes"]


def test_vector_index_fails_loudly_when_the_build_did_not_happen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed-width corpus refuses to narrow; exiting 0 would hide a corpus
    still running on exact scans."""
    fake = _VectorStore(
        {
            "pgvector": True,
            "dim": 256,
            "narrowed": [],
            "indexes": [],
            "errors": ["memories: cannot narrow embedding to vector(256)"],
        }
    )
    _patch_store(monkeypatch, fake)

    result = _run(["vector-index", "--root", str(tmp_path), "--store", "postgres"])

    assert result.exit_code == 1
    assert "错误" in result.output
    assert "向量索引就绪" not in result.output
    assert fake.closed is True
