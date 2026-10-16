"""P3 acceptance: the semantic half of hybrid memory recall.

Section 11 of the design doc lists P3's exit criteria as (a) a paraphrase query
set recalling above threshold and (b) embeddings degrading to lexical-only
without raising when unavailable. This module pins those plus the four
properties that make the vector channel safe to depend on:

* **Adapter parity.** ``TRACE_AGENT_MEMORY_STORE=json`` is advertised as a
  one-variable rollback, and that promise now spans vectors too. Both adapters
  pack through the same float32 helpers, so a cosine computed on SQLite and on
  the JSON sidecar agrees bit for bit - asserted here on ids, cosines and final
  scores, not merely on ordering.
* **Recipe identity.** A corpus embedded with one model must never be compared
  against a query embedded with another. Changing ``dim`` changes the model name,
  which marks every stored vector stale and forces a rebuild rather than a quiet
  mix of incomparable widths.
* **Graceful degradation.** No provider, or a provider that raises, costs
  semantic recall and nothing else. The claim under test is that the analysis
  run still gets its lexical hits.
* **Weight switch is earned.** The reranker moves 35% of a score onto the
  semantic term only once at least one candidate actually carries a cosine. An
  unembedded corpus must score *identically* to a lexical-only one, so adopting
  the semantic profile is never a silent reshuffle of recall.

Cosine magnitudes are asserted against thresholds the offline recipe was
measured to clear (paraphrase >= 0.4, unrelated <= 0.05), never against exact
values: the recipe is deterministic but its absolute scale is an implementation
detail, whereas "paraphrase above the floor, unrelated below it" is the contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trace_agent.memory.embedding.factory import EmbeddingSetup
from trace_agent.memory.embedding.local import HashedNgramEmbedder
from trace_agent.memory.embedding.ports import EmbeddingProvider, cosine
from trace_agent.memory.models import EpisodicMemory
from trace_agent.memory.ports import MemoryQuery
from trace_agent.memory.runtime import MemoryRuntime
from trace_agent.memory.store import JsonStore, SqliteStore

# Pinned so both adapters hold byte-identical timestamps. Freshness is a ranking
# signal, so letting each store stamp "now" would make the parity assertions
# drift by a few hundred microseconds of decay between the two fixtures.
FIXED = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

# Three cold-start-scoped episodes. Chinese text throughout: the whole point of
# P3 is that a paraphrase the tokenizer cannot align ("启动白屏问题排查" vs a stored
# "冷启动白屏") still clears the semantic floor.
COLD_CORPUS: tuple[tuple[str, str, str, str, str], ...] = (
    ("ep-cold", "run-cold", "冷启动白屏", "应用冷启动后长时间白屏",
     "冷启动阶段主线程同步读取配置文件，造成白屏"),
    ("ep-jank", "run-jank", "列表滑动掉帧", "滑动列表时明显卡顿",
     "布局层级过深导致主线程过度绘制"),
    ("ep-leak", "run-leak", "内存泄漏", "长时间使用后内存持续增长",
     "位图缓存无上限且从不淘汰"),
)

# Single white-screen episode, used where an unrelated query must find nothing.
WHITE_SCREEN: tuple[tuple[str, str, str, str, str], ...] = (COLD_CORPUS[0],)

# How many vectors a ``maintain()`` over ``COLD_CORPUS`` leaves behind: the three
# episodes plus the per-namespace project digest, which is a curated memory
# holding real recallable text and is embedded in the same beat that writes it.
# The digest is deterministic and idempotent (later beats write nothing), so the
# ``+ 1`` is stable rather than a function of how many times the beat ran.
CORPUS_VECTORS = len(COLD_CORPUS) + 1

DIM = 128


def _episode(
    episode_id: str,
    run_id: str,
    scenario: str,
    symptom: str,
    analysis: str,
    *,
    scenario_type: str = "cold_start",
) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=episode_id,
        run_id=run_id,
        trace_id="trace-1",
        scenario_type=scenario_type,
        scenario=scenario,
        symptom=symptom,
        summary="main thread blocked",
        root_causes=[analysis],
        findings=[
            {
                "title": "主线程同步IO",
                "severity": "high",
                "status": "confirmed",
                "confidence": 0.8,
                "analysis": analysis,
                "recommendation": "异步加载",
                "evidence_ids": ["ev-1"],
            }
        ],
        created_at=FIXED,
    )


def _seed(
    store: object,
    rows: Sequence[tuple[str, str, str, str, str]] = COLD_CORPUS,
) -> None:
    for row in rows:
        store.write_episode(_episode(*row))


def _runtime(
    store_cls: type,
    root: Path,
    provider: EmbeddingProvider | None,
    rows: Sequence[tuple[str, str, str, str, str]] = COLD_CORPUS,
) -> MemoryRuntime:
    store = store_cls(root)
    _seed(store, rows)
    return MemoryRuntime(
        root,
        store=store,
        embedding=EmbeddingSetup(provider=provider, kind="local"),
    )


def _local(dim: int = DIM) -> HashedNgramEmbedder:
    return HashedNgramEmbedder(dim=dim)


def _ident(hit: object) -> str:
    memory = getattr(hit, "memory", None)
    return str(
        getattr(memory, "episode_id", None) or getattr(memory, "memory_id", "")
    )


def _recall_keys(result: object) -> list[tuple[str, float, float]]:
    """Comparable identity: which records, at what score, on what cosine."""
    return [
        (_ident(hit), round(float(hit.score), 9), round(float(hit.semantic), 9))
        for hit in result.hits
    ]


class _BoomProvider:
    """A provider that is present but always fails, like a dead HTTP endpoint.

    ``EmbeddingProvider`` is documented never to raise; this deliberately breaks
    that contract to prove the callers enforce it anyway. A memory failure must
    cost semantic recall, never an analysis run.
    """

    name = "boom"
    model = "boom-v1"
    dim = 8

    def embed(self, text: str) -> list[float] | None:
        raise RuntimeError("network down")

    def embed_batch(self, texts: Sequence[str]) -> list[list[float] | None]:
        raise RuntimeError("network down")


# -------------------------------------------------------------------- backfill


def test_maintain_backfills_the_corpus(tmp_path: Path) -> None:
    """The dreaming beat, not recall, is what pays the embedding cost."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local())
    cov = runtime.semantic.coverage()
    assert cov["episodes_embedded"] == 0
    assert runtime.semantic.available is True

    batch = runtime.dreaming.maintain()

    assert batch.embedded == CORPUS_VECTORS
    cov = runtime.semantic.coverage()
    assert cov["episodes_embedded"] == len(COLD_CORPUS)
    assert cov["unembedded"] == 0
    assert cov["stale"] == 0


