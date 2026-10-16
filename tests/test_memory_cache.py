"""P4 acceptance: the recall cache is an accelerator and nothing else.

Section 6 puts a cache between the retriever and the store, and section 12 keeps
the source of truth in SQL. Those two are only compatible if a cached answer can
never be a *wrong* answer, so every test here is one of three claims:

* **Identical results.** Recall with a cache and recall without one return the
  same ids in the same order at the same scores. A cache that reordered hits
  under load would be indistinguishable from nondeterminism in the retriever.
* **Implicit invalidation.** No writer knows which queries it affected, so keys
  carry the store's ``corpus_epoch`` and one write retires the whole namespace.
  ``touch`` is the deliberate exception - access time is metadata, not corpus.
* **Degradation, never failure.** A cache that is off, unbuildable, unreachable,
  holding a payload from an older schema, or raising mid-call costs latency and
  nothing more.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from trace_agent.memory.cache import (
    KEY_PREFIX,
    InProcessCache,
    NullCache,
    RedisCache,
    build_recall_cache,
    cache_key,
)
from trace_agent.memory.flags import (
    MEMORY_CACHE_ENV,
    MEMORY_INCLUDE_CANDIDATE_ENV,
)
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    MemoryStatus,
    Provenance,
    RecallResult,
)
from trace_agent.memory.recall import RecallService
from trace_agent.memory.runtime import MemoryRuntime
from trace_agent.memory.store import JsonStore, SqliteStore

QUERY = "冷启动白屏 main thread blocked by sync IO"


def _memory(
    memory_id: str,
    *,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    confidence: float = 0.8,
    topic: str = "冷启动白屏",
    content: str = "main thread blocked by sync IO during cold start",
    keywords: tuple[str, ...] = ("冷启动", "白屏", "主线程"),
) -> CuratedMemory:
    """One recallable memory.

    ``topic`` is a parameter because :func:`~trace_agent.memory.recall._dedupe_hits`
    collapses near-identical topics into the best-scoring one. A test that seeds
    two memories on the same subject and then asserts both were recalled is
    asserting against the dedupe rule, not against the cache.
    """
    return CuratedMemory(
        memory_id=memory_id,
        kind=MemoryKind.PATTERN,
        scenario_type="cold_start",
        topic=topic,
        content=content,
        keywords=list(keywords),
        status=status,
        confidence=confidence,
        provenance=Provenance(
            source_type="finding", references=[f"run:{memory_id}", "ev:1"]
        ),
    )


def _episode(episode_id: str) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=episode_id,
        run_id=f"run-{episode_id}",
        trace_id="trace-1",
        scenario_type="cold_start",
        scenario="冷启动白屏",
        symptom="启动后长时间白屏",
        summary="main thread blocked by sync IO",
        root_causes=["main thread blocked by sync IO"],
    )


def _seeded_store(tmp_path: Path, *, kind: str = "sqlite") -> Any:
    """A store holding one active memory, one candidate and one episode."""
    root = tmp_path / "memory"
    store = SqliteStore(root) if kind == "sqlite" else JsonStore(root)
    store.write_curated(_memory("mem-active"))
    store.write_curated(
        _memory(
            "mem-guess",
            status=MemoryStatus.CANDIDATE,
            confidence=0.4,
            topic="冷启动白屏疑似网络超时",
            content="cold start blank page, main thread waiting on a sync network call",
            keywords=("冷启动", "白屏", "网络", "主线程"),
        )
    )
    store.write_episode(_episode("ep-1"))
    return store


def _keys(result: RecallResult) -> list[tuple[str, float]]:
    """Comparable identity for a recall: which records, in what order, at what score."""
    out: list[tuple[str, float]] = []
    for hit in result.hits:
        memory = hit.memory
        ident = getattr(memory, "memory_id", None) or getattr(memory, "episode_id", "")
        out.append((str(ident), round(float(hit.score), 9)))
    return out


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory knobs leak between tests; the cache ones decide what is built."""
    for name in (MEMORY_CACHE_ENV, MEMORY_INCLUDE_CANDIDATE_ENV):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------- cache objects
