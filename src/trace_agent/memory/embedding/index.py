"""The seam between an embedding provider and a memory store.

Neither side knows about the other. A provider turns text into vectors and may
fail; a store holds vectors and may not support them at all. This class is the
only place that has to reason about both, and its whole job is to make "no
semantic recall right now" a normal state rather than an exceptional one:

* :attr:`available` is ``False`` when there is no provider *or* the store cannot
  hold vectors, and every method returns an empty answer in that case. Recall
  checks the flag once and otherwise behaves exactly as it did before P3.
* Query vectors go through an LRU. One analysis run recalls several times with
  the same symptom text, and with a remote provider each of those would be a
  network round trip on the critical path.
* Corpus vectors are *not* computed on demand. They are produced by
  :meth:`backfill`, which the dreaming beat calls, because embedding a thousand
  migrated records inside a recall would be indistinguishable from a hang.

Every method swallows provider and store errors. A dead embedding endpoint must
cost semantic recall, not memory.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from trace_agent.memory.embedding.cache import EmbeddingCache
from trace_agent.memory.embedding.ports import (
    EmbeddingProvider,
    Vector,
    cosine,
    round_trip,
)
from trace_agent.memory.flags import memory_embed_limit, memory_vector_limit
from trace_agent.memory.ports import (
    MemoryQuery,
    PendingEmbedding,
    ScoredMemory,
    store_supports_vectors,
)


class SemanticIndex:
    """Vector side of hybrid retrieval, over any vector-capable store."""

    def __init__(
        self,
        store: Any,
        provider: EmbeddingProvider | None = None,
        *,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._cache = cache if cache is not None else EmbeddingCache()
        # Probed once: whether a store can hold vectors is a property of its
        # class, and re-probing per call would only add a getattr in a hot path.
        self._vector_store = store_supports_vectors(store)

    # ------------------------------------------------------------------ status
    @property
    def available(self) -> bool:
        """Both halves present: a provider that can embed and a store that can hold.

        Half a semantic index is worse than none. Embedding with nowhere to put
        the vectors burns provider calls on every beat; holding vectors with no
        provider means a corpus nobody can query against.
        """
        return self._provider is not None and self._vector_store

    @property
    def model(self) -> str:
        """Identity of the recipe currently in force.

        Stored beside every vector. Comparing a query embedded with one model
        against a corpus embedded with another yields meaningless cosines, so the
        name travels with the value and a change triggers a rebuild.
        """
        return getattr(self._provider, "model", "") or ""

    @property
    def dim(self) -> int:
        """Vector width, or 0 when the provider learns it from its first response."""
        return int(getattr(self._provider, "dim", 0) or 0)

    @property
    def provider_name(self) -> str:
        return str(getattr(self._provider, "name", "") or "")

    @property
    def last_error(self) -> str | None:
        """Why the remote provider last failed, when it did.

        Surfaced by ``memory stats``. Without it, an expired key looks identical
        to a working setup that simply has nothing semantically similar.
        """
        return getattr(self._provider, "last_error", None)

    # ------------------------------------------------------------------- query
    def query_vector(self, text: str) -> Vector | None:
        """Vector for one query, cached; ``None`` when semantics are unavailable.

        Rounded to the stored float32 width. The corpus vectors come back out of
        the store already quantised, and a float64 query compared against them
        would differ in the last bits from the same query compared on the other
        adapter - which is exactly the parity the two stores are supposed to have.
        """
        if not self.available or not (text or "").strip():
            return None
        model = self.model
        cached = self._cache.get(model, text)
        if cached is not None:
            return cached
        try:
            vector = self._provider.embed(text)
        except Exception:
            # A provider is documented never to raise; belt and braces, because
            # the cost of being wrong here is a failed analysis run.
            vector = None
        if not vector:
            # Failures are deliberately not cached: a gateway that timed out
            # must be retried on the next recall, not remembered as "this text
            # has no vector".
            return None
        quantised = round_trip(vector)
        self._cache.put(model, text, quantised)
        return quantised

    # --------------------------------------------------------------- retrieval
    def candidates(
        self, vector: Sequence[float] | None, query: MemoryQuery
    ) -> list[ScoredMemory]:
        """Nearest records to ``vector``, already filtered by the query's scope.

        Bounded by ``memory_vector_limit()`` rather than ``query.limit``: the
        vector pool is merged into the lexical one, and letting each channel
        contribute the full candidate budget would double the reranker's work for
        hits that are near-certainly redundant.
        """
        if not self.available or not vector:
            return []
        capped = query.limit if query.limit > 0 else memory_vector_limit()
        narrowed = (
            query
            if capped <= memory_vector_limit()
            else MemoryQuery(
                text=query.text,
                namespace=query.namespace,
                scenario_type=query.scenario_type,
                statuses=query.statuses,
                types=query.types,
                include_episodic=query.include_episodic,
                limit=memory_vector_limit(),
            )
        )
        try:
            return list(self._store.vector_search(vector, narrowed))
        except Exception:
            return []

    def vectors_for(
        self, record_ids: Sequence[str], *, episode: bool = False
    ) -> dict[str, Vector]:
        """Stored vectors for a set of ids, keyed by id; missing ones are absent.

        Used to score the *lexical* candidate pool semantically as well. A record
        found by both channels must get one similarity, not two competing ones,
        and reading the stored vectors is how the merged pool stays consistent.
        """
        ids = [str(item) for item in record_ids if item]
        if not self.available or not ids:
            return {}
        try:
            found = self._store.vectors_for(ids, episode=episode)
        except Exception:
            return {}
        return {key: list(value) for key, value in (found or {}).items() if value}

    def similarity_for(
        self,
        query_vector: Sequence[float] | None,
        record_ids: Sequence[str],
        *,
        episode: bool = False,
    ) -> dict[str, float]:
        """Cosine per id, for ids the store has a vector for."""
        if not query_vector:
            return {}
        return {
            record_id: cosine(query_vector, vector)
            for record_id, vector in self.vectors_for(
                record_ids, episode=episode
            ).items()
        }

    # ------------------------------------------------------------------ corpus
    def backfill(self, *, limit: int | None = None) -> int:
        """Embed records that are missing a vector or carry a stale model.

        Returns how many were stored. The store decides what is pending - only it
        knows which rows lack an embedding or were written by another recipe - so
        a model change is a rebuild rather than a silent mix of incomparable
        vectors.

        Bounded by ``memory_embed_limit()`` because this runs inside a timer beat:
        the first pass over a migrated corpus must not turn one tick into
        thousands of provider calls. It converges over beats instead.
        """
        if not self.available:
            return 0
        cap = memory_embed_limit() if limit is None else max(1, int(limit))
        try:
            pending: list[PendingEmbedding] = list(
                self._store.pending_embeddings(model=self.model, limit=cap)
            )
        except Exception:
            return 0
        if not pending:
            return 0
        try:
            vectors = self._provider.embed_batch([item.text for item in pending])
        except Exception:
            return 0
        stored = 0
        for item, vector in zip(pending, vectors, strict=False):
            if not vector:
                continue
            try:
                wrote = self._store.set_embedding(
                    item.record_id,
                    round_trip(vector),
                    model=self.model,
                    episode=item.episode,
                )
            except Exception:
                continue
            if wrote:
                stored += 1
        return stored

    def rebuild(self, *, limit: int | None = None) -> int:
        """Drop every stored vector, then re-embed up to ``limit`` records.

        The recovery path after a recipe change: ``keep_model=""`` matches
        nothing, so every vector is cleared and the corpus is refilled with the
        current model's. Vectors of differing width compare as ``0.0`` rather
        than raising, so leaving the old ones in place would degrade ranking
        quietly instead of visibly.
        """
        if not self.available:
            return 0
        try:
            self._store.prune_embeddings(keep_model="")
        except Exception:
            return 0
        self._cache.clear()
        return self.backfill(limit=limit)

    def prune_stale(self) -> int:
        """Remove vectors written by a model other than the current one."""
        if not self.available:
            return 0
        try:
            return int(self._store.prune_embeddings(keep_model=self.model))
        except Exception:
            return 0

    # ----------------------------------------------------------- observability
    def coverage(self) -> dict[str, Any]:
        """What is embedded, with what, and what is still outstanding.

        Answers the operational question behind section 14's "does semantic
        recall actually work here": a corpus at 3% coverage explains a paraphrase
        query returning nothing far better than the query does.
        """
        payload: dict[str, Any] = {
            "available": self.available,
            "kind": self.provider_name,
            "model": self.model,
            "dim": self.dim,
            "vector_store": self._vector_store,
            "last_error": self.last_error,
        }
        payload["cache"] = self._cache.stats()
        if not self.available:
            return payload
        try:
            raw = dict(self._store.embedding_coverage() or {})
        except Exception:
            raw = {}
        payload.update(raw)
        models = {
            str(key): int(value)
            for key, value in (raw.get("models") or {}).items()
        }
        payload["models"] = models
        payload["stale"] = sum(
            count for name, count in models.items() if name != self.model
        )
        return payload

    def clear_cache(self) -> None:
        self._cache.clear()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"SemanticIndex(available={self.available}, kind={self.provider_name!r}, "
            f"model={self.model!r})"
        )


__all__ = ["SemanticIndex"]