def test_backfill_respects_the_per_beat_limit(tmp_path: Path) -> None:
    """Bounded because the beat is on a timer; it converges over ticks."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local())

    assert runtime.semantic.backfill(limit=1) == 1
    assert runtime.semantic.coverage()["episodes_embedded"] == 1
    # A second beat picks up where the first stopped.
    assert runtime.semantic.backfill(limit=1) == 1
    assert runtime.semantic.coverage()["episodes_embedded"] == 2


def test_coverage_reports_embedding_state(tmp_path: Path) -> None:
    """The operational read behind 'does semantic recall work here'."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local())
    runtime.dreaming.maintain()
    cov = runtime.semantic.coverage()
    for key in (
        "available", "kind", "model", "dim", "vector_store", "last_error",
        "memories", "memories_embedded", "episodes", "episodes_embedded",
        "models", "unembedded", "stale", "cache",
    ):
        assert key in cov
    assert cov["model"] == f"hashed-cjk-v1:d{DIM}"
    assert cov["dim"] == DIM
    assert cov["models"] == {f"hashed-cjk-v1:d{DIM}": CORPUS_VECTORS}


# ------------------------------------------------------------ paraphrase recall


def test_paraphrase_recalls_after_backfill(tmp_path: Path) -> None:
    """The P3 acceptance query set: reworded Chinese still clears the floor."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local())
    runtime.dreaming.maintain()

    for query in ("冷启动 白屏", "启动白屏问题排查", "应用冷启动后长时间白屏怎么排查"):
        result = runtime.recall(query, scenario_type="cold_start")
        assert result.hits, query
        top = result.hits[0]
        assert _ident(top) == "ep-cold", query
        assert top.semantic > 0.0, query
        # Measured floor: these paraphrases land ~0.4-0.66 against the recipe.
        assert top.semantic >= 0.4, query


def test_unrelated_query_finds_nothing(tmp_path: Path) -> None:
    """Neither channel may pull an unrelated record in on trust or recency."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local(), rows=WHITE_SCREEN)
    runtime.dreaming.maintain()

    result = runtime.recall("帧率卡顿掉帧", scenario_type="cold_start")
    assert result.hits == []


