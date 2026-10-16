"""Semantic half of memory retrieval: providers, index, and the wiring between them.

Kept as a package rather than one module because the three concerns have very
different failure modes. A provider may be offline, unreachable or
misconfigured; the index must survive all three by degrading to "no semantic
signal", which recall already handles. Nothing here imports
:mod:`trace_agent.memory.recall` or the stores, so the dependency only ever
points one way.
"""

from __future__ import annotations

from trace_agent.memory.embedding.cache import EmbeddingCache
from trace_agent.memory.embedding.factory import (
    EmbeddingSetup,
    build_embedding_provider,
)
from trace_agent.memory.embedding.index import SemanticIndex
from trace_agent.memory.embedding.local import HashedNgramEmbedder
from trace_agent.memory.embedding.ports import (
    EmbeddingProvider,
    Vector,
    cosine,
    pack_vector,
    round_trip,
    unpack_vector,
)
from trace_agent.memory.embedding.remote import HttpEmbeddingProvider

__all__ = [
    "EmbeddingCache",
    "EmbeddingProvider",
    "EmbeddingSetup",
    "HashedNgramEmbedder",
    "HttpEmbeddingProvider",
    "SemanticIndex",
    "Vector",
    "build_embedding_provider",
    "cosine",
    "pack_vector",
    "round_trip",
    "unpack_vector",
]
