"""P3/P4 acceptance for the PostgreSQL adapter, without requiring a server.

Section 3.1 names PostgreSQL + pgvector as the target state and section 13 asks
for degradation rather than failure when it is unavailable. Both halves are
testable offline, and the offline half is the one that matters most: the adapter
is optional infrastructure, so the code that decides "use it" or "fall back and
say why" has to be right on a machine that has never had a database on it.

What is covered here:

* the DSN helpers, because a credential that reaches a log is a leak and a
  connection without a timeout is a hung analysis run;
* the pgvector literal codec and the ``tsquery`` builder, which is the Postgres
  counterpart of the FTS5 matcher and has to answer the same question about the
  same bigram-expanded text;
* schema application against a recording ``Execute``, including the rule that one
  failing DDL statement must not abort the rest;
* the factory's fallback path, asserted end to end through
  :func:`~trace_agent.memory.store.build_memory_store`;
* and, when ``TRACE_AGENT_MEMORY_DSN`` points at a reachable server, the real
  adapter held to the same contract as SQLite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from trace_agent.memory.flags import (
    MEMORY_CONNECT_TIMEOUT_ENV,
    MEMORY_DSN_ENV,
    MEMORY_SCHEMA_ENV,
)
from trace_agent.memory.store import build_memory_store, store_diagnostics
from trace_agent.memory.store.pg_schema import (
    PG_SCHEMA_VERSION,
    apply_schema,
    embedding_column_type,
    parse_pgvector_literal,
    pgvector_available,
    pgvector_literal,
    schema_version,
    server_version,
    tsquery_match,
)
from trace_agent.memory.store.postgres_store import (
    PostgresStore,
    PostgresUnavailable,
    load_driver,
    safe_dsn,
    with_connect_timeout,
)

# Read at import time, before ``_clean_env`` scrubs the process environment: an
# operator who points TRACE_AGENT_MEMORY_DSN at a real server has opted the live
# tests in, and a hermetic-env fixture must not quietly turn that opt-in into a
# skip. A contract test that always skips is a test that has stopped existing.
_AMBIENT_DSN = os.environ.get(MEMORY_DSN_ENV, "").strip()
_AMBIENT_SCHEMA = os.environ.get(MEMORY_SCHEMA_ENV, "").strip() or "public"
_AMBIENT_TIMEOUT = os.environ.get(MEMORY_CONNECT_TIMEOUT_ENV, "").strip() or "5"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSN in the developer's environment must not change what these assert."""
    for name in (MEMORY_DSN_ENV, MEMORY_SCHEMA_ENV, MEMORY_CONNECT_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------ DSN hygiene
@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@db.internal:5432/memory", "postgresql://u@db.internal:5432/memory"),
        ("postgres://db.internal/memory", "postgres://@db.internal/memory"),
        ("host=localhost dbname=memory password=s3cret", "host=localhost dbname=memory password=<redacted>"),
        ("", ""),
    ],
)
def test_safe_dsn_never_carries_a_password(dsn: str, expected: str) -> None:
    assert safe_dsn(dsn) == expected
    assert "s3cret" not in safe_dsn(dsn)
    assert "p@" not in safe_dsn(dsn)


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://h/db", "postgresql://h/db?connect_timeout=5"),
        ("postgresql://h/db?sslmode=require", "postgresql://h/db?sslmode=require&connect_timeout=5"),
        ("host=h dbname=db", "host=h dbname=db connect_timeout=3"),
        # An explicit choice in the DSN beats a default injected beside it.
        ("postgresql://h/db?connect_timeout=1", "postgresql://h/db?connect_timeout=1"),
        ("", ""),
    ],
)
def test_with_connect_timeout_preserves_the_dsn_syntax(
    dsn: str, expected: str
) -> None:
    seconds = 3.0 if "://" not in dsn else 5.0
    assert with_connect_timeout(dsn, seconds) == expected


def test_connect_timeout_is_bounded_below() -> None:
    """A sub-second timeout would make a cold connection look like an outage."""
    assert with_connect_timeout("host=h dbname=db", 0.2).endswith("connect_timeout=1")


