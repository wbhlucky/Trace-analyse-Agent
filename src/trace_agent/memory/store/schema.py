"""DDL for the SQL memory store.

This is the section-4 table shape from ``docs/memory-optimization-plan.md``,
and it is meant to be the *final* shape: SQLite and Postgres use the same
columns, keys and semantics, so moving to ``PostgresStore`` + pgvector is a new
adapter over an unchanged model rather than a second migration.

Three deliberate refinements over the section-4 sketch, all of them about
keeping one source of truth:

* ``namespace`` is part of every primary key. Section 8 requires that
  retrieval never crosses a namespace, and section 12 migrates by
  ``(namespace, id)``. A global ``id`` primary key would make the same memory
  id in two namespaces collide instead of coexisting, which is exactly the
  eval-isolation failure the plan is trying to remove.
* Every entity row carries a ``payload`` JSON column with the full Pydantic
  dump. Structured columns exist for filtering, ordering and analytics;
  ``payload`` is what reads are reconstructed from. Without it the SQL store
  would return a slightly different object than the JSON store (missing
  ``confirmed_run_ids``, dropped fields), and "identical recall on both
  adapters" would be a claim rather than a property.
* FTS tables have a single ``body`` column. Section 3.3 sketches
  ``topic``/``content``/``keywords`` columns, but the scorer reads a
  concatenation defined in :mod:`trace_agent.memory.search_text`. Indexing the
  same concatenation means adding a scored field cannot silently desync the
  index, and one column is a smaller index than three.

FTS rows are keyed by the parent table's implicit ``rowid``, which is stable
across ``ON CONFLICT DO UPDATE``. That makes index maintenance
``DELETE ... WHERE rowid = ?`` plus one insert - O(log n) - instead of
searching the FTS table for the row to replace.
"""

from __future__ import annotations

import sqlite3

# Bumped whenever a column, key or index changes meaning. ``store_meta`` records
# what a database file was created with, so ``memory migrate`` can refuse to
# half-upgrade a corpus instead of writing rows the code cannot read back.
#
# 2 adds the ``episodes`` embedding columns (P3). ``memories`` already carried
# them from version 1, which is why the migration list below only has to name
# one table.
#
# 3 adds the ownership columns: ``episodes.session_id``/``project_id`` (already
# in the CREATE below, so only a pre-3 file needs the ALTER) and
# ``sessions.agent_id``/``user_id``/``project_id``. All five are nullable and
# never read as a filter requirement, which is what makes the upgrade additive:
# a row written at version 2 stays valid and simply has no owner recorded.
#
# 3 also adds ``memory_versions.from_trust``. ``trust`` alone says where a
# record ended up; a transition is the pair, and reconstructing the other half
# from row order fails exactly when it matters - on a corpus whose history was
# migrated in pieces. The file adapter has recorded it since P2, so this is the
# SQL side catching up to the audit trail it is supposed to mirror.
SCHEMA_VERSION = 3

# Columns added after a table shipped, as ``(table, column, DDL fragment)``.
# ``apply_schema`` runs these before stamping the version, checking
# ``PRAGMA table_info`` first, so the same list is both "what an old database is
# missing" and "how to give it that". SQLite has no ``ADD COLUMN IF NOT
# EXISTS``, and re-running a bare ``ALTER TABLE`` fails; the probe is what makes
# opening a corpus idempotent instead of a one-shot upgrade.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("episodes", "embedding", "BLOB"),
    ("episodes", "embedding_model", "TEXT"),
    ("episodes", "session_id", "TEXT"),
    ("episodes", "project_id", "TEXT"),
    ("sessions", "agent_id", "TEXT"),
    ("sessions", "user_id", "TEXT"),
    ("sessions", "project_id", "TEXT"),
    ("memory_versions", "from_trust", "TEXT"),
)

# ``references`` is a reserved word in SQL, so it is always double-quoted. The
# quoting is called out here because forgetting it on a future column fails at
# runtime with a confusing syntax error rather than at review time.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  namespace     TEXT NOT NULL,
  id            TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  topic         TEXT NOT NULL DEFAULT '',
  summary       TEXT NOT NULL DEFAULT '',
  agent_id      TEXT,
  user_id       TEXT,
  project_id    TEXT,
  scenario_type TEXT,
  device        TEXT,
  build         TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  updated_at    TEXT NOT NULL,
  payload       TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (namespace, id)
);

