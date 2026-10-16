"""In-process cache for query embeddings.

Corpus vectors are already persisted - they live in the store's ``embedding``
column, which is the cache that matters, since a memory is embedded once and
compared many times. What is not persisted is the *query* side: every recall
needs a vector for the incoming text, and with a remote provider that is one
network round trip on the critical path of an analysis run.

The same query recurs constantly in practice (one run recalls several times with
the same symptom text, and the dashboard re-asks on every refresh), so a small
LRU removes almost all of those calls. It is per process and deliberately not
written to disk: a stale on-disk query cache survives a model change, and
correctness there would depend on invalidation logic whose only job is to
reproduce what ``dict`` already does for free.

Failures are never cached. A provider that returned ``None`` because a gateway
timed out must be retried on the next recall, not remembered as "this text has
no vector".
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha1
from typing import Any

from trace_agent.memory.embedding.ports import Vector

DEFAULT_MAXSIZE = 512


class EmbeddingCache:
    """Bounded LRU keyed by ``(model, text)``."""

    def __init__(self, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self.maxsize = max(1, int(maxsize))
        self._entries: OrderedDict[str, Vector] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(model: str, text: str) -> str:
        """Cache key. The model is part of it because vectors are not comparable
        across recipes, and a key without it would serve a stale-model vector to
        a new provider."""
        digest = sha1((text or "").encode("utf-8")).hexdigest()
        return f"{model or 'unknown'}:{digest}"

    def get(self, model: str, text: str) -> Vector | None:
        key = self.key(model, text)
        cached = self._entries.get(key)
        if cached is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return list(cached)

    def put(self, model: str, text: str, vector: Vector | None) -> None:
        if not vector:
            return
        key = self.key(model, text)
        self._entries[key] = list(vector)
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict[str, Any]:
        """Counts for ``memory stats`` and the dashboard."""
        total = self.hits + self.misses
        return {
            "size": len(self._entries),
            "maxsize": self.maxsize,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }

    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["DEFAULT_MAXSIZE", "EmbeddingCache"]