# --------------------------------------------------------------- vector literals
def test_pgvector_literal_round_trips() -> None:
    vector = [0.1, -0.25, 1.0, 0.0]
    literal = pgvector_literal(vector)
    assert literal == "[0.1,-0.25,1.0,0.0]"
    assert parse_pgvector_literal(literal) == vector


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "[]", "not a vector", b"\xff\xfe", "[1,2,x]", "[1,2,]"],
)
def test_parse_pgvector_literal_degrades_to_unembedded(raw: Any) -> None:
    """An unreadable vector means "no semantic channel", never an error."""
    assert parse_pgvector_literal(raw) == []


def test_parse_pgvector_literal_accepts_bytes_and_bare_lists() -> None:
    assert parse_pgvector_literal(b"[1.5,2.5]") == [1.5, 2.5]
    assert parse_pgvector_literal("1.5,2.5") == [1.5, 2.5]


def test_embedding_column_type_follows_the_extension() -> None:
    assert embedding_column_type(pgvector=True) == "vector"
    # BYTEA keeps working with an exact Python cosine, which is what the SQLite
    # adapter already does, so semantics degrade rather than disappear.
    assert embedding_column_type(pgvector=False) == "BYTEA"


# -------------------------------------------------------------------- tsquery
def test_tsquery_match_ors_its_terms() -> None:
    """``tsquery`` ANDs by default; the pool has to mean "shares a token"."""
    expression = tsquery_match("白屏 耗时")
    assert expression is not None
    assert "|" in expression
    assert " & " not in expression
    for token in ("白屏", "耗时"):
        assert f"'{token}'" in expression


def test_tsquery_match_expands_cjk_into_bigrams() -> None:
    """Same units on both stores: the indexed bigrams are the queried bigrams."""
    expression = tsquery_match("冷启动白屏") or ""
    for bigram in ("冷启", "启动", "动白", "白屏"):
        assert f"'{bigram}'" in expression


def test_tsquery_match_escapes_quotes_and_deduplicates() -> None:
    """Repeated tokens collapse, and every lexeme stays single-quoted.

    :func:`~trace_agent.memory.text.tokenize` never emits a token containing a
    quote - ``it's`` arrives as ``it`` and ``s`` - so the ``''`` escaping in the
    builder is defence against a future tokenizer, and what is observable here is
    that the expression is a well-formed list of quoted lexemes with no
    duplicates: a repeated term would make ``ts_rank`` favour the repeated query
    rather than the better-matching record.
    """
    expression = tsquery_match("cold cold start")
    assert expression is not None
    assert expression.count("'cold'") == 1
    parts = [part.strip() for part in expression.split("|")]
    assert all(part.startswith("'") and part.endswith("'") for part in parts)
    assert len(parts) == len(set(parts))


@pytest.mark.parametrize("text", ["", "   ", "!!! ???", "\n\t"])
def test_tsquery_match_without_tokens_is_none(text: str) -> None:
    """An empty tsquery is a syntax error server-side, so it must not be built."""
    assert tsquery_match(text) is None


# ---------------------------------------------------------------- schema setup
class _Recorder:
    """An ``Execute`` that records statements and can be told to fail some."""

    def __init__(self, *, fail_on: tuple[str, ...] = (), rows: Any = None) -> None:
        self.statements: list[str] = []
        self.params: list[tuple[Any, ...]] = []
        self.fail_on = fail_on
        self.rows = rows if rows is not None else []

    def __call__(self, sql: str, params: Any = ()) -> Any:
        self.statements.append(sql)
        self.params.append(tuple(params))
        if any(marker in sql for marker in self.fail_on):
            raise RuntimeError(f"permission denied: {sql[:40]}")
        if sql.strip().startswith("SELECT"):
            return self.rows
        return []


