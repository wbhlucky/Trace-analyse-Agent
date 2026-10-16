"""Recall cache (P4): a read accelerator that is allowed to be wrong nowhere.

Section 6 puts a cache between the retriever and the store, and section 12 keeps
the source of truth in SQL. Those two are only compatible if the cache can be
invalidated without any writer having to reason about which queries it affected,
so every key here carries the store's ``corpus_epoch``: one write advances the
counter and every cached answer for that namespace stops matching at once.
Deliberately blunt - a cache miss costs one indexed query, a stale prior in a
prompt costs the diagnosis.

Three modes, all of which must produce identical recall results:

``off``
    The default. :class:`NullCache` answers every lookup with a miss, which is
    the behaviour the whole module is measured against.
``memory``
    A bounded LRU with a TTL, for one process answering the same question
    repeatedly - the dashboard refreshing a panel, an eval recalling per trial.
``redis``
    The same entries in Redis, for several processes sharing them. The client is
    imported lazily and *every* failure degrades to uncached recall with a
    recorded reason: a cache that is down must cost latency, never correctness.

Nothing here is on the write path and nothing here raises. A cache that cannot
be built, cannot be reached, or holds a payload that no longer validates is
indistinguishable from no cache at all, apart from the diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

from trace_agent.memory.flags import (
    MEMORY_CACHE_MEMORY,
    MEMORY_CACHE_OFF,
    MEMORY_CACHE_REDIS,
    memory_cache_mode,
    memory_cache_size,
    memory_cache_ttl,
    memory_cache_url,
)
from trace_agent.memory.models import RecallResult
from trace_agent.memory.ports import store_supports_epoch

# Namespace for Redis keys. Prefixed rather than bare so a Redis instance shared
# with other subsystems cannot collide on a hex digest, and so ``memory flush``
# can drop every entry with one scan pattern.
KEY_PREFIX = "trace-agent:recall"


def cache_key(store: Any, **params: Any) -> str:
    """Cache identity for one recall.

    Three parts, each of which exists because leaving it out produces a wrong
    answer rather than a slower one:

    * store kind and namespace, so two adapters or two isolation domains in one
      process cannot share an entry - section 8's rule would be meaningless if a
      ``project:`` query could be served from an ``eval:`` result;
    * ``corpus_epoch``, the implicit invalidation described above;
    * every parameter that changes the result, *including* the ones read from the
      environment at call time. ``include_candidate`` and the weight table are
      the reason: flipping candidate recall on in a dashboard must not be served
      the answer computed with it off, and those two never appear in the
      arguments a caller passes.

    Hashed because the parameter dump can carry a whole weight table and Redis
    keys should stay short and printable.
    """
    identity = {
        "store": str(getattr(store, "store_kind", "unknown")),
        "namespace": str(getattr(store, "namespace", "")),
        "epoch": _epoch_of(store),
    }
    payload = json.dumps(
        {"identity": identity, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"{KEY_PREFIX}:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"


def _epoch_of(store: Any) -> int:
    """The store's write counter, or ``-1`` when it cannot report one.

    ``-1`` rather than ``0`` so a store without an epoch does not share keys
    with an empty one that has it. Callers are expected to have refused to cache
    in that case anyway (see :func:`build_recall_cache`); this is the belt to
    that brace.
    """
    reader = getattr(store, "corpus_epoch", None)
    if not callable(reader):
        return -1
    try:
        return int(reader())
    except Exception:
        return -1


class RecallCache(Protocol):
    """What :class:`~trace_agent.memory.recall.RecallService` needs from a cache."""

    #: Reported by ``memory stats`` and the dashboard.
    kind: str

    #: Non-empty when caching was requested but is not in effect, so an operator
    #: can tell "off because configured off" from "off because broken".
    degraded_reason: str | None

    def get(self, key: str) -> RecallResult | None: ...

    def put(self, key: str, value: RecallResult) -> None: ...

    def clear(self) -> int: ...

    def snapshot(self) -> dict[str, Any]: ...


class NullCache:
    """No caching. Every lookup misses, every write is dropped."""

    kind = MEMORY_CACHE_OFF

    def __init__(self, *, degraded_reason: str | None = None) -> None:
        self.degraded_reason = degraded_reason
        self._misses = 0

    def get(self, key: str) -> RecallResult | None:
        self._misses += 1
        return None

    def put(self, key: str, value: RecallResult) -> None:
        return None

    def clear(self) -> int:
        return 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entries": 0,
            "hits": 0,
            "misses": self._misses,
            "writes": 0,
            "evictions": 0,
            "errors": 0,
            "degraded_reason": self.degraded_reason,
        }


class InProcessCache:
    """Bounded LRU with a TTL, for a single process.

    The TTL is not the invalidation mechanism - the epoch is. It bounds how long
    a *stale clock*, a hand-edited corpus, or a write made through a path that
    does not bump the epoch can keep serving an old answer. Short by default for
    exactly that reason.

    Locked rather than lock-free: the dashboard can recall from several request
    threads at once, and an ``OrderedDict`` recency update is a mutation.
    """

    kind = MEMORY_CACHE_MEMORY

    def __init__(
        self,
        *,
        maxsize: int | None = None,
        ttl: float | None = None,
        degraded_reason: str | None = None,
    ) -> None:
        self.maxsize = max(1, int(maxsize if maxsize is not None else memory_cache_size()))
        self.ttl = float(ttl if ttl is not None else memory_cache_ttl())
        self.degraded_reason = degraded_reason
        self._entries: OrderedDict[str, tuple[float, RecallResult]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0
        self._errors = 0

    def get(self, key: str) -> RecallResult | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, value = entry
            if expires_at <= now:
                # Expired entries are dropped on read rather than by a sweeper:
                # a cache with no background thread has nothing to sweep with,
                # and an entry nobody asks for again costs one dict slot.
                del self._entries[key]
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: str, value: RecallResult) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self.ttl, value)
            self._entries.move_to_end(key)
            self._writes += 1
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)
                self._evictions += 1

    def clear(self) -> int:
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            return removed

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "kind": self.kind,
                "entries": len(self._entries),
                "maxsize": self.maxsize,
                "ttl_seconds": self.ttl,
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "evictions": self._evictions,
                "errors": self._errors,
                "hit_rate": _rate(self._hits, self._misses),
                "degraded_reason": self.degraded_reason,
            }


class RedisCache:
    """Shared cache over Redis, degrading to :class:`NullCache` behaviour on error.

    Values are the Pydantic JSON dump. Deserialising on read and treating a
    failure as a miss is what makes a schema change survivable: an entry written
    by an older build simply stops validating, the recall is recomputed, and the
    stale payload is overwritten on the next write. The alternative - raising -
    would turn a rolling upgrade into an outage.

    The connection is opened lazily so importing this module, or building a
    runtime in a test, never touches the network.
    """

    kind = MEMORY_CACHE_REDIS

    def __init__(
        self,
        *,
        url: str | None = None,
        ttl: float | None = None,
        client: Any = None,
        degraded_reason: str | None = None,
    ) -> None:
        self.url = url if url is not None else memory_cache_url()
        self.ttl = float(ttl if ttl is not None else memory_cache_ttl())
        self.degraded_reason = degraded_reason
        self._client = client
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0
        self._errors = 0

    # ------------------------------------------------------------- connection
    @property
    def client(self) -> Any:
        """The Redis client, connected on first use, ``None`` when unavailable."""
        with self._lock:
            if self._client is not None or self.degraded_reason:
                return self._client
            try:
                import redis  # noqa: PLC0415 - optional dependency, lazy by design
            except ImportError:
                self.degraded_reason = "redis package not installed"
                return None
            try:
                client = redis.Redis.from_url(
                    self.url, decode_responses=True, socket_connect_timeout=2.0
                )
                client.ping()
            except Exception as exc:  # noqa: BLE001 - any failure means "no cache"
                self.degraded_reason = f"redis unavailable: {exc.__class__.__name__}"
                self._errors += 1
                return None
            self._client = client
            return client

    def get(self, key: str) -> RecallResult | None:
        client = self.client
        if client is None:
            self._misses += 1
            return None
        try:
            raw = client.get(key)
        except Exception:  # noqa: BLE001 - a cache outage must not fail a recall
            self._errors += 1
            self._misses += 1
            return None
        if not raw:
            self._misses += 1
            return None
        try:
            result = RecallResult.model_validate_json(raw)
        except Exception:  # noqa: BLE001 - unreadable entry is a miss, not an error
            self._errors += 1
            self._misses += 1
            return None
        self._hits += 1
        return result

    def put(self, key: str, value: RecallResult) -> None:
        client = self.client
        if client is None:
            return None
        try:
            payload = value.model_dump_json()
        except Exception:  # noqa: BLE001 - unserialisable result simply is not cached
            self._errors += 1
            return None
        try:
            client.set(key, payload, ex=max(1, int(self.ttl)))
            self._writes += 1
        except Exception:  # noqa: BLE001
            self._errors += 1
        return None

    def clear(self) -> int:
        """Drop this deployment's entries. Returns how many were deleted."""
        client = self.client
        if client is None:
            return 0
        removed = 0
        try:
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor=cursor, match=f"{KEY_PREFIX}:*", count=200)
                if keys:
                    removed += int(client.delete(*keys))
                if not cursor:
                    break
        except Exception:  # noqa: BLE001
            self._errors += 1
        return removed

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": _redact(self.url),
            "ttl_seconds": self.ttl,
            "connected": self._client is not None,
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "evictions": self._evictions,
            "errors": self._errors,
            "hit_rate": _rate(self._hits, self._misses),
            "degraded_reason": self.degraded_reason,
        }