def test_semantic_beats_pure_lexical_on_rewording(tmp_path: Path) -> None:
    """A near-paraphrase ranks above a loose single-token mention."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local())
    runtime.dreaming.maintain()

    close = runtime.recall("冷启动白屏", scenario_type="cold_start").hits[0]
    loose = runtime.recall("白屏", scenario_type="cold_start").hits[0]
    assert _ident(close) == _ident(loose) == "ep-cold"
    assert close.semantic > loose.semantic


# ------------------------------------------------------------- recipe identity


def test_model_change_marks_corpus_stale_then_rebuilds(tmp_path: Path) -> None:
    """A different ``dim`` is a different recipe; old vectors must not be reused."""
    root = tmp_path / "memory"
    store = SqliteStore(root)
    _seed(store)

    first = MemoryRuntime(
        root, store=store,
        embedding=EmbeddingSetup(provider=_local(dim=128), kind="local"),
    )
    first.dreaming.maintain()
    assert first.semantic.coverage()["stale"] == 0

    # Same store, new recipe. Nothing is embedded under the new model yet, and
    # every existing vector is now stale (produced by a model that is not current).
    second = MemoryRuntime(
        root, store=store,
        embedding=EmbeddingSetup(provider=_local(dim=64), kind="local"),
    )
    assert second.semantic.model == "hashed-cjk-v1:d64"
    cov = second.semantic.coverage()
    assert cov["stale"] == CORPUS_VECTORS
    assert len(list(store.pending_embeddings(model=second.semantic.model, limit=50))) == CORPUS_VECTORS

    assert second.semantic.rebuild() == CORPUS_VECTORS
    cov = second.semantic.coverage()
    assert cov["stale"] == 0
    assert cov["unembedded"] == 0
    assert cov["models"] == {"hashed-cjk-v1:d64": CORPUS_VECTORS}


def test_prune_stale_drops_other_models(tmp_path: Path) -> None:
    runtime = _runtime(SqliteStore, tmp_path / "memory", _local(dim=128))
    runtime.dreaming.maintain()
    # A second recipe over the same store, without rebuilding.
    other = MemoryRuntime(
        tmp_path / "memory",
        store=runtime.store,
        embedding=EmbeddingSetup(provider=_local(dim=96), kind="local"),
    )
    assert other.semantic.coverage()["stale"] == CORPUS_VECTORS
    assert other.semantic.prune_stale() == CORPUS_VECTORS
    assert other.semantic.coverage()["stale"] == 0
    assert other.semantic.coverage()["episodes_embedded"] == 0


# ------------------------------------------------------------------ degradation


def test_semantic_degrades_without_a_provider(tmp_path: Path) -> None:
    """No provider => not available => recall is exactly the lexical recall."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", None)
    assert runtime.semantic.available is False
    assert runtime.semantic.query_vector("冷启动白屏") is None

    runtime.dreaming.maintain()  # no-op, must not raise
    result = runtime.recall("冷启动白屏", scenario_type="cold_start")
    assert result.hits
    assert all(hit.semantic == 0.0 for hit in result.hits)


def test_raising_provider_degrades_to_lexical(tmp_path: Path) -> None:
    """A provider that breaks its never-raise contract still cannot break recall."""
    runtime = _runtime(SqliteStore, tmp_path / "memory", _BoomProvider())
    # Provider exists and the store holds vectors, so the index reports available;
    # the failure surfaces per-call, swallowed, as "no vector".
    assert runtime.semantic.available is True
    assert runtime.semantic.query_vector("冷启动白屏") is None

    batch = runtime.dreaming.maintain()  # must not raise
    assert batch.embedded == 0
    result = runtime.recall("冷启动白屏", scenario_type="cold_start")
    assert result.hits
    assert all(hit.semantic == 0.0 for hit in result.hits)