CREATE TABLE IF NOT EXISTS session_messages (
  seq          INTEGER PRIMARY KEY,
  namespace    TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  role         TEXT NOT NULL,
  content      TEXT NOT NULL,
  "references" TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session
  ON session_messages (namespace, session_id, seq);

CREATE TABLE IF NOT EXISTS episodes (
  namespace      TEXT NOT NULL,
  id             TEXT NOT NULL,
  session_id     TEXT,
  project_id     TEXT,
  run_id         TEXT NOT NULL,
  trace_id       TEXT NOT NULL DEFAULT '',
  scenario_type  TEXT NOT NULL DEFAULT '',
  scenario       TEXT NOT NULL DEFAULT '',
  symptom        TEXT NOT NULL DEFAULT '',
  target_process TEXT,
  device         TEXT,
  build          TEXT,
  summary        TEXT NOT NULL DEFAULT '',
  root_causes    TEXT NOT NULL DEFAULT '[]',
  outcome        TEXT NOT NULL DEFAULT 'completed',
  confidence     REAL NOT NULL DEFAULT 0,
  eval_run_id    TEXT,
  graded_pass    INTEGER,
  source_run_dir TEXT,
  created_at     TEXT NOT NULL,
  payload        TEXT NOT NULL DEFAULT '{}',
  embedding       BLOB,
  embedding_model TEXT,
  PRIMARY KEY (namespace, id),
  UNIQUE (namespace, run_id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_ns_created
  ON episodes (namespace, created_at DESC);

CREATE TABLE IF NOT EXISTS memories (
  namespace         TEXT NOT NULL,
  id                TEXT NOT NULL,
  type              TEXT NOT NULL DEFAULT 'semantic',
  kind              TEXT NOT NULL DEFAULT 'pattern',
  topic             TEXT NOT NULL DEFAULT '',
  content           TEXT NOT NULL DEFAULT '',
  keywords          TEXT NOT NULL DEFAULT '[]',
  scope             TEXT NOT NULL DEFAULT '{}',
  trust             TEXT NOT NULL DEFAULT 'candidate',
  status_reason     TEXT,
  confidence        REAL NOT NULL DEFAULT 0,
  importance        REAL NOT NULL DEFAULT 0.5,
  severity          TEXT,
  occurrences       INTEGER NOT NULL DEFAULT 1,
  distinct_runs     INTEGER NOT NULL DEFAULT 1,
  contradictions    INTEGER NOT NULL DEFAULT 0,
  version           INTEGER NOT NULL DEFAULT 1,
  source_episode_id TEXT,
  supersedes_id     TEXT,
  superseded_by     TEXT,
  archived_reason   TEXT,
  provenance        TEXT NOT NULL DEFAULT '{}',
  payload           TEXT NOT NULL DEFAULT '{}',
  embedding         BLOB,
  embedding_model   TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  last_accessed_at  TEXT,
  expires_at        TEXT,
  PRIMARY KEY (namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_memories_scope
  ON memories (namespace, type, trust, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_trust
  ON memories (namespace, trust);
CREATE INDEX IF NOT EXISTS idx_memories_expires
  ON memories (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_sources (
  namespace  TEXT NOT NULL,
  memory_id  TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  episode_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (namespace, memory_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_sources_memory
  ON memory_sources (namespace, memory_id);

CREATE TABLE IF NOT EXISTS memory_versions (
  seq        INTEGER PRIMARY KEY,
  namespace  TEXT NOT NULL,
  memory_id  TEXT NOT NULL,
  version    INTEGER NOT NULL,
  topic      TEXT NOT NULL DEFAULT '',
  content    TEXT NOT NULL DEFAULT '',
  from_trust TEXT,
  trust      TEXT NOT NULL DEFAULT 'candidate',
  confidence REAL NOT NULL DEFAULT 0,
  reason     TEXT NOT NULL DEFAULT '',
  actor      TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (namespace, memory_id, version)
);
CREATE INDEX IF NOT EXISTS idx_versions_memory
  ON memory_versions (namespace, memory_id, version);

CREATE TABLE IF NOT EXISTS memory_links (
  namespace         TEXT NOT NULL,
  memory_id         TEXT NOT NULL,
  related_memory_id TEXT NOT NULL,
  relation          TEXT NOT NULL,
  evidence          TEXT,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (namespace, memory_id, related_memory_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_links_memory
  ON memory_links (namespace, memory_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  namespace UNINDEXED,
  memory_id UNINDEXED,
  body,
  tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  namespace UNINDEXED,
  episode_id UNINDEXED,
  body,
  tokenize='unicode61'
);
"""


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if absent, then stamp the schema version.

    Idempotent by construction (``IF NOT EXISTS`` everywhere), so it runs on
    every new connection and doubles as the migration entry point: opening an
    older database adds whatever is missing without touching existing rows.
    """
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn)
    conn.execute(
        # ``DO UPDATE``, not ``DO NOTHING``: a file created at version 1 and
        # then opened by version 2 code has already been upgraded by the ALTER
        # above, and a stamp that never moves would make ``memory migrate`` and
        # ``memory stats`` keep reporting a schema the rows no longer match.
        "INSERT INTO store_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns a database file predates, leaving existing rows untouched.

    Additive only, on purpose. Every column here is nullable with no default
    that carries meaning, so backfilling is a separate, resumable job
    (:meth:`SemanticIndex.backfill`) rather than something a connection open has
    to finish before returning. A corpus missing its vectors is a corpus whose
    recall is lexical-only, which is exactly the pre-P3 behaviour.
    """
    for table, column, fragment in _ADDITIVE_COLUMNS:
        try:
            present = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.Error:
            continue
        if not present or column in present:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {fragment}")
        except sqlite3.Error:
            # A database that cannot be altered still works: the column stays
            # absent, ``store_supports_vectors`` reports the store as unusable
            # and recall degrades instead of the run failing.
            continue


def schema_version(conn: sqlite3.Connection) -> int:
    """Version a database file was created with, or 0 for an uninitialised one."""
    try:
        row = conn.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0