def build_recall_cache(store: Any = None, *, mode: str | None = None) -> RecallCache:
    """Build the cache the configuration asks for, or the null one.

    Two refusals, both reported through ``degraded_reason`` so they are visible
    in ``memory stats`` instead of silently looking like a cold cache:

    * a store that cannot report ``corpus_epoch``, because caching against it
      would mean guessing an invalidation rule per writer;
    * a Redis deployment that cannot be reached, because that is an
      infrastructure problem and not a reason to serve wrong priors.
    """
    requested = (mode or memory_cache_mode() or MEMORY_CACHE_OFF).strip().lower()
    if requested == MEMORY_CACHE_OFF:
        return NullCache()
    if store is not None and not store_supports_epoch(store):
        return NullCache(
            degraded_reason=(
                f"store {getattr(store, 'store_kind', 'unknown')!r} reports no "
                "corpus epoch; recall caching disabled"
            )
        )
    if requested == MEMORY_CACHE_REDIS:
        cache = RedisCache()
        # Touch the connection now rather than on first recall, so the reason a
        # cache is not in effect is known before it matters and appears in
        # diagnostics from the first run.
        if cache.client is None:
            return NullCache(degraded_reason=cache.degraded_reason or "redis unavailable")
        return cache
    if requested == MEMORY_CACHE_MEMORY:
        return InProcessCache()
    return NullCache(degraded_reason=f"unknown cache mode {requested!r}")


def _rate(hits: int, misses: int) -> float:
    total = hits + misses
    return round(hits / total, 4) if total else 0.0


def _redact(url: str) -> str:
    """Drop credentials from a Redis URL before it reaches a log or a report."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


__all__ = [
    "InProcessCache",
    "KEY_PREFIX",
    "NullCache",
    "RecallCache",
    "RedisCache",
    "build_recall_cache",
    "cache_key",
]