# --------------------------------------------------------------- weight switch


def test_unembedded_corpus_scores_identically_to_lexical_only(tmp_path: Path) -> None:
    """The semantic profile is earned, not assumed.

    Before any vector exists, a semantic-capable runtime must produce the same
    scores as one with the provider switched off - otherwise adopting P3 would
    silently reshuffle recall for a corpus that has not been backfilled yet.
    """
    root = tmp_path / "memory"
    store = SqliteStore(root)
    _seed(store)

    semantic_runtime = MemoryRuntime(
        root, store=store,
        embedding=EmbeddingSetup(provider=_local(), kind="local"),
    )
    before = semantic_runtime.recall("冷启动白屏", scenario_type="cold_start")

    lexical_runtime = MemoryRuntime(
        root, store=store, embedding=EmbeddingSetup(provider=None, kind="off")
    )
    lexical_only = lexical_runtime.recall("冷启动白屏", scenario_type="cold_start")

    assert _recall_keys(before) == _recall_keys(lexical_only)
    assert all(hit.semantic == 0.0 for hit in before.hits)

    # After backfill the same query carries a cosine and the profile switches.
    semantic_runtime.dreaming.maintain()
    after = semantic_runtime.recall("冷启动白屏", scenario_type="cold_start")
    assert after.hits[0].semantic > 0.0


# ------------------------------------------------------------- adapter parity


@pytest.mark.parametrize("query", ["冷启动白屏", "启动白屏问题排查", "白屏", "帧率卡顿掉帧"])
def test_vector_search_parity_across_adapters(tmp_path: Path, query: str) -> None:
    """The rollback promise, extended to vectors: same ids, same cosines."""
    vec_results: dict[str, list[tuple[str, float]]] = {}
    for name, store_cls in (("json", JsonStore), ("sqlite", SqliteStore)):
        runtime = _runtime(store_cls, tmp_path / f"memory-{name}", _local())
        runtime.dreaming.maintain()
        vector = runtime.semantic.query_vector(query)
        assert vector is not None
        hits = runtime.semantic.candidates(
            vector, MemoryQuery(text=query, scenario_type="cold_start", limit=50)
        )
        vec_results[name] = [
            (_ident_from_memory(h.memory), round(float(h.similarity or 0.0), 9))
            for h in hits
        ]
    assert vec_results["json"] == vec_results["sqlite"]


@pytest.mark.parametrize("query", ["冷启动白屏", "启动白屏问题排查", "白屏"])
def test_recall_parity_across_adapters_with_semantics(tmp_path: Path, query: str) -> None:
    """End-to-end recall - scores included - is identical on both adapters."""
    keys: dict[str, list[tuple[str, float, float]]] = {}
    for name, store_cls in (("json", JsonStore), ("sqlite", SqliteStore)):
        runtime = _runtime(store_cls, tmp_path / f"memory-{name}", _local())
        runtime.dreaming.maintain()
        keys[name] = _recall_keys(runtime.recall(query, scenario_type="cold_start"))
    assert keys["json"] == keys["sqlite"]


def _ident_from_memory(memory: object) -> str:
    return str(
        getattr(memory, "episode_id", None) or getattr(memory, "memory_id", "")
    )


# --------------------------------------------------------------- cosine sanity


def test_offline_recipe_separates_paraphrase_from_unrelated() -> None:
    """The provider alone, without a store: paraphrase close, unrelated far."""
    embedder = _local()
    base = embedder.embed("冷启动阶段主线程同步读取配置文件造成白屏")
    assert base is not None

    for paraphrase in ("冷启动白屏", "启动白屏问题排查", "白屏", "blank screen on cold start"):
        assert cosine(base, embedder.embed(paraphrase)) >= 0.35, paraphrase

    for unrelated in ("帧率卡顿掉帧", "GPU 进程崩溃未上报帧", "内存泄漏 OOM 崩溃"):
        assert cosine(base, embedder.embed(unrelated)) <= 0.10, unrelated