def test_apply_schema_creates_every_table_and_stamps_a_version() -> None:
    recorder = _Recorder(rows=[(1,)])
    vectors = apply_schema(recorder, schema="public", pgvector=False)

    assert vectors is False
    joined = "\n".join(recorder.statements)
    for table in ("store_meta", "sessions", "memories", "episodes", "memory_versions"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
    assert "ADD COLUMN IF NOT EXISTS embedding BYTEA" in joined
    # The version insert is what ``memory migrate`` reads to decide whether a
    # snapshot belongs to this schema.
    assert any("schema_version" in sql for sql in recorder.statements)
    assert str(PG_SCHEMA_VERSION) in [str(p[0]) for p in recorder.params if p]


def test_apply_schema_uses_the_vector_column_when_pgvector_is_present() -> None:
    recorder = _Recorder()
    assert apply_schema(recorder, pgvector=True) is True
    assert "ADD COLUMN IF NOT EXISTS embedding vector" in "\n".join(
        recorder.statements
    )


def test_apply_schema_creates_a_non_public_schema() -> None:
    recorder = _Recorder()
    apply_schema(recorder, schema="memory_prod", pgvector=False)
    assert recorder.statements[0] == 'CREATE SCHEMA IF NOT EXISTS "memory_prod"'


def test_one_failing_statement_does_not_abort_the_ddl() -> None:
    """An index that cannot be created still leaves a working, slower corpus."""
    recorder = _Recorder(fail_on=("CREATE EXTENSION", "CREATE INDEX"))
    # ``pgvector=None`` is the real path: the adapter does not know yet, so the
    # extension probe runs and is allowed to fail.
    vectors = apply_schema(recorder, schema="public")
    joined = "\n".join(recorder.statements)
    assert vectors is False
    assert any(sql.startswith("CREATE EXTENSION") for sql in recorder.statements)
    assert "CREATE TABLE IF NOT EXISTS memories" in joined
    assert "ADD COLUMN IF NOT EXISTS embedding BYTEA" in joined
    assert any("schema_version" in sql for sql in recorder.statements)


def test_an_explicit_pgvector_flag_skips_the_probe() -> None:
    """A caller that already knows must not pay for - or fail on - a second look."""
    recorder = _Recorder()
    assert apply_schema(recorder, pgvector=False) is False
    assert not any("pg_extension" in sql for sql in recorder.statements)


def test_pgvector_available_reports_the_extension() -> None:
    assert pgvector_available(_Recorder(rows=[(1,)])) is True
    assert pgvector_available(_Recorder(rows=[])) is False
    assert pgvector_available(_Recorder(fail_on=("pg_extension",))) is False


class _SplitRecorder(_Recorder):
    """Answers the extension probe and the column-type probe independently.

    That split is the whole bug: a corpus created before pgvector existed keeps
    its ``BYTEA`` embedding column forever, because ``ADD COLUMN IF NOT EXISTS``
    never rewrites a column that is already there, while ``CREATE EXTENSION``
    starts succeeding the moment the server gains the extension.
    """

    def __init__(self, *, extension: bool, column_type: str | None) -> None:
        super().__init__()
        self._extension = extension
        self._column_type = column_type

    def __call__(self, sql: str, params: Any = ()) -> Any:
        self.statements.append(sql)
        self.params.append(tuple(params))
        if "pg_extension" in sql:
            return [(1,)] if self._extension else []
        if "typname" in sql:
            return [] if self._column_type is None else [(self._column_type,)]
        return []


def test_apply_schema_follows_the_bytea_column_over_a_present_extension() -> None:
    """The store casts to ``::vector``; over BYTEA every write would silently no-op."""
    recorder = _SplitRecorder(extension=True, column_type="bytea")
    assert apply_schema(recorder) is False
    assert any("typname" in sql for sql in recorder.statements)


def test_apply_schema_follows_the_vector_column_when_it_really_is_vector() -> None:
    assert apply_schema(_SplitRecorder(extension=True, column_type="vector")) is True


def test_apply_schema_keeps_the_probe_when_the_column_cannot_be_read() -> None:
    """An unreadable catalog is not evidence against pgvector, so do not guess."""
    assert apply_schema(_SplitRecorder(extension=True, column_type=None)) is True
    assert apply_schema(_SplitRecorder(extension=False, column_type=None)) is False


def test_schema_and_server_version_tolerate_a_broken_connection() -> None:
    broken = _Recorder(fail_on=("SELECT",))
    assert schema_version(broken) == 0
    assert server_version(broken) == ""
    assert schema_version(_Recorder(rows=[])) == 0
    assert schema_version(_Recorder(rows=[("not-a-number",)])) == 0
    assert schema_version(_Recorder(rows=[(PG_SCHEMA_VERSION,)])) == PG_SCHEMA_VERSION
    assert server_version(_Recorder(rows=[("PostgreSQL 17.2",)])).startswith("PostgreSQL")


# -------------------------------------------------------------------- driver
def test_load_driver_finds_an_installed_one() -> None:
    try:
        name, module = load_driver()
    except PostgresUnavailable:
        pytest.skip("no PostgreSQL driver installed")
    assert name in {"psycopg", "psycopg2"}
    assert callable(getattr(module, "connect", None))


def test_load_driver_raises_a_typed_error_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` in ``sys.modules`` makes ``import`` raise, which is the no-driver case."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    with pytest.raises(PostgresUnavailable):
        load_driver()


# ------------------------------------------------------------------- fallback
def test_requesting_postgres_without_a_dsn_degrades_to_a_local_corpus(
    tmp_path: Path,
) -> None:
    """Asking for PostgreSQL is a request, and an unhonourable one must not fail a run."""
    store = build_memory_store(tmp_path / "memory", kind="postgres")
    try:
        assert store.store_kind in {"sqlite", "json"}
        assert "postgres store unavailable" in (store.degraded_reason or "")
        assert "TRACE_AGENT_MEMORY_DSN" in (store.degraded_reason or "")
        # The fallback is a real corpus, not a store that discards writes.
        payload = store_diagnostics(store)
        assert payload["pushdown"] is True
    finally:
        store.close()


def test_an_unreachable_server_degrades_with_the_reason_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEMORY_DSN_ENV, "postgresql://nobody@127.0.0.1:1/memory")
    monkeypatch.setenv(MEMORY_CONNECT_TIMEOUT_ENV, "1")
    store = build_memory_store(tmp_path / "memory", kind="postgres")
    try:
        assert store.store_kind in {"sqlite", "json"}
        reason = store.degraded_reason or ""
        assert "postgres store unavailable" in reason
        # The credential-free form is what reaches a report.
        assert "nobody@" in reason or "127.0.0.1" in reason
    finally:
        store.close()


def test_postgres_store_reports_its_kind_without_connecting() -> None:
    assert PostgresStore.store_kind == "postgres"


# ------------------------------------------------------- live server (optional)
def _live_store(root: Path, namespace: str = "project:pg-test") -> Any:
    """A probed ``PostgresStore``, or a skip explaining why there is none."""
    if not _AMBIENT_DSN:
        pytest.skip(f"{MEMORY_DSN_ENV} is not set")
    try:
        store = PostgresStore(
            root,
            namespace=namespace,
            dsn=_AMBIENT_DSN,
            schema=_AMBIENT_SCHEMA,
            connect_timeout=float(_AMBIENT_TIMEOUT),
        )
        store.probe()
    except Exception as exc:  # noqa: BLE001 - unreachable means skip, not fail
        pytest.skip(f"PostgreSQL unreachable: {type(exc).__name__}: {exc}")
    return store


def test_live_server_holds_the_same_contract_as_sqlite(tmp_path: Path) -> None:
    """Write, recall, epoch and tenancy, against a real database."""
    from trace_agent.memory.models import (
        CuratedMemory,
        EpisodicMemory,
        MemoryKind,
        MemoryStatus,
        Provenance,
    )
    from trace_agent.memory.ports import MemoryQuery
    from trace_agent.memory.recall import RecallService

    store = _live_store(tmp_path / "memory")
    try:
        memory = CuratedMemory(
            memory_id="mem-pg-live",
            kind=MemoryKind.PATTERN,
            scenario_type="cold_start",
            topic="冷启动白屏",
            content="main thread blocked by sync IO during cold start",
            keywords=["冷启动", "白屏", "主线程"],
            status=MemoryStatus.ACTIVE,
            confidence=0.8,
            provenance=Provenance(source_type="finding", references=["run:pg-1"]),
        )
        store.write_curated(memory)
        store.write_episode(
            EpisodicMemory(
                episode_id="ep-pg-live",
                run_id="run-pg-live",
                trace_id="trace-pg",
                scenario_type="cold_start",
                scenario="冷启动白屏",
                symptom="启动后长时间白屏",
                summary="main thread blocked by sync IO",
                root_causes=["main thread blocked by sync IO"],
            )
        )

        loaded = store.load_curated("mem-pg-live")
        assert loaded is not None and loaded.topic == "冷启动白屏"
        assert store.list_curated(active_only=True)

        pushdown = store.search(
            MemoryQuery(text="冷启动 白屏", namespace=store.namespace, limit=50)
        )
        assert any(
            getattr(row.memory, "memory_id", "") == "mem-pg-live" for row in pushdown
        )

        result = RecallService(store).recall("冷启动 白屏", scenario_type="cold_start")
        assert "mem-pg-live" in [
            getattr(hit.memory, "memory_id", "") for hit in result.hits
        ]

        epoch = store.corpus_epoch()
        store.write_curated(
            CuratedMemory(
                memory_id="mem-pg-live-2",
                kind=MemoryKind.FACT,
                scenario_type="cold_start",
                topic="启动阶段主线程同步 IO",
                content="sync file read on the main thread",
                keywords=["冷启动"],
                status=MemoryStatus.ACTIVE,
                confidence=0.7,
                provenance=Provenance(source_type="finding", references=["run:pg-2"]),
            )
        )
        assert store.corpus_epoch() > epoch
        store.touch("mem-pg-live")
        assert store.corpus_epoch() > epoch

        namespaces = store.list_namespaces()
        assert any(row["namespace"] == store.namespace for row in namespaces)
        assert any(row["current"] for row in namespaces)

        summary = store.stats()
        assert summary.memories >= 2 and summary.episodes >= 1

        _assert_governance_import_is_idempotent(store)
    finally:
        store.close()


def _assert_governance_import_is_idempotent(store: Any) -> None:
    """The file-adapter -> SQL replay property, against a real PostgreSQL.

    A bundle exported by :class:`JsonStore` carries audit rows with no ordinal, so
    the SQL adapter has to derive one. Deriving it as ``MAX(version) + 1`` gives a
    *different* answer on every replay, ``ON CONFLICT DO NOTHING`` never fires,
    and each re-run of a migration duplicates the whole history. The SQLite half
    of this is asserted in ``tests/test_memory_store.py``; the Postgres half needs
    a server, which is why it lives here.

    ``reason`` carries a per-call suffix because the live namespace persists
    between runs: a fixed reason would already be in the table on the second run
    of this test, and "the first import added one row" would fail for the opposite
    of the reason under test.
    """
    from uuid import uuid4

    from trace_agent.memory.migrate import export_bundle, import_bundle
    from trace_agent.memory.models import MemoryStatus
    from trace_agent.memory.ports import MemoryRelation

    store.set_trust("mem-pg-live", MemoryStatus.VERIFIED, reason="expert-signed")
    store.link("mem-pg-live", "mem-pg-live-2", MemoryRelation.SUPPORTS)

    sidecar = [
        # No ``version`` key at all: this is the shape ``JsonStore``'s
        # ``versions.jsonl`` produces and ``export_bundle`` carries over.
        {
            "memory_id": "mem-pg-live",
            "topic": "冷启动白屏",
            "content": "main thread blocked by sync IO during cold start",
            "from_trust": "candidate",
            "trust": "active",
            "confidence": 0.8,
            "reason": f"sidecar-{uuid4().hex[:8]}",
            "actor": None,
            "created_at": "2026-03-01T12:00:00+00:00",
        }
    ]
    bundle = {
        "version": 2,
        "store": "json",
        "namespace": store.namespace,
        "episodes": [],
        "memories": [],
        "sessions": [],
        "versions": sidecar,
        "links": store.list_links(limit=1000),
    }

    first = import_bundle(store, bundle)
    assert first.versions == 1, first.as_dict()
    # Links were already present, so the conflict path is what made this zero.
    assert first.links == 0, first.as_dict()
    rows = len(store.recent_versions(limit=100_000))
    for _ in range(2):
        replay = import_bundle(store, bundle)
        assert replay.versions == 0, "an ordinal-less row was renumbered and re-added"
        assert replay.links == 0, replay.as_dict()
    assert len(store.recent_versions(limit=100_000)) == rows

    # The hand-written row above is the shape ``JsonStore``'s sidecar produces, and
    # the export still round-trips - so this did not test a row the real exporter
    # would never emit.
    exported = export_bundle(store)["versions"]
    assert any(row["reason"] == sidecar[0]["reason"] for row in exported)


def test_live_server_backup_produces_a_restorable_bundle(tmp_path: Path) -> None:
    """No local data directory to copy, so the honest artifact is a logical one."""
    from trace_agent.memory.backup import KIND_BUNDLE, backup_corpus

    store = _live_store(tmp_path / "memory")
    try:
        report = backup_corpus(store, tmp_path / "backup")
    finally:
        store.close()

    assert report.store_kind == "postgres"
    assert [item.kind for item in report.artifacts] == [KIND_BUNDLE]
    assert report.manifest is not None and report.manifest.is_file()
    assert report.notes, "the operator must be told the snapshot is logical"
    for item in report.artifacts:
        assert item.path.is_file() and item.sha256
