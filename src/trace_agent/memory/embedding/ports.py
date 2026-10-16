"""Contracts and value objects for the semantic half of memory retrieval.

Section 9 of ``docs/memory-optimization-plan.md`` asks for hybrid recall:
lexical and vector candidates merged, then reranked. The lexical half already
exists (:mod:`trace_agent.memory.rerank`). This module defines the seam the
vector half plugs into, and it is deliberately narrow:

* An :class:`EmbeddingProvider` turns text into a vector or admits failure by
  returning ``None``. It never raises. Recall is allowed to degrade to
  lexical-only - that is the plan's acceptance criterion - and a provider that
  threw on a network timeout would turn "no embeddings today" into "no memory
  today".
* Vectors cross the store boundary as ``list[float]`` and are stored packed as
  float32. Both adapters pack through the same helpers, so a cosine computed on
  SQLite and on the JSON sidecar agrees bit for bit instead of approximately.

``dim`` is a property of the provider, not of the caller: a corpus embedded with
one dimensionality cannot be compared against a query embedded with another,
which is why the model name is stored next to every vector and why changing it
triggers a rebuild rather than a silent mix.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

Vector = list[float]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that can turn memory text into a comparable vector.

    Two implementations ship: a deterministic offline hasher
    (:mod:`trace_agent.memory.embedding.local`) and an OpenAI-compatible HTTP
    client (:mod:`trace_agent.memory.embedding.remote`). Both return ``None``
    instead of raising, so callers have exactly one failure mode to handle.
    """

    #: Stable identity of the recipe. Stored beside every vector; a change here
    #: invalidates the corpus and triggers a rebuild.
    model: str

    #: Vector length, or 0 when the provider learns it from the first response.
    dim: int

    def embed(self, text: str) -> Vector | None:
        """One vector, or ``None`` when it could not be produced."""
        ...

    def embed_batch(self, texts: Sequence[str]) -> list[Vector | None]:
        """Vectors parallel to ``texts``; individual entries may be ``None``."""
        ...


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity clamped to ``[0, 1]``.

    Clamping the negative half is a modelling choice, not a convenience: memory
    vectors are bags of symptom/domain features, where "opposite" is not a
    meaningful relation and a negative score would only let an unrelated record
    subtract itself out of a weighted sum. Zero means "no shared signal", which
    is what the reranker's other components already assume.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=False):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    value = dot / math.sqrt(left_norm * right_norm)
    if value != value:  # NaN guard: a corrupt blob must score 0, not propagate
        return 0.0
    return min(1.0, max(0.0, value))


def pack_vector(vector: Sequence[float]) -> bytes:
    """Serialise to little-endian float32, the width pgvector also uses."""
    return struct.pack(f"<{len(vector)}f", *[float(item) for item in vector])


def unpack_vector(blob: Any) -> Vector:
    """Inverse of :func:`pack_vector`; returns ``[]`` for anything unusable.

    Tolerant on purpose. A vector column is derived data: if a blob is truncated
    by a partial write or was stored at another dimensionality, the correct
    response is to treat that record as unembedded and let the next backfill
    regenerate it, not to fail the recall that happened to touch it.
    """
    if not blob:
        return []
    if isinstance(blob, (bytes, bytearray, memoryview)):
        raw = bytes(blob)
        if len(raw) % 4 != 0:
            return []
        try:
            return list(struct.unpack(f"<{len(raw) // 4}f", raw))
        except struct.error:  # pragma: no cover - defensive
            return []
    if isinstance(blob, str):
        # SQLite may hand back TEXT for a BLOB column when a driver coerced it.
        try:
            raw = blob.encode("latin-1")
        except UnicodeEncodeError:  # pragma: no cover - defensive
            return []
        return unpack_vector(raw)
    if isinstance(blob, Sequence):
        try:
            return [float(item) for item in blob]
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return []
    return []


def round_trip(vector: Sequence[float]) -> Vector:
    """Quantise to float32 without packing, so text stores match binary ones.

    The JSON adapter keeps vectors in a sidecar file as numbers. Storing the
    float64 the provider returned would make its cosine differ in the last bits
    from the SQLite adapter's, and "both adapters return the same hits" would
    stop being a testable property. Rounding both sides to the stored width
    keeps it one.
    """
    return unpack_vector(pack_vector(vector))


__all__ = [
    "EmbeddingProvider",
    "Vector",
    "cosine",
    "pack_vector",
    "round_trip",
    "unpack_vector",
]