def test_null_cache_misses_everything() -> None:
    cache = NullCache()
    assert cache.get("k") is None
    cache.put("k", RecallResult(query="q"))
    assert cache.get("k") is None
    assert cache.clear() == 0
    snapshot = cache.snapshot()
    assert snapshot["kind"] == "off"
    assert snapshot["hits"] == 0
    assert snapshot["misses"] == 2
    assert snapshot["degraded_reason"] is None


def test_in_process_cache_round_trips_and_counts() -> None:
    cache = InProcessCache(maxsize=4, ttl=60.0)
    value = RecallResult(query="q")
    cache.put("k", value)
    assert cache.get("k") is value
    assert cache.get("other") is None
    snapshot = cache.snapshot()
    assert (snapshot["hits"], snapshot["misses"], snapshot["writes"]) == (1, 1, 1)
    assert snapshot["entries"] == 1
    assert snapshot["hit_rate"] == 0.5
    assert cache.clear() == 1
    assert cache.snapshot()["entries"] == 0


def test_in_process_cache_expires_by_ttl() -> None:
    cache = InProcessCache(maxsize=4, ttl=0.01)
    cache.put("k", RecallResult(query="q"))
    time.sleep(0.03)
    assert cache.get("k") is None
    # Expired entries are dropped on read: a sweeper thread would be a second
    # place for the cache to be wrong.
    assert cache.snapshot()["entries"] == 0


def test_in_process_cache_evicts_least_recently_used() -> None:
    cache = InProcessCache(maxsize=2, ttl=60.0)
    cache.put("a", RecallResult(query="a"))
    cache.put("b", RecallResult(query="b"))
    assert cache.get("a") is not None  # "a" becomes the most recently used
    cache.put("c", RecallResult(query="c"))
    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.snapshot()["evictions"] == 1


# -------------------------------------------------------------------- cache key
def test_cache_key_separates_everything_that_changes_the_answer(tmp_path: Path) -> None:
    sqlite = _seeded_store(tmp_path, kind="sqlite")
    files = _seeded_store(tmp_path / "other", kind="json")
    same = SqliteStore(tmp_path / "memory")

    base = cache_key(sqlite, query=QUERY, policy={"include_candidate": False})
    assert base.startswith(f"{KEY_PREFIX}:")
    # Same store, same params -> same key, so a repeat query is a hit.
    assert base == cache_key(same, query=QUERY, policy={"include_candidate": False})
    # A different query, adapter, namespace or policy must not share an entry.
    assert base != cache_key(sqlite, query="列表掉帧", policy={"include_candidate": False})
    assert base != cache_key(files, query=QUERY, policy={"include_candidate": False})
    assert base != cache_key(sqlite, query=QUERY, policy={"include_candidate": True})
    other_ns = SqliteStore(tmp_path / "memory", namespace="eval:run-1:case-1")
    assert base != cache_key(other_ns, query=QUERY, policy={"include_candidate": False})
    # One write retires every key for the namespace, without naming a query.
    sqlite.write_curated(_memory("mem-new"))
    assert base != cache_key(same, query=QUERY, policy={"include_candidate": False})


class _NoEpochStore:
    """A store that cannot say when it changed."""

    store_kind = "custom"
    namespace = "project:demo"


def test_build_recall_cache_refuses_a_store_without_an_epoch(tmp_path: Path) -> None:
    cache = build_recall_cache(_NoEpochStore(), mode="memory")
    assert isinstance(cache, NullCache)
    assert "corpus epoch" in (cache.degraded_reason or "")
    # The refusal is reported, not silent: "off because broken" must be
    # distinguishable from "off because configured off".
    assert build_recall_cache(_seeded_store(tmp_path), mode="memory").degraded_reason is None


