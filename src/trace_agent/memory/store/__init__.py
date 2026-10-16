"""Store selection for the long-term memory corpus.

One factory, one environment variable. Services depend on
:class:`~trace_agent.memory.ports.MemoryStore` and never on an adapter, so
switching between SQL and files is a configuration change rather than a code
change - which is the property that makes ``TRACE_AGENT_MEMORY_STORE=json`` a
usable rollback in an incident instead of a revert-and-redeploy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trace_agent.memory.flags import (
    MEMORY_STORE_JSON,
    MEMORY_STORE_POSTGRES,
    memory_connect_timeout,
    memory_dsn,
    memory_schema,
    memory_store_kind,
)
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.store.json_store import JsonStore
from trace_agent.memory.store.sqlite_store import SqliteStore


def build_memory_store(
    root: Path | str,
    *,
    namespace: str | None = None,
    kind: str | None = None,
) -> MemoryStore:
    """Build the configured store, degrading to a local adapter if SQL is unusable.

    The probe is why this is a function rather than a constructor call. FTS5 is
    a compile-time option and is absent from some system SQLite builds; without
    the probe such a host would construct a store successfully and then lose
    every write to a caught best-effort exception - an empty memory corpus that
    looks healthy. Falling back to the file adapter keeps memory working and
    records why on ``degraded_reason`` so ``memory stats`` can surface it.

    Probing does touch disk, in that it creates the database file. That is a
    deliberate trade: a silently dead corpus is worse than an empty file
    appearing when a runtime is built.

    ``postgres`` follows the same rule one level up: it is a *request*, and a
    request that cannot be honoured - no DSN, no driver, no server, no CREATE
    privilege - degrades to the local SQLite corpus instead of failing the run.
    That is the right default for memory, which is allowed to cost recall quality
    and is never allowed to cost an analysis, and it is also the safe one: the
    fallback is a real corpus on local disk rather than a store that silently
    discards writes. ``degraded_reason`` says which happened, so an operator who
    asked for PostgreSQL and got SQLite can see it in ``memory stats`` rather
    than discover it by finding the corpus on the wrong machine.
    """
    resolved = (kind or memory_store_kind()).strip().lower()
    if resolved == MEMORY_STORE_JSON:
        return JsonStore(root, namespace=namespace)
    if resolved == MEMORY_STORE_POSTGRES:
        store, reason = _try_postgres_store(root, namespace=namespace)
        if store is not None:
            return store
        fallback = _local_store(root, namespace=namespace)
        fallback.degraded_reason = f"postgres store unavailable: {reason}"
        return fallback
    return _local_store(root, namespace=namespace)


def _local_store(root: Path | str, *, namespace: str | None) -> Any:
    """SQLite, or files when SQLite itself is unusable."""
    try:
        store = SqliteStore(root, namespace=namespace)
        # Reading the schema version forces a connection and applies the DDL, so
        # this is where a missing FTS5, an unwritable root or a locked file
        # surfaces instead of at the first write.
        store.version
        return store
    except Exception as exc:  # pragma: no cover - depends on host SQLite build
        fallback = JsonStore(root, namespace=namespace)
        fallback.degraded_reason = f"sqlite store unavailable: {exc}"
        return fallback


def _try_postgres_store(
    root: Path | str, *, namespace: str | None
) -> tuple[Any | None, str]:
    """Attempt the PostgreSQL adapter; ``(store, "")`` or ``(None, reason)``.

    Imported lazily and never exported at module scope, so ``import
    trace_agent.memory.store`` stays free of any driver import: the adapter is
    optional infrastructure, and a package that could not be imported without
    psycopg2 installed would make ``TRACE_AGENT_MEMORY_STORE=sqlite`` a lie about
    the dependency list.
    """
    from trace_agent.memory.store.postgres_store import PostgresStore, safe_dsn

    dsn = memory_dsn()
    if not dsn:
        return None, "TRACE_AGENT_MEMORY_DSN is not set"
    # Credential-free, and part of the reason even when the failure happened
    # before a connection was attempted (no driver, no CREATE privilege): an
    # operator reading ``memory stats`` has to know which server was asked for,
    # otherwise "postgres store unavailable" names no host to go look at.
    target = safe_dsn(dsn)
    try:
        store = PostgresStore(
            root,
            namespace=namespace,
            dsn=dsn,
            schema=memory_schema(),
            connect_timeout=memory_connect_timeout(),
        )
        store.probe()
        return store, ""
    except Exception as exc:
        return None, f"{target}: {type(exc).__name__}: {exc}"


def store_diagnostics(store: Any) -> dict[str, Any]:
    """Uniform facts about a store, for ``memory stats`` and the dashboard."""
    payload = {
        "kind": getattr(store, "store_kind", type(store).__name__),
        "root": str(getattr(store, "root", "")),
        "db_path": str(getattr(store, "db_path", "") or ""),
        "namespace": getattr(store, "namespace", ""),
        "pushdown": bool(callable(getattr(store, "search", None))),
        "degraded_reason": getattr(store, "degraded_reason", None),
    }
    # Server-side facts only a PostgreSQL adapter has. Reported as extra keys
    # rather than a nested dict so every consumer keeps reading the same shape,
    # and both are already credential-free: ``server`` comes from ``safe_dsn``.
    server = getattr(store, "server", "")
    if server:
        payload["server"] = str(server)
        payload["server_version"] = str(getattr(store, "server_version", "") or "")
        payload["pgvector"] = bool(getattr(store, "pgvector", False))
        payload["vectors"] = bool(
            callable(getattr(store, "vector_search", None))
        )
    return payload


__all__ = [
    "JsonStore",
    "MemoryStore",
    "SqliteStore",
    "build_memory_store",
    "store_diagnostics",
]
