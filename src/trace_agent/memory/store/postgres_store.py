"""PostgreSQL adapter for the long-term memory corpus.

The third implementation of :class:`~trace_agent.memory.ports.MemoryStore`, and
the one section 3.1 of the design doc names as the target state: relational
columns and JSONB metadata in one ACID database, full-text search over the same
bigram-expanded body SQLite indexes, and vectors either in pgvector or in BYTEA.

It exists because the *shape* of the corpus outgrew a single machine, not because
SQLite ran out of features. The four triggers in section 3.1 - concurrent writers,
a corpus big enough to need ANN, cross-project sharing with permissions, and
joins against business tables - are all properties of PostgreSQL and none of them
are properties of a file. Until one of them fires the default stays SQLite; this
adapter is what makes "switch when it fires" a configuration change instead of a
rewrite, which is the whole reason the port was written first.

Three rules keep it honest against the other adapters:

* **Reads come from the ``payload`` column.** Structured columns are caches for
  filtering, ordering and analytics. A PostgreSQL corpus and a SQLite corpus
  holding the same writes therefore return the same objects, which is what makes
  ``memory migrate`` between them a copy rather than a translation.
* **Recall semantics are identical.** Same filters pushed into SQL, same
  trust/scenario rules, same "more negative lexical rank is better" convention
  (``ts_rank`` is negated to match SQLite's bm25), same deterministic tie-break.
  A store switch may change latency; it may not change an answer.
* **Nothing here is fatal.** Every optional capability - pgvector, an ANN index,
  a schema permission - degrades to the behaviour the SQLite adapter already has,
  and a server that cannot be reached at all degrades to the file adapter in
  :func:`~trace_agent.memory.store.build_memory_store`.

Connections are thread-local and run in autocommit, with explicit ``BEGIN`` in
:meth:`PostgresStore._tx` for multi-statement writes. That mirrors the SQLite
adapter's structure without its process-wide writer lock: PostgreSQL does
row-level locking server-side, so serialising writers in the client would only
remove the concurrency that is the reason to be here.

The dream queue and ``execution_snapshot`` stay file-backed, exactly as they do on
SQLite: the queue is a transient work list whose JSONL semantics the dreaming beat
depends on, and a checkpoint is a projection of a *run* directory, not corpus
state. A multi-instance deployment shares the corpus through PostgreSQL and the
queue through the file root, which is why the root is still required.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.embedding.ports import cosine, pack_vector, unpack_vector
from trace_agent.memory.episode import apply_grade
from trace_agent.memory.flags import memory_dsn, memory_namespace
from trace_agent.memory.models import (
    RETIRED_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    ExecutionCheckpointSnapshot,
    MemoryStatus,
    SessionMemory,
)
from trace_agent.memory.ports import (
    MemoryQuery,
    MemoryStats,
    MemoryTrust,
    PendingEmbedding,
    ScoredMemory,
    memory_type_for,
)
from trace_agent.memory.rerank import episode_confidence, importance_for
from trace_agent.memory.search_text import (
    curated_index_text,
    episode_index_text,
)
from trace_agent.memory.store import pg_schema
from trace_agent.memory.store.fts import fts_body
from trace_agent.memory.store.rows import (
    VersionResolver,
    as_float,
    dumps_json,
    iso_stamp,
    optional_int,
    parse_stamp,
    state_key,
    stored_validator,
    validate_payload,
)
from trace_agent.models import utc_now

# Trust levels ``list_curated(active_only=True)`` hides. The same constant the
# other two adapters use, so a rollback cannot change what "retired" means.
_RETIRED_TRUSTS: tuple[str, ...] = tuple(
    sorted(status.value for status in RETIRED_STATUSES)
)

# ``memory_links.relation`` values counted by :class:`MemoryStats`. Written out
# rather than imported from ``ports`` only to keep the SQL strings greppable; the
# vocabulary itself is defined once in :class:`~trace_agent.memory.ports.MemoryRelation`.
_CONTRADICTS = "contradicts"
_DUPLICATES = "duplicate-of"

# Seconds a connection attempt may block. Bounded because recall sits inside an
# analysis run: a PostgreSQL server that is down must cost memory, not the run.
DEFAULT_CONNECT_TIMEOUT = 5.0


class PostgresUnavailable(RuntimeError):
    """Raised when no driver is installed or the server cannot be reached.

    Distinct from a driver's own error type so the factory can degrade on exactly
    this condition without catching - and therefore hiding - a programming error
    in a query.
    """


def load_driver() -> tuple[str, Any]:
    """Import a PostgreSQL driver, preferring psycopg 3.

    Both are supported because the API surface used here is the intersection of
    the two: ``connect(dsn)``, ``cursor()``, ``execute(sql, params)``,
    ``commit``, ``rollback``, ``rowcount``. psycopg 3 is tried first as the
    maintained one; psycopg2 is what a lot of existing environments already
    carry, and refusing it would make the adapter useless exactly where it is
    most likely to be wanted.

    Neither is a runtime dependency of this package. Importing lazily, here, is
    what keeps ``TRACE_AGENT_MEMORY_STORE=sqlite`` installable with five
    dependencies and no database driver.
    """
    try:
        import psycopg  # type: ignore[import-not-found]

        return "psycopg", psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore[import-not-found]

        return "psycopg2", psycopg2
    except ImportError as exc:
        raise PostgresUnavailable(
            "no PostgreSQL driver installed (need psycopg or psycopg2)"
        ) from exc


def with_connect_timeout(dsn: str, seconds: float = DEFAULT_CONNECT_TIMEOUT) -> str:
    """Return ``dsn`` with a bounded connect timeout, preserving its syntax.

    Both the URI form (``postgresql://host/db``) and the keyword form
    (``host=localhost dbname=memory``) are accepted, because operators paste
    whichever their platform gave them. An existing timeout is left alone: an
    explicit choice in the DSN beats a default injected beside it.
    """
    text = (dsn or "").strip()
    if not text or "connect_timeout" in text:
        return text
    value = str(max(1, int(seconds)))
    if "://" in text:
        separator = "&" if "?" in text else "?"
        return f"{text}{separator}connect_timeout={value}"
    return f"{text} connect_timeout={value}"


def safe_dsn(dsn: str) -> str:
    """DSN with any password removed, for logs and ``memory stats``.

    A store that cannot be reached is something an operator has to diagnose from
    a report, and the report must not be the place the credential leaks. Both the
    URI ``user:pass@host`` form and the keyword ``password=...`` form are
    handled, and anything unparseable is reduced to a placeholder rather than
    echoed.
    """
    text = (dsn or "").strip()
    if not text:
        return ""
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return "<dsn>"
        user = parts.username or ""
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        database = (parts.path or "/").lstrip("/")
        return f"{parts.scheme}://{user}@{host}{port}/{database}"
    fields = []
    for token in text.split():
        key = token.split("=", 1)[0].lower()
        # The key survives, the value does not: an operator reading a failure
        # report needs to know a password *was* supplied, and showing which
        # field was hidden costs nothing.
        fields.append(f"{key}=<redacted>" if key == "password" else token)
    return " ".join(fields)


class PostgresStore:
    """PostgreSQL-backed memory store with tsvector search and optional pgvector."""

    #: Value reported by ``TRACE_AGENT_MEMORY_STORE`` and ``memory stats``.
    store_kind = "postgres"

    #: Set by the factory when this store was requested but could not be used.
    degraded_reason: str | None = None

    def __init__(
        self,
        root: Path | str,
        *,
        namespace: str | None = None,
        dsn: str | None = None,
        schema: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        # Still required: the dream queue and checkpoint sidecars are files on
        # every adapter, and an export or backup writes its snapshot here.
        self.root = Path(root)
        self.dsn = (dsn if dsn is not None else memory_dsn()) or ""
        if not self.dsn.strip():
            raise PostgresUnavailable(
                "TRACE_AGENT_MEMORY_DSN is not set (required for store=postgres)"
            )
        self.schema = (schema or "public").strip() or "public"
        self.connect_timeout = float(connect_timeout)
        # Resolved once at construction, as in the other adapters: a namespace
        # that changed mid-process would split one corpus across two domains.
        self.namespace = (
            namespace if namespace is not None else memory_namespace()
        )
        self._local = threading.local()
        self._files = JsonMemoryBackend(self.root)
        self.driver_name = ""
        self._pgvector: bool = False
        self._server_version = ""

    # ----------------------------------------------------------- diagnostics
    @property
    def db_path(self) -> None:
        """``None``: the corpus lives in a server, not a file."""
        return None

    @property
    def server(self) -> str:
        """Credential-free description of where this corpus lives."""
        return safe_dsn(self.dsn)

    @property
    def server_version(self) -> str:
        return self._server_version

    @property
    def pgvector(self) -> bool:
        """Whether vectors are stored in pgvector rather than BYTEA."""
        return self._pgvector

    # ------------------------------------------------------- connection mgmt
    def _conn(self) -> Any:
        """This thread's connection, applying the schema on first use.

        Lazy for the same reason as the SQLite adapter: constructing a runtime
        must not require a reachable database, so a CLI invocation that never
        touches memory neither fails nor hangs on a server that is down.
        """
        conn: Any = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        name, module = load_driver()
        try:
            conn = module.connect(
                with_connect_timeout(self.dsn, self.connect_timeout)
            )
        except Exception as exc:
            raise PostgresUnavailable(
                f"cannot connect to {safe_dsn(self.dsn)}: {exc}"
            ) from exc
        # Autocommit with explicit BEGIN in ``_tx``. Leaving the driver's default
        # (a transaction opened by the first statement) would keep read-only
        # recall "idle in transaction" for the life of the connection, pinning a
        # snapshot on the server and blocking vacuum - a real cost for a dashboard
        # process that holds connections open.
        conn.autocommit = True
        self.driver_name = name
        self._local.conn = conn
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._pgvector = pg_schema.apply_schema(
            self._execute_raw, schema=self.schema
        )
        self._server_version = pg_schema.server_version(self._execute_raw)
        return conn

    def _execute_raw(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """Run one statement on this thread's connection and return its rows.

        The schema module is handed this callable rather than a connection, so it
        stays free of any driver import and can be exercised in tests without a
        server.
        """
        rows, _ = self._run(sql, params)
        return rows

    def _run(self, sql: str, params: Sequence[Any] = ()) -> tuple[list[Any], int]:
        """Execute one statement, returning ``(rows, rowcount)``.

        ``rowcount`` is part of the return because two port members are defined by
        it: :meth:`add_source` answers "is this run *new* evidence", and
        :meth:`set_embedding` answers "did a row exist to update". Both are the
        difference between a governance decision and a guess.
        """
        conn = self._conn()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall() if cursor.description is not None else []
            count = cursor.rowcount
            return list(rows), int(count if count is not None and count >= 0 else 0)
        finally:
            cursor.close()

    @contextmanager
    def _tx(self) -> Iterator["PostgresStore"]:
        """Group statements into one transaction; commit or roll back.

        Nesting is supported, as in the SQLite adapter, so a caller written for
        one record per transaction can be wrapped in :meth:`batch` without knowing
        about it. An inner block yields the same store and commits nothing.
        """
        self._conn()
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            try:
                yield self
            finally:
                self._local.depth = depth
            return
        self._execute_raw("BEGIN")
        self._local.depth = 1
        try:
            yield self
        except BaseException:
            self._rollback()
            self._local.depth = 0
            raise
        self._execute_raw("COMMIT")
        self._local.depth = 0

    def _rollback(self) -> None:
        try:
            self._execute_raw("ROLLBACK")
        except Exception:
            # A connection that cannot roll back is unusable; dropping it means
            # the next statement reconnects instead of failing forever.
            self.close()

    @contextmanager
    def batch(self) -> Iterator["PostgresStore"]:
        """Group many writes into one transaction, as the SQLite adapter does."""
        with self._tx() as store:
            yield store

    def close(self) -> None:
        """Release this thread's connection. Safe to call more than once."""
        conn: Any = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        self._local.depth = 0
        try:
            conn.close()
        except Exception:
            return

    @property
    def version(self) -> int:
        """Schema version this database was stamped with."""
        return pg_schema.schema_version(self._execute_raw)

    def probe(self) -> str:
        """Force a connection, apply the schema, and report the server version.

        This is the factory's equivalent of reading :attr:`SqliteStore.version`:
        the one moment where "can this adapter actually be used" has to be
        answered before any record is written. It raises
        :class:`PostgresUnavailable` when there is no driver, when the server
        cannot be reached inside ``connect_timeout``, or when the DDL did not land
        - the last one matters because :func:`pg_schema.apply_schema` swallows
        per-statement failures by design, so a role without ``CREATE`` would
        otherwise produce a store that connects happily and then loses every
        write to a caught best-effort exception. An empty corpus that looks healthy
        is the failure mode the probe exists to prevent.
        """
        self._conn()
        if pg_schema.schema_version(self._execute_raw) <= 0:
            raise PostgresUnavailable(
                f"schema could not be applied in {safe_dsn(self.dsn)} "
                f"(schema={self.schema}); the role may lack CREATE"
            )
        return self._server_version

    # --------------------------------------------------------------- helpers
    def _one(self, sql: str, params: Sequence[Any] = ()) -> Any | None:
        rows, _ = self._run(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _marks(values: Sequence[Any]) -> str:
        """Placeholder list for an ``IN`` clause (``%s`` paramstyle)."""
        return ", ".join("%s" for _ in values)

    @staticmethod
    def _json_equal(left: Any, right: Any) -> bool:
        """Compare two JSON values that may arrive as text or already parsed.

        ``scope`` is JSONB, so PostgreSQL returns a dict while the value written
        was a string - and JSONB does not preserve key order, so comparing the
        text would report a change on every write. A false "changed" here is not
        cosmetic: it retires the stored embedding, silently dropping a record out
        of the semantic channel until the next backfill.
        """
        return _as_json(left) == _as_json(right)

    def _previous_changed(
        self,
        previous: Any,
        columns: Sequence[str],
        current: dict[str, Any],
    ) -> bool:
        """Whether the columns the indexed text is built from changed.

        ``previous is None`` means an insert, where there is no stored vector to
        retire. Columns absent from ``current`` are skipped rather than treated as
        changed, so a caller that selects more than it compares cannot invalidate
        the whole corpus on every write. ``scope`` is compared as JSON, everything
        else as stored text.
        """
        if previous is None:
            return False
        for index, column in enumerate(columns):
            if column not in current:
                continue
            stored = previous[index]
            value = current[column]
            if column == "scope":
                if not self._json_equal(stored, value):
                    return True
                continue
            if stored != value:
                return True
        return False

    # -------------------------------------------------------------- episodes
    def write_episode(self, episode: EpisodicMemory) -> None:
        """Persist one run's episode projection, keeping one row per run.

        ``UNIQUE (namespace, run_id)`` is the section-4 idempotency rule. A run
        re-analysed under a *new* episode id would otherwise either violate the
        constraint or leave two projections of the same run competing in recall,
        so the stale row and its index entry are retired first: the newest
        projection of a run is the one that counts.
        """
        payload = dumps_json(episode.model_dump(mode="json"))
        created = iso_stamp(episode.created_at) or iso_stamp(utc_now()) or ""
        with self._tx():
            rows, _ = self._run(
                "SELECT id FROM episodes WHERE namespace = %s AND run_id = %s "
                "AND id <> %s",
                (self.namespace, episode.run_id, episode.episode_id),
            )
            for row in rows:
                stale_id = str(row[0])
                self._run(
                    "DELETE FROM episodes_fts WHERE namespace = %s "
                    "AND episode_id = %s",
                    (self.namespace, stale_id),
                )
                self._run(
                    "DELETE FROM episodes WHERE namespace = %s AND id = %s",
                    (self.namespace, stale_id),
                )
            previous = self._one(
                "SELECT scenario_type, scenario, symptom, summary, root_causes, "
                "target_process, device, build "
                "FROM episodes WHERE namespace = %s AND id = %s",
                (self.namespace, episode.episode_id),
            )
            self._upsert_episode_row(episode, payload, created)
            self._index_episode(episode)
            # Same rule as ``_put_curated``: a re-projection whose narrative
            # changed retires the vector, an identical one keeps it.
            if self._previous_changed(
                previous,
                (
                    "scenario_type",
                    "scenario",
                    "symptom",
                    "summary",
                    "root_causes",
                    "target_process",
                    "device",
                    "build",
                ),
                {
                    "scenario_type": episode.scenario_type,
                    "scenario": episode.scenario,
                    "symptom": episode.symptom,
                    "summary": episode.summary,
                    "root_causes": dumps_json(episode.root_causes),
                    "target_process": episode.target_process,
                    "device": episode.device,
                    "build": episode.build,
                },
            ):
                self._clear_embedding("episodes", episode.episode_id)
            self.bump_epoch()

    def _upsert_episode_row(
        self, episode: EpisodicMemory, payload: str, created: str
    ) -> None:
        self._run(
            """
            INSERT INTO episodes (
              namespace, id, session_id, project_id, run_id, trace_id,
              scenario_type, scenario, symptom,
              target_process, device, build, summary, root_causes, outcome,
              confidence, eval_run_id, graded_pass, source_run_dir, created_at,
              payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, id) DO UPDATE SET
              session_id = excluded.session_id,
              project_id = excluded.project_id,
              run_id = excluded.run_id,
              trace_id = excluded.trace_id,
              scenario_type = excluded.scenario_type,
              scenario = excluded.scenario,
              symptom = excluded.symptom,
              target_process = excluded.target_process,
              device = excluded.device,
              build = excluded.build,
              summary = excluded.summary,
              root_causes = excluded.root_causes,
              outcome = excluded.outcome,
              confidence = excluded.confidence,
              eval_run_id = excluded.eval_run_id,
              graded_pass = excluded.graded_pass,
              source_run_dir = excluded.source_run_dir,
              payload = excluded.payload
            """,
            (
                self.namespace,
                episode.episode_id,
                episode.session_id,
                episode.project_id,
                episode.run_id,
                episode.trace_id,
                episode.scenario_type,
                episode.scenario,
                episode.symptom,
                episode.target_process,
                episode.device,
                episode.build,
                episode.summary,
                dumps_json(episode.root_causes),
                episode.status,
                episode_confidence(episode),
                episode.eval_run_id,
                None if episode.graded_pass is None else int(episode.graded_pass),
                episode.output_dir,
                created,
                payload,
            ),
        )

    def _index_episode(self, episode: EpisodicMemory) -> None:
        """Replace one episode's full-text row.

        ``ON CONFLICT DO UPDATE`` instead of the SQLite adapter's delete-by-rowid:
        PostgreSQL has no stable implicit id, and the side table is keyed by the
        record's real identity anyway.
        """
        self._run(
            "INSERT INTO episodes_fts (namespace, episode_id, body) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (namespace, episode_id) DO UPDATE SET body = excluded.body",
            (
                self.namespace,
                episode.episode_id,
                fts_body(episode_index_text(episode)),
            ),
        )

    def load_episode(self, episode_id: str) -> EpisodicMemory | None:
        row = self._one(
            "SELECT payload FROM episodes WHERE namespace = %s AND id = %s",
            (self.namespace, episode_id),
        )
        if row is None:
            return None
        return validate_payload(EpisodicMemory, row[0])

    def list_episodes(self) -> list[EpisodicMemory]:
        """All episodes in this namespace, newest first.

        The final sort happens in Python even though SQL ordered the rows, so the
        result matches the other adapters exactly - including for legacy records
        whose timestamps predate timezone awareness.
        """
        rows, _ = self._run(
            "SELECT payload FROM episodes WHERE namespace = %s "
            "ORDER BY created_at DESC",
            (self.namespace,),
        )
        episodes = [
            item
            for item in (validate_payload(EpisodicMemory, row[0]) for row in rows)
            if item is not None
        ]
        return sorted(episodes, key=lambda item: item.created_at, reverse=True)

    def mark_episode_graded(
        self,
        episode_id: str,
        *,
        graded_pass: bool,
        graded_score: float | None = None,
        eval_run_id: str | None = None,
    ) -> bool:
        """Stamp a grade onto one episode through the ordinary write path.

        Going through :meth:`write_episode` rather than an ``UPDATE`` keeps the
        payload, the dedicated ``graded_pass`` column and the index row in one
        transaction, so a graded episode cannot end up with a stale index entry.
        Re-grading a trial after a threshold change stays safe because the write
        is idempotent on ``(namespace, run_id)``.
        """
        episode = self.load_episode(episode_id)
        if episode is None:
            return False
        self.write_episode(
            apply_grade(
                episode,
                graded_pass=graded_pass,
                graded_score=graded_score,
                eval_run_id=eval_run_id,
            )
        )
        return True

    # --------------------------------------------------------------- curated
    def write_curated(self, memory: CuratedMemory) -> None:
        """Persist a curated memory, exactly as the other adapters would.

        ``updated_at`` is refreshed here rather than by the caller, for the reason
        the file backend gives: freshness is a ranking signal, and a store that did
        not bump it would make every memory look older than it is.
        """
        self._put_curated(memory, reason="consolidate")

    def _put_curated(
        self,
        memory: CuratedMemory,
        *,
        reason: str,
        actor: str | None = None,
        stamp: datetime | None = None,
    ) -> None:
        """Write one memory row plus its index, sources, version history and epoch.

        Column ownership follows the SQLite adapter exactly. Caller-owned columns
        are rewritten from the model on every write; store-owned ones -
        ``contradictions``, ``version``, ``last_accessed_at``, ``supersedes_id``,
        ``created_at`` - are excluded from the ``DO UPDATE`` set, so a routine
        consolidation cannot wipe out decay bookkeeping or an access timestamp.
        ``expires_at`` is the deliberate exception: retention owns it, but it lives
        on the record so every adapter honours the same decision.
        """
        if stamp is not None:
            memory = memory.model_copy(update={"updated_at": stamp})
        else:
            memory = memory.model_copy(update={"updated_at": utc_now()})
        payload = dumps_json(memory.model_dump(mode="json"))
        episode_ids = sorted(memory.source_episode_ids)
        # Owned by the record, not by the adapter, so the two SQL stores cannot
        # disagree about what a project-scoped query matches.
        scope = dumps_json(memory.scope)

        with self._tx():
            previous = self._one(
                "SELECT version, topic, content, trust, confidence, "
                "distinct_runs, kind, keywords, scope, provenance "
                "FROM memories WHERE namespace = %s AND id = %s",
                (self.namespace, memory.memory_id),
            )
            current_state = state_key(
                memory.topic, memory.content, memory.status.value, memory.confidence
            )
            tail = None
            reimported = False
            # Version numbers continue from the audit trail rather than from the
            # row, and on *both* paths. ``remove_curated`` deliberately keeps
            # ``memory_versions``, so a memory re-created under the same id must
            # not restart at 1 and collide with the history it left behind -
            # which would raise inside a consolidation and cost the run its
            # memory write. The trail can also run ahead of a row that still
            # exists: ``import_governance`` derives an ordinal for an ordinal-less
            # sidecar row from ``MAX(version)`` and leaves the record alone, so a
            # migration replayed onto a populated target puts ordinals there the
            # row's own counter has not reached. Either way the next one has to
            # clear both, or the insert collides with
            # ``UNIQUE(namespace, memory_id, version)``.
            highest = self._one(
                "SELECT COALESCE(MAX(version), 0) FROM memory_versions "
                "WHERE namespace = %s AND memory_id = %s",
                (self.namespace, memory.memory_id),
            )
            last_version = int(highest[0] if highest else 0)
            if previous is None:
                version = last_version + 1
            else:
                version = max(int(previous[0]), last_version) + 1
            if previous is None and last_version:
                # ``memory migrate`` loads the audit trail before the records,
                # so an insert can arrive with history that already ends at
                # exactly this state. Recognising that is what keeps a
                # re-run migration from appending a duplicate tail.
                tail = self._one(
                    "SELECT topic, content, trust, confidence "
                    "FROM memory_versions "
                    "WHERE namespace = %s AND memory_id = %s AND version = %s",
                    (self.namespace, memory.memory_id, last_version),
                )
                if tail is not None and state_key(*tail[:4]) == current_state:
                    reimported = True
                    version = last_version
            if reimported:
                changed = False
            elif tail is not None:
                # Deleted and re-created under the same id with different content:
                # a real transition, and the history says so.
                changed = True
            else:
                changed = previous is None or (
                    previous[1] != memory.topic
                    or previous[2] != memory.content
                    or previous[3] != memory.status.value
                    or float(previous[4]) != float(memory.confidence)
                    or int(previous[5]) != memory.distinct_runs
                    # Parity with the SQLite adapter: an expert signature changes
                    # what a record claims without touching its text, and section 7
                    # requires that to be auditable. ``provenance`` is appended
                    # last in the SELECT and deliberately left out of the
                    # ``_previous_changed`` column list, because a signature is
                    # not part of the indexed text and must not retire the vector.
                    or stored_validator(previous[9])
                    != ((memory.provenance.validated_by or "").strip() or None)
                )
            if not changed and previous is not None:
                # Re-consolidating identical evidence is a no-op. Recording a
                # version per write would bury real transitions in noise.
                version = int(previous[0])
            # The other half of the transition, stored with it: ``trust`` alone
            # says where the record ended up, and reconstructing where it came
            # from by reading the previous row is exactly what breaks on a
            # migrated corpus. ``None`` on an insert means "first version".
            from_trust = None if previous is None else previous[3]

            self._upsert_memory_row(
                memory, payload=payload, scope=scope, version=version
            )
            self._index_memory(memory)
            self._sync_sources(memory, episode_ids)
            # Editing the indexed text retires the vector. Trust, confidence and
            # corroboration deliberately do not: they reorder recall without
            # changing what the record says, and re-embedding on a promotion would
            # burn provider calls for a vector that would come out identical.
            if self._previous_changed(
                previous,
                (
                    "version",
                    "topic",
                    "content",
                    "trust",
                    "confidence",
                    "distinct_runs",
                    "kind",
                    "keywords",
                    "scope",
                ),
                {
                    "topic": memory.topic,
                    "content": memory.content,
                    "kind": memory.kind.value,
                    "keywords": dumps_json(memory.keywords),
                    "scope": scope,
                },
            ):
                self._clear_embedding("memories", memory.memory_id)
            if changed:
                self._run(
                    "INSERT INTO memory_versions (namespace, memory_id, version, "
                    "topic, content, from_trust, trust, confidence, reason, "
                    "actor, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        self.namespace,
                        memory.memory_id,
                        version,
                        memory.topic,
                        memory.content,
                        from_trust,
                        memory.status.value,
                        memory.confidence,
                        reason,
                        actor,
                        iso_stamp(memory.updated_at) or "",
                    ),
                )
            self.bump_epoch()

    def _upsert_memory_row(
        self,
        memory: CuratedMemory,
        *,
        payload: str,
        scope: str,
        version: int,
    ) -> None:
        episode_ids = sorted(memory.source_episode_ids)
        self._run(
            """
            INSERT INTO memories (
              namespace, id, type, kind, topic, content, keywords, scope, trust,
              status_reason, confidence, importance, severity, occurrences,
              distinct_runs, version, source_episode_id, superseded_by,
              archived_reason, provenance, payload, created_at, updated_at,
              expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace, id) DO UPDATE SET
              type = excluded.type,
              kind = excluded.kind,
              topic = excluded.topic,
              content = excluded.content,
              keywords = excluded.keywords,
              scope = excluded.scope,
              trust = excluded.trust,
              status_reason = excluded.status_reason,
              confidence = excluded.confidence,
              importance = excluded.importance,
              severity = excluded.severity,
              occurrences = excluded.occurrences,
              distinct_runs = excluded.distinct_runs,
              version = excluded.version,
              source_episode_id = excluded.source_episode_id,
              superseded_by = excluded.superseded_by,
              archived_reason = excluded.archived_reason,
              provenance = excluded.provenance,
              payload = excluded.payload,
              updated_at = excluded.updated_at,
              expires_at = excluded.expires_at
            """,
            (
                self.namespace,
                memory.memory_id,
                memory_type_for(memory.kind).value,
                memory.kind.value,
                memory.topic,
                memory.content,
                dumps_json(memory.keywords),
                scope,
                memory.status.value,
                memory.status_reason,
                memory.confidence,
                importance_for(memory),
                memory.severity.value if memory.severity is not None else None,
                memory.occurrences,
                memory.distinct_runs,
                version,
                episode_ids[0] if episode_ids else None,
                memory.superseded_by,
                memory.archived_reason,
                dumps_json(memory.provenance.model_dump(mode="json")),
                payload,
                iso_stamp(memory.created_at) or "",
                iso_stamp(memory.updated_at) or "",
                iso_stamp(memory.expires_at),
            ),
        )

    def _index_memory(self, memory: CuratedMemory) -> None:
        self._run(
            "INSERT INTO memories_fts (namespace, memory_id, body) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (namespace, memory_id) DO UPDATE SET body = excluded.body",
            (
                self.namespace,
                memory.memory_id,
                fts_body(curated_index_text(memory)),
            ),
        )

    def _sync_sources(self, memory: CuratedMemory, episode_ids: list[str]) -> None:
        """Make ``memory_sources`` agree with provenance, then re-derive the count.

        ``ON CONFLICT DO NOTHING`` against ``PRIMARY KEY (namespace, memory_id,
        run_id)`` is the mechanical half of the section-1.2 defect-A fix: the same
        run consolidated twice inserts one row, so ``distinct_runs`` cannot be
        inflated by repetition. The column is then overwritten from a COUNT rather
        than incremented, so it can never drift from the evidence it summarises.
        """
        now = iso_stamp(utc_now()) or ""
        episode_id = episode_ids[0] if episode_ids else ""
        for run_id in sorted(memory.source_run_ids):
            self._run(
                "INSERT INTO memory_sources "
                "(namespace, memory_id, run_id, episode_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (self.namespace, memory.memory_id, run_id, episode_id, now),
            )
        self._refresh_distinct_runs(memory.memory_id)

    def load_curated(self, memory_id: str) -> CuratedMemory | None:
        row = self._one(
            "SELECT payload, version, last_accessed_at FROM memories "
            "WHERE namespace = %s AND id = %s",
            (self.namespace, memory_id),
        )
        if row is None:
            return None
        return self._governed(row)

    def list_curated(self, *, active_only: bool = False) -> list[CuratedMemory]:
        sql = (
            "SELECT payload, version, last_accessed_at FROM memories "
            "WHERE namespace = %s"
        )
        params: list[Any] = [self.namespace]
        if active_only:
            sql += f" AND trust NOT IN ({self._marks(_RETIRED_TRUSTS)})"
            params.extend(_RETIRED_TRUSTS)
        sql += " ORDER BY updated_at DESC, id"
        rows, _ = self._run(sql, params)
        memories = [
            item
            for item in (self._governed(row) for row in rows)
            if item is not None
        ]
        return sorted(memories, key=lambda item: item.updated_at, reverse=True)

    @staticmethod
    def _governed(row: Any) -> CuratedMemory | None:
        """Rebuild a record and stamp the two store-owned governance fields.

        Identical to the SQLite adapter's, on purpose: ``version`` and
        ``last_accessed_at`` are columns rather than payload keys, so a reader
        that returned the payload alone would hand back a record disagreeing with
        the audit trail next to it.
        """
        memory = validate_payload(CuratedMemory, row[0])
        if memory is None:
            return None
        return memory.model_copy(
            update={
                "version": optional_int(row[1]),
                "last_accessed_at": parse_stamp(row[2]),
            }
        )

    def remove_curated(self, memory_id: str) -> bool:
        """Delete a memory and its evidence rows, keeping its version history.

        ``memory_versions`` and ``memory_links`` survive on purpose: section 7
        requires every trust transition to stay auditable, and deleting the history
        of a retired memory is how a self-contamination incident becomes
        unexplainable after the fact.
        """
        with self._tx():
            self._run(
                "DELETE FROM memories_fts WHERE namespace = %s AND memory_id = %s",
                (self.namespace, memory_id),
            )
            self._run(
                "DELETE FROM memories WHERE namespace = %s AND id = %s",
                (self.namespace, memory_id),
            )
            self._run(
                "DELETE FROM memory_sources WHERE namespace = %s AND memory_id = %s",
                (self.namespace, memory_id),
            )
            self.bump_epoch()
        return True

    # -------------------------------------------------------------- sessions
    def write_session(self, session: SessionMemory) -> None:
        """Persist a session, with messages normalised into their own table.

        Messages live in ``session_messages`` and *not* in ``payload``; the session
        dump is stored with the key removed. Keeping them in one place only would
        be simpler but would give two answers to "what did this session say" as
        soon as summarisation starts appending turns.
        """
        self._put_session(session.model_copy(update={"updated_at": utc_now()}))

    def _put_session(self, session: SessionMemory) -> None:
        """Shared session writer.

        The only thing separating :meth:`write_session` from
        :meth:`import_session` is who decided ``updated_at``.
        """
        data = session.model_dump(mode="json")
        messages = data.pop("messages", [])
        with self._tx():
            self._run(
                """
                INSERT INTO sessions (
                  namespace, id, title, topic, summary, agent_id, user_id,
                  project_id, scenario_type, device, build, status, started_at,
                  ended_at, updated_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s)
                ON CONFLICT (namespace, id) DO UPDATE SET
                  title = excluded.title,
                  topic = excluded.topic,
                  summary = excluded.summary,
                  agent_id = excluded.agent_id,
                  user_id = excluded.user_id,
                  project_id = excluded.project_id,
                  scenario_type = excluded.scenario_type,
                  device = excluded.device,
                  build = excluded.build,
                  status = excluded.status,
                  updated_at = excluded.updated_at,
                  payload = excluded.payload
                """,
                (
                    self.namespace,
                    session.session_id,
                    session.title,
                    session.topic,
                    session.summary,
                    session.agent_id,
                    session.user_id,
                    session.project_id,
                    session.scenario_type,
                    session.device,
                    session.build,
                    session.status,
                    iso_stamp(session.created_at) or "",
                    None,
                    iso_stamp(session.updated_at) or "",
                    dumps_json(data),
                ),
            )
            self._run(
                "DELETE FROM session_messages WHERE namespace = %s "
                "AND session_id = %s",
                (self.namespace, session.session_id),
            )
            for message in messages:
                self._run(
                    'INSERT INTO session_messages (namespace, session_id, role, '
                    'content, "references", created_at) '
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        self.namespace,
                        session.session_id,
                        str(message.get("role") or "user"),
                        str(message.get("content") or ""),
                        dumps_json(message.get("references") or []),
                        str(message.get("created_at") or ""),
                    ),
                )
            self.bump_epoch()

    def load_session(self, session_id: str) -> SessionMemory | None:
        row = self._one(
            "SELECT payload FROM sessions WHERE namespace = %s AND id = %s",
            (self.namespace, session_id),
        )
        if row is None:
            return None
        return self._assemble_session(row[0], session_id)

    def list_sessions(self) -> list[SessionMemory]:
        rows, _ = self._run(
            "SELECT id, payload FROM sessions WHERE namespace = %s "
            "ORDER BY updated_at DESC, id",
            (self.namespace,),
        )
        sessions = [
            item
            for item in (
                self._assemble_session(row[1], str(row[0])) for row in rows
            )
            if item is not None
        ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def _assemble_session(self, payload: Any, session_id: str) -> SessionMemory | None:
        """Rejoin a session row with its messages."""
        try:
            data = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        rows, _ = self._run(
            'SELECT role, content, "references", created_at FROM session_messages '
            "WHERE namespace = %s AND session_id = %s ORDER BY seq",
            (self.namespace, session_id),
        )
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                references = json.loads(row[2] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                references = []
            messages.append(
                {
                    "role": row[0],
                    "content": row[1],
                    "references": references,
                    "created_at": row[3],
                }
            )
        data["messages"] = messages
        try:
            return SessionMemory.model_validate(data)
        except ValueError:
            return None

    # ------------------------------------------------- file-backed sidecars
    @property
    def dreams_path(self) -> Path:
        return self._files.dreams_path

    def append_dream_job(self, payload: dict[str, object]) -> None:
        self._files.append_dream_job(payload)

    def read_dream_jobs(self, *, limit: int = 100) -> list[dict[str, object]]:
        return self._files.read_dream_jobs(limit=limit)

    def clear_dream_jobs(self) -> None:
        self._files.clear_dream_jobs()

    @staticmethod
    def execution_snapshot(output_dir: str | Path) -> ExecutionCheckpointSnapshot:
        """Project a run directory; it is not corpus state, so nothing is stored."""
        return JsonMemoryBackend.execution_snapshot(output_dir)

    # ------------------------------------------------------ indexed retrieval
    def search(self, query: MemoryQuery) -> list[ScoredMemory]:
        """Candidate pool from the tsvector index, filtered in SQL.

        The PostgreSQL twin of ``SqliteStore.search``, and it has to answer the
        same question with the same permissions: identical trust, type and
        scenario predicates, and the same "more negative lexical rank is better"
        convention, which is why ``ts_rank`` (higher is better, always positive)
        is negated into the ``bm25`` field. A store switch may change latency; it
        may not change an answer.

        An empty pool for a tokenless query is correct rather than lossy, for the
        reason the SQLite adapter documents: such a query scores ``0.0`` lexical
        relevance, below the floor, so a scan would return nothing either.
        """
        match = pg_schema.tsquery_match(query.text)
        if match is None:
            return []
        namespace = query.namespace or self.namespace
        pool: list[ScoredMemory] = []
        pool.extend(self._search_memories(match, query, namespace))
        if query.include_episodic:
            pool.extend(self._search_episodes(match, query, namespace))
        pool.sort(key=lambda item: item.bm25 if item.bm25 is not None else 0.0)
        return pool[: max(1, query.limit)]

    def _search_memories(
        self, match: str, query: MemoryQuery, namespace: str
    ) -> list[ScoredMemory]:
        statuses = sorted(status.value for status in query.statuses)
        if not statuses:
            return []
        clauses = ["f.tsv @@ to_tsquery('simple', %s)", "f.namespace = %s"]
        params: list[Any] = [match, namespace]
        clauses.append(f"m.trust IN ({self._marks(statuses)})")
        params.extend(statuses)
        if query.types:
            types = sorted(item.value for item in query.types)
            clauses.append(f"m.type IN ({self._marks(types)})")
            params.extend(types)
        # A memory with no scenario_type is scenario-agnostic and may match any
        # query, so a NULL scope must survive the filter - the same rule the
        # SQLite adapter encodes with ``json_extract(...) IS NULL``.
        clauses.append(
            "(%s::text IS NULL OR m.scope->>'scenario_type' IS NULL "
            "OR m.scope->>'scenario_type' = %s)"
        )
        params.extend([query.scenario_type, query.scenario_type])
        params.append(max(1, query.limit))
        sql = (
            "SELECT m.payload AS payload, "
            "-ts_rank(f.tsv, to_tsquery('simple', %s)) AS rank "
            "FROM memories_fts f JOIN memories m "
            "ON m.namespace = f.namespace AND m.id = f.memory_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT %s"
        )
        return self._scored_rows(sql, (match, *params), CuratedMemory)

    def _search_episodes(
        self, match: str, query: MemoryQuery, namespace: str
    ) -> list[ScoredMemory]:
        clauses = ["f.tsv @@ to_tsquery('simple', %s)", "f.namespace = %s"]
        params: list[Any] = [match, namespace]
        # Episodes always carry a scenario_type, so unlike memories the filter is
        # strict - matching the scan path's ``episode.scenario_type != x``.
        clauses.append("(%s::text IS NULL OR e.scenario_type = %s)")
        params.extend([query.scenario_type, query.scenario_type])
        params.append(max(1, query.limit))
        sql = (
            "SELECT e.payload AS payload, "
            "-ts_rank(f.tsv, to_tsquery('simple', %s)) AS rank "
            "FROM episodes_fts f JOIN episodes e "
            "ON e.namespace = f.namespace AND e.id = f.episode_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT %s"
        )
        return self._scored_rows(sql, (match, *params), EpisodicMemory)

    def _scored_rows(
        self, sql: str, params: Sequence[Any], model_class: type[Any]
    ) -> list[ScoredMemory]:
        """Run a ``(payload, rank)`` query into scored candidates.

        Rows are tuples here rather than mappings, because the intersection of
        psycopg 2 and 3 that this adapter targets guarantees positional access
        and nothing more. Corrupt payloads are skipped, never raised: one bad row
        must not empty recall.
        """
        rows, _ = self._run(sql, params)
        out: list[ScoredMemory] = []
        for row in rows:
            record = validate_payload(model_class, row[0])
            if record is None:
                continue
            try:
                rank = float(row[1])
            except (TypeError, ValueError, IndexError):
                rank = 0.0
            out.append(ScoredMemory(memory=record, bm25=rank))
        return out

    # ------------------------------------------------------------ governance
    def add_source(
        self,
        memory_id: str,
        *,
        run_id: str,
        episode_id: str = "",
    ) -> bool:
        """Record a run as evidence for a memory. ``True`` only if it is new.

        ``ON CONFLICT DO NOTHING`` plus ``rowcount`` is the whole mechanism the
        section-1.2 defect-A fix rests on: the same run consolidated twice inserts
        one row and reports ``False``, so corroboration cannot be manufactured by
        repetition - which is exactly what the double consolidation in
        ``analyze.py`` used to do.
        """
        if not run_id:
            return False
        with self._tx():
            _, count = self._run(
                "INSERT INTO memory_sources "
                "(namespace, memory_id, run_id, episode_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    self.namespace,
                    memory_id,
                    run_id,
                    episode_id,
                    iso_stamp(utc_now()) or "",
                ),
            )
            inserted = count > 0
            self._refresh_distinct_runs(memory_id)
        return inserted

    def _refresh_distinct_runs(self, memory_id: str) -> None:
        """Overwrite the counter from a COUNT of evidence rows.

        Derived, never incremented: a column that is bumped by writers drifts the
        first time one of them retries, and this is the number the quality gate
        promotes on.
        """
        self._run(
            "UPDATE memories SET distinct_runs = ("
            "  SELECT COUNT(*) FROM memory_sources "
            "  WHERE namespace = %s AND memory_id = %s"
            ") WHERE namespace = %s AND id = %s",
            (self.namespace, memory_id, self.namespace, memory_id),
        )

    def distinct_runs(self, memory_id: str) -> int:
        """Independent runs behind a memory, read from the evidence table."""
        row = self._one(
            "SELECT COUNT(*) FROM memory_sources "
            "WHERE namespace = %s AND memory_id = %s",
            (self.namespace, memory_id),
        )
        return int(row[0]) if row is not None else 0

    def set_trust(
        self,
        memory_id: str,
        trust: MemoryTrust,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> bool:
        """Change one memory's trust level, leaving an auditable version behind.

        Section 7 makes this the *only* legitimate way to move between trust
        levels. Routing through :meth:`_put_curated` gives the audit trail for
        free: the payload is rewritten so reads see the new status, and a
        ``memory_versions`` row captures the transition with reason and actor.
        """
        memory = self.load_curated(memory_id)
        if memory is None:
            return False
        updated = memory.model_copy(
            update={
                "status": trust,
                "status_reason": reason or None,
                "occurrences": memory.distinct_runs,
            }
        )
        self._put_curated(updated, reason=reason or "trust", actor=actor)
        return True

    def revise(
        self,
        memory: CuratedMemory,
        *,
        reason: str,
        actor: str | None = None,
    ) -> bool:
        """Write a governance edit under its own audit reason.

        Halving a memory's confidence because another run contradicted it is a
        substantive change; filing it as ``"consolidate"`` would make the audit
        trail claim the corpus agreed with itself at the moment it stopped doing
        so.
        """
        self._put_curated(memory, reason=reason or "revise", actor=actor)
        return True

    def link(
        self,
        left: str,
        right: str,
        relation: str,
        *,
        evidence: str | None = None,
    ) -> None:
        """Assert a typed relation between two memories.

        ``memory_links`` is the source of truth; ``memories.contradictions`` is a
        denormalised counter refreshed from it in the same transaction, so the
        dashboard can read the number without a join and the two cannot disagree.
        Re-asserting an edge updates its evidence rather than duplicating it: the
        primary key is the identity of a claim, and a claim restated with better
        evidence should keep the better evidence.
        """
        with self._tx():
            self._run(
                "INSERT INTO memory_links "
                "(namespace, memory_id, related_memory_id, relation, evidence, "
                "created_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (namespace, memory_id, related_memory_id, relation) "
                "DO UPDATE SET evidence = excluded.evidence",
                (
                    self.namespace,
                    left,
                    right,
                    relation,
                    evidence,
                    iso_stamp(utc_now()) or "",
                ),
            )
            if relation == _CONTRADICTS:
                self._refresh_contradictions(left)
            self.bump_epoch()

    def _refresh_contradictions(self, memory_id: str) -> None:
        self._run(
            "UPDATE memories SET contradictions = ("
            "  SELECT COUNT(*) FROM memory_links WHERE namespace = %s "
            "  AND memory_id = %s AND relation = %s"
            ") WHERE namespace = %s AND id = %s",
            (
                self.namespace,
                memory_id,
                _CONTRADICTS,
                self.namespace,
                memory_id,
            ),
        )

    def count_links(self, memory_id: str, relation: str | None = None) -> int:
        """How many edges of one kind point out of a memory.

        Counting links rather than trusting a counter column means the gate's
        verdict is reproducible from the audit trail alone.
        """
        sql = "SELECT COUNT(*) FROM memory_links WHERE namespace = %s AND memory_id = %s"
        params: list[Any] = [self.namespace, memory_id]
        if relation is not None:
            sql += " AND relation = %s"
            params.append(relation)
        row = self._one(sql, params)
        return int(row[0]) if row is not None else 0

    def linked_ids(self, memory_id: str, relation: str | None = None) -> list[str]:
        """Neighbours reachable from one memory over one relation kind.

        ``count_links`` answers "how many refutations"; this answers "which
        records corroborate me", which the gate needs in order to read the other
        side's run ids and exclude evidence that is not actually independent.
        """
        sql = (
            "SELECT related_memory_id FROM memory_links "
            "WHERE namespace = %s AND memory_id = %s"
        )
        params: list[Any] = [self.namespace, memory_id]
        if relation is not None:
            sql += " AND relation = %s"
            params.append(relation)
        sql += " ORDER BY related_memory_id"
        rows, _ = self._run(sql, params)
        return [str(row[0]) for row in rows]

    def list_links(
        self, relation: str | None = None, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Edges in this namespace, newest first, for the dashboard and audits."""
        sql = (
            "SELECT memory_id, related_memory_id, relation, evidence, created_at "
            "FROM memory_links WHERE namespace = %s"
        )
        params: list[Any] = [self.namespace]
        if relation is not None:
            sql += " AND relation = %s"
            params.append(relation)
        sql += " ORDER BY created_at DESC, memory_id LIMIT %s"
        params.append(max(1, int(limit)))
        rows, _ = self._run(sql, params)
        return [dict(zip(_LINK_ORDER, row)) for row in rows]

    def recent_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Latest trust/content transitions, newest first.

        Section 7 requires every transition to be recorded; this is what makes
        that requirement checkable instead of assumed.

        The key set matches the SQLite adapter and the file adapter's
        ``versions.jsonl``, because this is what ``memory migrate`` exports: two
        adapters returning different audit shapes would make a migration
        silently lossy in one direction.
        """
        rows, _ = self._run(
            "SELECT memory_id, version, topic, content, from_trust, trust, "
            "confidence, reason, actor, "
            "created_at FROM memory_versions WHERE namespace = %s "
            "ORDER BY seq DESC LIMIT %s",
            (self.namespace, max(1, int(limit))),
        )
        return [dict(zip(_VERSION_ORDER, row)) for row in rows]

    def import_governance(
        self,
        versions: list[dict[str, Any]],
        links: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Load a migration bundle's audit trail and edges; returns what was added.

        Semantics match the SQLite adapter exactly, because ``memory migrate``
        treats the two SQL stores as interchangeable targets: conflicts are
        ignored so a re-run adds nothing, a missing ``version`` is derived from
        the memory's own maximum so ``UNIQUE(namespace, memory_id, version)``
        stays satisfiable, and counts come from a before/after ``COUNT(*)``
        rather than from rowcounts. The derivation goes through
        :class:`~trace_agent.memory.store.rows.VersionResolver` for the same
        reason it does on SQLite: a plain ``MAX(version) + 1`` is not stable
        across replays, so a bundle of ordinal-less sidecar rows would grow the
        audit trail every time the migration was re-run.
        """
        with self._tx():
            versions_sql = (
                "SELECT COUNT(*) FROM memory_versions WHERE namespace = %s"
            )
            links_sql = "SELECT COUNT(*) FROM memory_links WHERE namespace = %s"
            before_versions = self._count(versions_sql)
            before_links = self._count(links_sql)
            now = iso_stamp(utc_now()) or ""
            existing, _ = self._run(
                "SELECT memory_id, version, trust, reason, created_at "
                "FROM memory_versions WHERE namespace = %s",
                (self.namespace,),
            )
            resolver = VersionResolver(existing)
            for record in versions:
                memory_id = str(record.get("memory_id") or "")
                if not memory_id:
                    continue
                trust = str(record.get("trust") or MemoryStatus.CANDIDATE.value)
                reason = str(record.get("reason") or "")
                created_at = str(record.get("created_at") or now)
                version = resolver.ordinal(
                    memory_id=memory_id,
                    version=record.get("version"),
                    trust=trust,
                    reason=reason,
                    created_at=created_at,
                )
                self._run(
                    "INSERT INTO memory_versions (namespace, memory_id, version, "
                    "topic, content, from_trust, trust, confidence, reason, "
                    "actor, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (namespace, memory_id, version) DO NOTHING",
                    (
                        self.namespace,
                        memory_id,
                        version,
                        str(record.get("topic") or ""),
                        str(record.get("content") or ""),
                        record.get("from_trust"),
                        trust,
                        as_float(record.get("confidence")),
                        reason,
                        record.get("actor"),
                        created_at,
                    ),
                )
            for record in links:
                memory_id = str(record.get("memory_id") or "")
                related = str(record.get("related_memory_id") or "")
                relation = str(record.get("relation") or "")
                if not (memory_id and related and relation):
                    continue
                self._run(
                    "INSERT INTO memory_links (namespace, memory_id, "
                    "related_memory_id, relation, evidence, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (namespace, memory_id, related_memory_id, "
                    "relation) DO NOTHING",
                    (
                        self.namespace,
                        memory_id,
                        related,
                        relation,
                        record.get("evidence"),
                        str(record.get("created_at") or now),
                    ),
                )
            added_versions = self._count(versions_sql) - before_versions
            added_links = self._count(links_sql) - before_links
            if added_versions or added_links:
                # Governance feeds the gate's corroboration and contradiction
                # counts, so a trust-filtered recall can change without a single
                # record being touched.
                self.bump_epoch()
        return added_versions, added_links

    def _count(self, sql: str) -> int:
        """Scalar ``COUNT(*)`` read, tolerant of a driver that returns nothing."""
        row = self._one(sql, (self.namespace,))
        if not row:
            return 0
        return optional_int(row[0]) or 0

    def touch(self, memory_id: str) -> None:
        """Stamp ``last_accessed_at`` without touching the stored record.

        Access time is store-owned metadata: it must not rewrite ``payload`` or
        ``updated_at``, because recalling a memory should not make it look newly
        corroborated and push it up the freshness ranking. It also does not bump
        the corpus epoch - a read that invalidated every cached answer would make
        the cache useless on a dashboard that recalls and then touches.
        """
        self._run(
            "UPDATE memories SET last_accessed_at = %s "
            "WHERE namespace = %s AND id = %s",
            (iso_stamp(utc_now()), self.namespace, memory_id),
        )

    def expire(self, now: datetime | None = None) -> int:
        """Archive memories past ``expires_at``; returns how many were retired.

        Archiving rather than deleting keeps section 7's rule that retired memory
        stays auditable, and it is the retention half of the lifecycle: a corpus
        that can only grow eventually drowns recall in priors nobody has believed
        in for a year.
        """
        reference = iso_stamp(now or utc_now()) or ""
        rows, _ = self._run(
            "SELECT payload FROM memories WHERE namespace = %s "
            "AND expires_at IS NOT NULL AND expires_at <= %s AND trust <> %s",
            (self.namespace, reference, MemoryStatus.ARCHIVED.value),
        )
        expired = 0
        for row in rows:
            memory = validate_payload(CuratedMemory, row[0])
            if memory is None:
                continue
            retired = memory.model_copy(
                update={
                    "status": MemoryStatus.ARCHIVED,
                    "archived_reason": "expired",
                    "status_reason": "retention: past expires_at",
                }
            )
            self._put_curated(retired, reason="expire")
            expired += 1
        return expired

    def stats(self) -> MemoryStats:
        """Corpus shape for ``memory stats``, the dashboard and migration checks."""
        namespace = self.namespace

        def count(sql: str, *extra: Any) -> int:
            row = self._one(sql, (namespace, *extra))
            return int(row[0]) if row is not None else 0

        def grouped(column: str) -> dict[str, int]:
            rows, _ = self._run(
                f"SELECT {column} AS bucket, COUNT(*) AS total FROM memories "
                "WHERE namespace = %s GROUP BY bucket",
                (namespace,),
            )
            return {str(row[0]): int(row[1]) for row in rows}

        return MemoryStats(
            store="postgres",
            namespace=namespace,
            memories=count("SELECT COUNT(*) FROM memories WHERE namespace = %s"),
            episodes=count("SELECT COUNT(*) FROM episodes WHERE namespace = %s"),
            sessions=count("SELECT COUNT(*) FROM sessions WHERE namespace = %s"),
            by_trust=grouped("trust"),
            by_type=grouped("type"),
            by_kind=grouped("kind"),
            contradictions=count(
                "SELECT COUNT(*) FROM memory_links "
                "WHERE namespace = %s AND relation = %s",
                _CONTRADICTS,
            ),
            duplicates=count(
                "SELECT COUNT(*) FROM memory_links "
                "WHERE namespace = %s AND relation = %s",
                _DUPLICATES,
            ),
            versions=count(
                "SELECT COUNT(*) FROM memory_versions WHERE namespace = %s"
            ),
            links=count("SELECT COUNT(*) FROM memory_links WHERE namespace = %s"),
            expired=count(
                "SELECT COUNT(*) FROM memories "
                "WHERE namespace = %s AND archived_reason = 'expired'"
            ),
            superseded=count(
                "SELECT COUNT(*) FROM memories WHERE namespace = %s AND trust = %s",
                MemoryStatus.SUPERSEDED.value,
            ),
        )

    def reindex(self) -> int:
        """Rebuild both tsvector indexes from stored records; returns rows indexed.

        The body is recomputed from the loaded model through
        :mod:`trace_agent.memory.search_text`, not copied out of the old index, so
        this is also the recovery path after the tokenizer or the indexed field
        set changes. The generated ``tsv`` column rebuilds itself from ``body``,
        which is the one place PostgreSQL does work SQLite's FTS5 needs a manual
        ``optimize`` for.
        """
        indexed = 0
        with self._tx():
            self._run(
                "DELETE FROM memories_fts WHERE namespace = %s", (self.namespace,)
            )
            self._run(
                "DELETE FROM episodes_fts WHERE namespace = %s", (self.namespace,)
            )
            rows, _ = self._run(
                "SELECT payload FROM memories WHERE namespace = %s",
                (self.namespace,),
            )
            for row in rows:
                memory = validate_payload(CuratedMemory, row[0])
                if memory is None:
                    continue
                self._index_memory(memory)
                indexed += 1
            rows, _ = self._run(
                "SELECT payload FROM episodes WHERE namespace = %s",
                (self.namespace,),
            )
            for row in rows:
                episode = validate_payload(EpisodicMemory, row[0])
                if episode is None:
                    continue
                self._index_episode(episode)
                indexed += 1
            self.bump_epoch()
        return indexed

    # ------------------------------------------------- semantic vectors (P3)
    # Same six optional port members as the SQLite adapter, with one difference:
    # when pgvector is installed the nearest-neighbour ordering is pushed into the
    # server (``embedding <=> probe``), which is the only reason to be on
    # PostgreSQL for vectors at all. When it is not - no extension, no superuser -
    # the column is BYTEA and the cosine is computed in Python over rows SQL has
    # already filtered, i.e. exactly what SQLite does. Neither path may raise: a
    # database whose additive migration failed costs semantic recall, not the run.

    def pending_embeddings(
        self, *, model: str, limit: int = 32
    ) -> list[PendingEmbedding]:
        """Records needing a vector: unembedded, or embedded by another recipe.

        ``embedding_model <> %s`` is what makes a model change a rebuild. The
        store cannot tell whether two recipes are comparable and must not guess: a
        stale vector mixed into a new corpus produces cosines that look plausible
        and mean nothing. Curated and episodic rows are interleaved rather than
        concatenated, so one table cannot starve the other.
        """
        budget = max(1, int(limit))
        memories = self._pending_rows(
            "memories",
            model,
            budget,
            CuratedMemory,
            curated_index_text,
            order="updated_at DESC",
            extra=f"AND trust NOT IN ({self._marks(_RETIRED_TRUSTS)}) ",
            extra_params=_RETIRED_TRUSTS,
        )
        episodes = self._pending_rows(
            "episodes",
            model,
            budget,
            EpisodicMemory,
            episode_index_text,
            order="created_at DESC",
        )
        out: list[PendingEmbedding] = []
        for pair in zip(memories, episodes, strict=False):
            out.extend(pair)
        longer, shorter = (
            (memories, episodes)
            if len(memories) >= len(episodes)
            else (episodes, memories)
        )
        out.extend(longer[len(shorter) :])
        return out[:budget]

    def _pending_rows(
        self,
        table: str,
        model: str,
        limit: int,
        model_class: type[Any],
        index_text: Any,
        *,
        order: str,
        extra: str = "",
        extra_params: Any = (),
    ) -> list[PendingEmbedding]:
        """One table's outstanding embedding work, newest first.

        Newest first because a fresh run is the record most likely to be recalled
        before the next beat, so it is also the one whose missing vector costs the
        most.
        """
        sql = (
            f"SELECT id, payload, embedding_model FROM {table} "
            "WHERE namespace = %s AND (embedding IS NULL "
            "OR embedding_model IS NULL OR embedding_model <> %s) "
            f"{extra}ORDER BY {order} LIMIT %s"
        )
        try:
            rows, _ = self._run(
                sql, (self.namespace, model, *extra_params, limit)
            )
        except Exception:
            # No driver-specific error class is importable here - the driver
            # itself is optional - so this is the one place the adapter catches
            # broadly. Backfill is a dreaming-beat job; a failure means "no
            # vectors this tick".
            return []
        out: list[PendingEmbedding] = []
        for row in rows:
            record = validate_payload(model_class, row[1])
            if record is None:
                continue
            text = str(index_text(record) or "").strip()
            if not text:
                continue
            out.append(
                PendingEmbedding(
                    record_id=str(row[0]),
                    text=text,
                    episode=table == "episodes",
                    stored_model=str(row[2]) if row[2] else None,
                )
            )
        return out

    def set_embedding(
        self,
        record_id: str,
        vector: Any,
        *,
        model: str,
        episode: bool = False,
    ) -> bool:
        """Store one vector; ``True`` only if a row existed to update.

        With pgvector the value goes in as a cast text literal, without it as the
        same float32 blob the SQLite adapter writes - so a cosine computed against
        either corpus agrees, and ``memory migrate`` between them carries vectors
        across unchanged.
        """
        values = [float(item) for item in (vector or ())]
        if not values or not record_id:
            return False
        table = "episodes" if episode else "memories"
        if self._pgvector:
            placeholder = "%s::vector"
            value: Any = pg_schema.pgvector_literal(values)
        else:
            placeholder = "%s"
            try:
                value = pack_vector(values)
            except (OverflowError, TypeError, ValueError):
                return False
        with self._tx():
            try:
                _, count = self._run(
                    f"UPDATE {table} SET embedding = {placeholder}, "
                    "embedding_model = %s WHERE namespace = %s AND id = %s",
                    (value, model, self.namespace, record_id),
                )
            except Exception:
                # Wrong width for a bounded column, or a server that went away:
                # this record stays unembedded and the next beat retries.
                return False
            return count > 0

    def vectors_for(
        self, record_ids: Any, *, episode: bool = False
    ) -> dict[str, list[float]]:
        """Stored vectors for a set of ids; ids without one are absent."""
        ids = [str(item) for item in (record_ids or ()) if item]
        if not ids:
            return {}
        table = "episodes" if episode else "memories"
        column = "embedding::text" if self._pgvector else "embedding"
        try:
            rows, _ = self._run(
                f"SELECT id, {column} FROM {table} "
                f"WHERE namespace = %s AND id IN ({self._marks(ids)})",
                (self.namespace, *ids),
            )
        except Exception:
            return {}
        out: dict[str, list[float]] = {}
        for row in rows:
            vector = (
                pg_schema.parse_pgvector_literal(row[1])
                if self._pgvector
                else unpack_vector(row[1])
            )
            if vector:
                out[str(row[0])] = vector
        return out

    def vector_search(
        self, vector: Any, query: MemoryQuery
    ) -> list[ScoredMemory]:
        """Nearest records to ``vector``, under the query's own filters.

        The filters are the *same* predicates the lexical path uses, minus the
        tsvector match: a vector channel that ignored ``trust`` would hand recall
        candidates the reranker is not allowed to return, and one that ignored
        ``scenario_type`` would leak a cold-start pattern into a frame-drop query.

        ``bm25`` is left ``None`` on these rows. A cosine and a lexical rank are
        not comparable, and filling the field with one would let a caller sort a
        merged pool by a number that means two different things.
        """
        probe = [float(item) for item in (vector or ())]
        if not probe:
            return []
        namespace = query.namespace or self.namespace
        pool = self._pushdown_search(probe, query, namespace)
        if pool is None:
            pool = []
            pool.extend(
                self._vector_pool(
                    self._scan_vector_rows("memories", query, namespace),
                    CuratedMemory,
                    probe,
                )
            )
            if query.include_episodic:
                pool.extend(
                    self._vector_pool(
                        self._scan_vector_rows("episodes", query, namespace),
                        EpisodicMemory,
                        probe,
                    )
                )
        pool.sort(
            key=lambda item: (
                -(item.similarity or 0.0),
                str(
                    getattr(item.memory, "memory_id", None)
                    or getattr(item.memory, "episode_id", "")
                    or ""
                ),
            )
        )
        return pool[: max(1, query.limit)]

    def _pushdown_search(
        self, probe: list[float], query: MemoryQuery, namespace: str
    ) -> list[ScoredMemory] | None:
        """Let pgvector order the pool, or ``None`` when it cannot.

        Returns ``None`` - never a partial pool - whenever the server rejects the
        operator. The realistic cause is a mixed-width corpus: a recipe change
        leaves old rows at the old dimension until the backfill finishes, and
        ``<=>`` between different widths is an error rather than a zero. Falling
        back to the exact Python scan is then not just safer but *more* correct,
        because :func:`cosine` scores a width mismatch as 0.0 instead of failing.
        """
        if not self._pgvector:
            return None
        literal = pg_schema.pgvector_literal(probe)
        limit = max(1, query.limit)
        try:
            pool: list[ScoredMemory] = []
            for table, alias, model_class in (
                ("memories", "m", CuratedMemory),
                *(
                    [("episodes", "e", EpisodicMemory)]
                    if query.include_episodic
                    else []
                ),
            ):
                filters = self._vector_filters(table, query, namespace)
                if filters is None:
                    continue
                clauses, params = filters
                sql = (
                    f"SELECT {alias}.payload AS payload, "
                    f"1 - ({alias}.embedding <=> %s::vector) AS similarity "
                    f"FROM {table} {alias} WHERE {' AND '.join(clauses)} "
                    f"ORDER BY {alias}.embedding <=> %s::vector LIMIT %s"
                )
                rows, _ = self._run(sql, (literal, *params, literal, limit))
                for row in rows:
                    record = validate_payload(model_class, row[0])
                    if record is None:
                        continue
                    try:
                        value = float(row[1])
                    except (TypeError, ValueError, IndexError):
                        value = 0.0
                    pool.append(
                        ScoredMemory(
                            memory=record,
                            bm25=None,
                            # Clamped to ``[0, 1]`` to match ``cosine``: cosine
                            # distance can exceed 1, and a negative similarity
                            # would let an unrelated record subtract itself out of
                            # the reranker's weighted sum.
                            similarity=min(1.0, max(0.0, value)),
                        )
                    )
            return pool
        except Exception:
            return None

    def _vector_filters(
        self, table: str, query: MemoryQuery, namespace: str
    ) -> tuple[list[str], list[Any]] | None:
        """Shared WHERE clause for the vector channel; ``None`` if nothing qualifies.

        One builder for both the pushdown and the scan, because the two must admit
        the same records or a query's answer would depend on whether pgvector
        happened to be installed.
        """
        alias = "m" if table == "memories" else "e"
        clauses = [f"{alias}.namespace = %s", f"{alias}.embedding IS NOT NULL"]
        params: list[Any] = [namespace]
        if table == "memories":
            statuses = sorted(status.value for status in query.statuses)
            if not statuses:
                return None
            clauses.append(f"{alias}.trust IN ({self._marks(statuses)})")
            params.extend(statuses)
            if query.types:
                types = sorted(item.value for item in query.types)
                clauses.append(f"{alias}.type IN ({self._marks(types)})")
                params.extend(types)
            # NULL scope means scenario-agnostic, exactly as in the lexical path.
            clauses.append(
                f"(%s::text IS NULL OR {alias}.scope->>'scenario_type' IS NULL "
                f"OR {alias}.scope->>'scenario_type' = %s)"
            )
            params.extend([query.scenario_type, query.scenario_type])
        else:
            # Episodes always carry a scenario_type, so the filter is strict.
            clauses.append(f"(%s::text IS NULL OR {alias}.scenario_type = %s)")
            params.extend([query.scenario_type, query.scenario_type])
        return clauses, params

    def _scan_vector_rows(
        self, table: str, query: MemoryQuery, namespace: str
    ) -> list[Any]:
        """Exact-scan rows ``(payload, embedding)`` for the Python cosine path."""
        filters = self._vector_filters(table, query, namespace)
        if filters is None:
            return []
        clauses, params = filters
        alias = "m" if table == "memories" else "e"
        column = f"{alias}.embedding::text" if self._pgvector else f"{alias}.embedding"
        sql = (
            f"SELECT {alias}.payload AS payload, {column} AS embedding "
            f"FROM {table} {alias} WHERE {' AND '.join(clauses)}"
        )
        try:
            rows, _ = self._run(sql, params)
            return rows
        except Exception:
            return []

    def _vector_pool(
        self, rows: list[Any], model_class: type[Any], probe: list[float]
    ) -> list[ScoredMemory]:
        out: list[ScoredMemory] = []
        for row in rows:
            record = validate_payload(model_class, row[0])
            if record is None:
                continue
            stored = (
                pg_schema.parse_pgvector_literal(row[1])
                if self._pgvector
                else unpack_vector(row[1])
            )
            if not stored:
                # Truncated or wrong-width value: treat as unembedded, which is
                # what the next backfill repairs.
                continue
            out.append(
                ScoredMemory(
                    memory=record, bm25=None, similarity=cosine(probe, stored)
                )
            )
        return out

    def prune_embeddings(self, *, keep_model: str) -> int:
        """Delete vectors not produced by ``keep_model``; returns rows cleared.

        ``keep_model=""`` matches nothing and therefore clears everything, which
        is how :meth:`SemanticIndex.rebuild` turns a recipe change into a fresh
        corpus instead of a mixture of incomparable vectors.
        """
        kept = keep_model or ""
        pruned = 0
        with self._tx():
            for table in ("memories", "episodes"):
                try:
                    _, count = self._run(
                        f"UPDATE {table} SET embedding = NULL, "
                        "embedding_model = NULL "
                        "WHERE namespace = %s AND embedding IS NOT NULL "
                        "AND (embedding_model IS NULL OR embedding_model <> %s)",
                        (self.namespace, kept),
                    )
                except Exception:
                    continue
                pruned += max(0, count)
        return pruned

    def embedding_coverage(self) -> dict[str, Any]:
        """What is embedded, with which recipe, and what is still outstanding."""

        def count(sql: str, *extra: Any) -> int:
            try:
                row = self._one(sql, (self.namespace, *extra))
            except Exception:
                return 0
            return int(row[0]) if row is not None else 0

        memories = count("SELECT COUNT(*) FROM memories WHERE namespace = %s")
        memories_embedded = count(
            "SELECT COUNT(*) FROM memories WHERE namespace = %s "
            "AND embedding IS NOT NULL"
        )
        episodes = count("SELECT COUNT(*) FROM episodes WHERE namespace = %s")
        episodes_embedded = count(
            "SELECT COUNT(*) FROM episodes WHERE namespace = %s "
            "AND embedding IS NOT NULL"
        )
        models: dict[str, int] = {}
        for table in ("memories", "episodes"):
            try:
                rows, _ = self._run(
                    f"SELECT embedding_model AS model, COUNT(*) AS total "
                    f"FROM {table} WHERE namespace = %s AND embedding IS NOT NULL "
                    "GROUP BY model",
                    (self.namespace,),
                )
            except Exception:
                continue
            for row in rows:
                key = str(row[0] or "")
                models[key] = models.get(key, 0) + int(row[1])
        return {
            "memories": memories,
            "memories_embedded": memories_embedded,
            "episodes": episodes,
            "episodes_embedded": episodes_embedded,
            "models": models,
            "unembedded": max(
                0,
                (memories - memories_embedded) + (episodes - episodes_embedded),
            ),
            "pgvector": self._pgvector,
        }

    def vector_index(self, dim: int) -> dict[str, Any]:
        """Fix the vector width and build an ANN index. Operator action, not recall.

        pgvector can only index a *bounded* ``vector(n)``, and the schema keeps the
        column unbounded on purpose so a configuration change to the embedding
        dimension cannot turn into a write error. This is the command that closes
        that gap once a corpus has settled on one recipe: it narrows the column and
        then creates an HNSW index over cosine distance, which is the operator the
        pushdown path uses.

        Narrowing fails loudly rather than silently if any stored vector has a
        different width - which is the correct outcome, because the alternative is
        a truncated corpus. The fix is ``memory embed --rebuild`` (or
        :meth:`prune_embeddings`) first, and the returned report says so.
        """
        report: dict[str, Any] = {
            "pgvector": self._pgvector,
            "dim": int(dim),
            "narrowed": [],
            "indexes": [],
            "errors": [],
        }
        if not self._pgvector:
            report["errors"].append(
                "pgvector is not installed; embeddings are stored as BYTEA and "
                "searched with an exact scan, which needs no index"
            )
            return report
        width = max(1, int(dim))
        with self._tx():
            for table in ("memories", "episodes"):
                try:
                    self._run(
                        f"ALTER TABLE {table} ALTER COLUMN embedding "
                        f"TYPE vector({width}) USING embedding::vector({width})"
                    )
                    report["narrowed"].append(table)
                except Exception as exc:
                    report["errors"].append(
                        f"{table}: cannot narrow embedding to vector({width}): {exc}"
                    )
            for table in report["narrowed"]:
                try:
                    self._run(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_embedding_hnsw "
                        f"ON {table} USING hnsw (embedding vector_cosine_ops)"
                    )
                    report["indexes"].append(table)
                except Exception as exc:
                    report["errors"].append(f"{table}: cannot build hnsw index: {exc}")
        return report

    # ---------------------------------------------------- embedding hygiene
    def _clear_embedding(self, table: str, record_id: str) -> None:
        """Retire a vector whose source text no longer matches the record.

        Clearing rather than recomputing: a write happens inside a run, and
        calling a provider there would put embedding latency on the hot path for a
        vector the next dreaming beat produces anyway. In between, the record is
        simply absent from the vector channel, which degrades to lexical recall
        for that one row and nothing more.
        """
        try:
            self._run(
                f"UPDATE {table} SET embedding = NULL, embedding_model = NULL "
                "WHERE namespace = %s AND id = %s",
                (self.namespace, record_id),
            )
        except Exception:
            return

    # ------------------------------------------------------- corpus epoch (P4)
    def corpus_epoch(self) -> int:
        """How many times this namespace's corpus has been written.

        One counter per namespace in ``store_meta``, read by the recall cache to
        build its key. It exists so invalidation is a property of the corpus
        rather than of every writer: nothing has to know which cached queries a
        consolidation affected, because a change in the epoch changes every key at
        once. Deliberately coarse - a cache miss is cheap, a stale prior in a
        prompt is not.

        Raises when the server cannot be reached. The caller is expected to treat
        that as "do not cache", not as "epoch 0": a down database and an empty one
        must not share a cache key.
        """
        row = self._one(
            "SELECT value FROM store_meta WHERE key = %s",
            (_epoch_key(self.namespace),),
        )
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def bump_epoch(self) -> None:
        """Advance the epoch. Called by every write path, inside its transaction.

        Inside the transaction matters: a write that rolls back must not
        invalidate anything, and a write that commits must invalidate everything,
        in the same atomic step as the data it changed.
        """
        self._run(
            "INSERT INTO store_meta (key, value) VALUES (%s, '1') "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = (store_meta.value::bigint + 1)::text",
            (_epoch_key(self.namespace),),
        )

    # ----------------------------------------------------- multi-tenancy (P4)
    def list_namespaces(self) -> list[dict[str, Any]]:
        """Every isolation domain in this database, with its record counts.

        Section 8's namespace rule is only enforceable if it is inspectable: an
        operator has to be able to see that ``eval:*`` domains exist, how big they
        got, and when they were last written, without querying the corpus they are
        supposed to be isolated from. Counts come from one pass per table over the
        namespace index rather than a join, because a namespace with episodes but no
        memories still has to appear.
        """
        rows, _ = self._run(
            "SELECT namespace, SUM(memories), SUM(episodes), SUM(sessions), "
            "MAX(last_updated) FROM ("
            "  SELECT namespace, COUNT(*) AS memories, 0 AS episodes, "
            "    0 AS sessions, MAX(updated_at) AS last_updated "
            "    FROM memories GROUP BY namespace"
            "  UNION ALL"
            "  SELECT namespace, 0, COUNT(*), 0, MAX(created_at) "
            "    FROM episodes GROUP BY namespace"
            "  UNION ALL"
            "  SELECT namespace, 0, 0, COUNT(*), MAX(updated_at) "
            "    FROM sessions GROUP BY namespace"
            ") AS tallies GROUP BY namespace ORDER BY namespace"
        )
        return [
            {
                "namespace": str(row[0]),
                "memories": int(row[1] or 0),
                "episodes": int(row[2] or 0),
                "sessions": int(row[3] or 0),
                "last_updated": str(row[4] or ""),
                "current": str(row[0]) == self.namespace,
            }
            for row in rows
        ]

    # ------------------------------------------------------------- migration
    def import_curated(
        self,
        memory: CuratedMemory,
        *,
        reason: str = "migrate",
        actor: str | None = None,
    ) -> None:
        """Write a record while preserving its stored ``updated_at``.

        Migration and restore must not rewrite history. Freshness is a ranking
        signal, so restamping every imported memory would flatten the corpus to
        "all equally new" and quietly reorder recall after a migration that was
        supposed to be transparent.
        """
        self._put_curated(
            memory, reason=reason, stamp=memory.updated_at, actor=actor
        )

    def import_session(self, session: SessionMemory) -> None:
        """Session counterpart of :meth:`import_curated`."""
        self._put_session(session)


def _epoch_key(namespace: str) -> str:
    """``store_meta`` key holding one namespace's write counter."""
    return f"epoch:{namespace}"


def _as_json(raw: Any) -> Any:
    """Normalise a JSON column value so two representations can be compared.

    ``scope`` is JSONB, so PostgreSQL hands back a dict where the value written
    was a string - and JSONB does not preserve key order, so comparing the text
    would report a change on every write. A false "changed" is not cosmetic: it
    retires the stored embedding and silently drops the record out of the semantic
    channel until the next backfill.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, (dict, list, bool, int, float)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(raw)


# Column order for the dict-shaped governance reads. Written out rather than
# derived from ``cursor.description`` because the port documents these keys and a
# driver that returns a different description must not be allowed to rename them.
_LINK_ORDER = (
    "memory_id",
    "related_memory_id",
    "relation",
    "evidence",
    "created_at",
)
_VERSION_ORDER = (
    "memory_id",
    "version",
    "topic",
    "content",
    "from_trust",
    "trust",
    "confidence",
    "reason",
    "actor",
    "created_at",
)


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "PostgresStore",
    "PostgresUnavailable",
    "load_driver",
    "safe_dsn",
    "with_connect_timeout",
]