def test_build_recall_cache_modes(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    assert isinstance(build_recall_cache(store, mode="off"), NullCache)
    assert isinstance(build_recall_cache(store, mode="memory"), InProcessCache)
    unknown = build_recall_cache(store, mode="memcached")
    assert isinstance(unknown, NullCache)
    assert "memcached" in (unknown.degraded_reason or "")


def test_build_recall_cache_reads_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _seeded_store(tmp_path)
    monkeypatch.setenv(MEMORY_CACHE_ENV, "memory")
    assert isinstance(build_recall_cache(store), InProcessCache)
    monkeypatch.setenv(MEMORY_CACHE_ENV, "nonsense")
    # An unrecognised mode means off, not the default: turning a cache on is a
    # decision, and a typo must not start buffering recall in a process nobody
    # intended to be stateful.
    assert isinstance(build_recall_cache(store), NullCache)


# ------------------------------------------------------------- recall parity
@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_cached_recall_matches_uncached_recall(tmp_path: Path, kind: str) -> None:
    store = _seeded_store(tmp_path, kind=kind)
    plain = RecallService(store)
    cached = RecallService(store, cache=InProcessCache(maxsize=8, ttl=60.0))

    first = cached.recall(QUERY, scenario_type="cold_start")
    second = cached.recall(QUERY, scenario_type="cold_start")
    expected = plain.recall(QUERY, scenario_type="cold_start")

    assert first.hits, "the seeded corpus must be recallable"
    assert _keys(first) == _keys(second) == _keys(expected)
    assert first.context_prompt == second.context_prompt == expected.context_prompt
    assert first.injected_ids == expected.injected_ids
    snapshot = cached.cache_snapshot()
    assert snapshot["hits"] == 1 and snapshot["misses"] == 1


def test_recall_stamps_latency_without_persisting_it(tmp_path: Path) -> None:
    """Every recall reports its own wall-clock cost; the cache stores none of it.

    Section 12.4 asks for ``latency_ms`` on the recall result so a run, the
    dashboard probe and an eval trial all quote the same number. A hit that
    replayed the miss's latency would report a cost this call never paid, so the
    stamp is applied to a copy on the way out and the entry keeps zero.
    """
    store = _seeded_store(tmp_path)
    cache = InProcessCache(maxsize=8, ttl=60.0)
    service = RecallService(store, cache=cache)

    first = service.recall(QUERY, scenario_type="cold_start")  # miss
    second = service.recall(QUERY, scenario_type="cold_start")  # hit

    assert first.latency_ms > 0.0 and second.latency_ms > 0.0
    assert first is not second
    assert _keys(first) == _keys(second), "stamping must not touch the answer"

    key = service._cache_key(
        QUERY, scenario_type="cold_start", top_k=5, include_episodic=True
    )
    stored = cache.get(key)
    assert stored is not None
    assert stored.latency_ms == 0.0, "the cached entry must stay unstamped"


def test_write_invalidates_the_cached_answer(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    cache = InProcessCache(maxsize=8, ttl=60.0)
    service = RecallService(store, cache=cache)

    before = service.recall(QUERY, scenario_type="cold_start")
    epoch = store.corpus_epoch()
    store.write_curated(
        _memory(
            "mem-newborn",
            topic="启动阶段主线程同步 IO",
            content="sync file read on the main thread leaves the first frame blank",
            keywords=("冷启动", "主线程", "白屏"),
        )
    )
    assert store.corpus_epoch() > epoch
    after = service.recall(QUERY, scenario_type="cold_start")

    assert "mem-newborn" in [key for key, _ in _keys(after)]
    # The answer was recomputed, not served: no entry survived the write.
    assert cache.snapshot()["hits"] == 0
    assert _keys(before) != _keys(after)


class _TouchSpy:
    """Delegates to a store while recording every ``touch`` call.

    ``last_accessed_at`` is store-owned metadata. It sits on
    :class:`~trace_agent.memory.models.CuratedMemory` with ``exclude=True``, so
    reads can carry it back to a caller while no write can smuggle a stale stamp
    into the stored payload - recalling a memory must not make it look newly
    corroborated. The observable fact here is therefore that recall *asked* for
    the stamp, and that asking did not invalidate the entry it had just read.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def touch(self, memory_id: str) -> None:
        self.touched.append(memory_id)
        self._store.touch(memory_id)


def test_touching_a_hit_does_not_invalidate_it(tmp_path: Path) -> None:
    """Access time is metadata. Stamping it must not evict what was just read."""
    store = _TouchSpy(_seeded_store(tmp_path))
    cache = InProcessCache(maxsize=8, ttl=60.0)
    service = RecallService(store, cache=cache)

    first = service.recall(QUERY, scenario_type="cold_start")
    assert first.hits
    epoch = store.corpus_epoch()
    second = service.recall(QUERY, scenario_type="cold_start")

    assert store.corpus_epoch() == epoch
    assert cache.snapshot()["hits"] == 1
    assert _keys(first) == _keys(second)
    assert "mem-active" in store.touched
    # Episodes carry no access-time column; their value is already captured by
    # the memories projected from them.
    assert "ep-1" not in store.touched


def test_empty_recall_is_not_cached(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    cache = InProcessCache(maxsize=8, ttl=60.0)
    service = RecallService(store, cache=cache)

    first = service.recall("毫不相干的查询 zzz", scenario_type="cold_start")
    assert first.hits == []
    assert cache.snapshot()["entries"] == 0
    service.recall("毫不相干的查询 zzz", scenario_type="cold_start")
    assert cache.snapshot()["hits"] == 0
    assert cache.snapshot()["misses"] == 2


def test_a_policy_change_does_not_share_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``include_candidate`` is read from the environment, so it must be in the key.

    This is the section-10 rule enforced through the cache: flipping candidate
    recall on in a dashboard must not be served the answer computed with it off.
    """
    store = _seeded_store(tmp_path)
    cache = InProcessCache(maxsize=8, ttl=60.0)
    service = RecallService(store, cache=cache)

    monkeypatch.setenv(MEMORY_INCLUDE_CANDIDATE_ENV, "0")
    trusted = service.recall(QUERY, scenario_type="cold_start")
    monkeypatch.setenv(MEMORY_INCLUDE_CANDIDATE_ENV, "1")
    with_candidates = service.recall(QUERY, scenario_type="cold_start")

    ids_trusted = {key for key, _ in _keys(trusted)}
    ids_all = {key for key, _ in _keys(with_candidates)}
    assert "mem-guess" not in ids_trusted
    assert "mem-guess" in ids_all
    assert cache.snapshot()["hits"] == 0
    assert cache.snapshot()["entries"] == 2


class _BrokenCache(InProcessCache):
    """A cache that fails on every call, the way a network partition does."""

    kind = "broken"

    def get(self, key: str) -> RecallResult | None:
        raise RuntimeError("cache down")

    def put(self, key: str, value: RecallResult) -> None:
        raise RuntimeError("cache down")


def test_a_broken_cache_costs_latency_not_correctness(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    plain = RecallService(store)
    service = RecallService(store, cache=_BrokenCache(maxsize=4, ttl=60.0))

    result = service.recall(QUERY, scenario_type="cold_start")
    assert _keys(result) == _keys(plain.recall(QUERY, scenario_type="cold_start"))
    assert service.cache_snapshot()["kind"] == "broken"


def test_runtime_builds_no_cache_by_default(tmp_path: Path) -> None:
    """The default deployment must not pay an epoch query per recall."""
    runtime = MemoryRuntime(tmp_path / "memory")
    assert runtime.cache is None
    assert runtime.cache_stats()["kind"] == "off"
    runtime.recall(QUERY, scenario_type="cold_start")
    assert runtime.cache_stats()["hits"] == 0


def test_runtime_builds_the_configured_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MEMORY_CACHE_ENV, "memory")
    runtime = MemoryRuntime(tmp_path / "memory")
    assert isinstance(runtime.cache, InProcessCache)

    runtime.store.write_curated(_memory("mem-active"))
    runtime.remember_episode(_episode("ep-1"), consolidate=False)
    first = runtime.recall(QUERY, scenario_type="cold_start")
    second = runtime.recall(QUERY, scenario_type="cold_start")
    assert first.hits
    assert _keys(first) == _keys(second)
    assert runtime.cache_stats()["hits"] >= 1


# ------------------------------------------------------------------------ redis
class _FakeRedis:
    """Enough of the client surface for the cache, without a server."""

    def __init__(self, *, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.fail = fail
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("no route to host")
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise ConnectionError("no route to host")
        self.data[key] = value
        self.ttls[key] = int(ex or 0)

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.data.pop(key, None) is not None else 0
        return removed

    def scan(self, cursor: int = 0, match: str = "*", count: int = 100):
        return 0, list(self.data)


def test_redis_cache_round_trips_through_an_injected_client() -> None:
    client = _FakeRedis()
    cache = RedisCache(client=client, ttl=42.0)
    value = RecallResult(query="q")
    cache.put("k", value)
    assert client.ttls["k"] == 42

    restored = cache.get("k")
    assert restored is not None
    assert restored.model_dump_json() == value.model_dump_json()
    assert cache.snapshot()["connected"] is True
    assert cache.clear() == 1
    assert cache.get("k") is None


def test_redis_cache_treats_an_unreadable_payload_as_a_miss() -> None:
    """A rolling upgrade must not become an outage."""
    client = _FakeRedis()
    client.data["k"] = "{not a RecallResult"
    cache = RedisCache(client=client)
    assert cache.get("k") is None
    assert cache.snapshot()["errors"] == 1


def test_redis_cache_degrades_when_the_server_is_down() -> None:
    cache = RedisCache(client=_FakeRedis(fail=True))
    cache.put("k", RecallResult(query="q"))
    assert cache.get("k") is None
    assert cache.clear() == 0
    snapshot = cache.snapshot()
    assert snapshot["errors"] >= 2
    assert snapshot["hits"] == 0


def test_redis_snapshot_redacts_credentials() -> None:
    cache = RedisCache(
        url="redis://:s3cret@cache.internal:6379/2", client=_FakeRedis()
    )
    snapshot = cache.snapshot()
    assert "s3cret" not in str(snapshot)
    assert snapshot["url"] == "redis://***@cache.internal:6379/2"


def test_build_recall_cache_gives_up_on_an_unreachable_redis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing to cache is an infrastructure answer, and it must be reported."""
    monkeypatch.setenv("TRACE_AGENT_MEMORY_CACHE_URL", "redis://127.0.0.1:1/0")
    cache = build_recall_cache(_seeded_store(tmp_path), mode="redis")
    assert isinstance(cache, NullCache)
    assert cache.degraded_reason


@pytest.fixture()
def live_redis() -> Any:
    """A real Redis, or a skip. Optional infrastructure is tested when present."""
    try:
        import redis  # noqa: PLC0415 - optional dependency
    except ImportError:
        pytest.skip("redis package not installed")
    url = os.environ.get("TRACE_AGENT_MEMORY_CACHE_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1.0)
        client.ping()
    except Exception as exc:  # noqa: BLE001 - unreachable means "skip", not "fail"
        pytest.skip(f"redis unreachable at {url}: {exc}")
    return client


def test_redis_cache_against_a_live_server(tmp_path: Path, live_redis: Any) -> None:
    store = _seeded_store(tmp_path)
    cache = RedisCache(client=live_redis, ttl=30.0)
    service = RecallService(store, cache=cache)
    try:
        first = service.recall(QUERY, scenario_type="cold_start")
        assert first.hits
        second = service.recall(QUERY, scenario_type="cold_start")
        assert _keys(first) == _keys(second)
        assert cache.snapshot()["hits"] == 1
        assert cache.snapshot()["writes"] == 1

        # The value survived a JSON round trip through the server intact.
        key = cache_key(
            store,
            query=QUERY,
            scenario_type="cold_start",
            top_k=5,
            include_episodic=True,
            policy=service._policy_fingerprint(),
        )
        raw = live_redis.get(key)
        assert raw is not None
        restored = RecallResult.model_validate_json(raw)
        # ``latency_ms`` is *this call's* cost, stamped on the way out and never
        # written back. A hit must not replay the timing of the miss that filled
        # the entry, so the serialised payload carries zero while ``first`` -
        # what the caller actually got - carries the real number. Everything else
        # has to survive the JSON round trip through the server byte for byte.
        assert restored.latency_ms == 0.0
        assert first.latency_ms > 0.0
        assert restored.model_dump_json() == first.model_copy(
            update={"latency_ms": 0.0}
        ).model_dump_json()
    finally:
        cache.clear()


def test_redis_mode_end_to_end_when_a_server_is_present(
    tmp_path: Path, live_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TRACE_AGENT_MEMORY_CACHE=redis`` builds a working shared cache."""
    store = _seeded_store(tmp_path)
    cache = build_recall_cache(store, mode="redis")
    if isinstance(cache, NullCache):
        pytest.skip(f"redis declined: {cache.degraded_reason}")
    service = RecallService(store, cache=cache)
    try:
        assert _keys(service.recall(QUERY, scenario_type="cold_start")) == _keys(
            RecallService(store).recall(QUERY, scenario_type="cold_start")
        )
        assert service.recall(QUERY, scenario_type="cold_start").hits
        assert cache.snapshot()["hits"] >= 1
    finally:
        cache.clear()
