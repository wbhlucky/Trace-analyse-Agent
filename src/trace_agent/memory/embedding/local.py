"""Deterministic offline embedder: hashed n-grams plus domain concepts.

The plan's P3 asks for vector recall whose acceptance test is a paraphrase query
set, and for graceful degradation when embeddings are unavailable. Shipping only
a network-backed provider would make the first true and the second untestable,
so the default provider is this one: no dependencies, no I/O, same text in -
same vector out, on every platform.

What it is good at, and why that is more than the lexical score already does:

* **Order and length insensitivity with graded overlap.** Coverage in
  :func:`trace_agent.memory.rerank.lexical_score` is a ratio over query tokens,
  so a query that adds one word drops every stored record's score together. A
  normalised bag with sublinear term frequency scores partial overlap by how
  much signal is shared, which ranks a near-paraphrase above a loose mention.
* **Cross-lingual and synonym matching**, via the concept features in
  :mod:`trace_agent.memory.embedding.lexicon`. ``blank screen`` and ``白屏``
  share no characters, so no n-gram recipe can connect them; one concept token
  each does.
* **Character-level CJK features.** Bigrams alone make a one-character edit
  destroy two features, so single characters carry a smaller weight alongside
  them. That is what keeps "启动白屏" and "启动后白屏" close.

What it is not: a language model. Two records that describe the same failure in
genuinely different words and different scripts will still be far apart unless
the lexicon covers the pair. Configuring ``TRACE_AGENT_MEMORY_EMBEDDING=http``
replaces this provider with a real model and nothing else changes.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from hashlib import blake2b

from trace_agent.memory.embedding.lexicon import concepts_in
from trace_agent.memory.embedding.ports import Vector
from trace_agent.memory.text import cjk_runs, tokenize

# Bumped whenever the feature recipe changes. Stored vectors record it, and a
# mismatch means the corpus is rebuilt rather than compared against vectors that
# were computed from different features - the failure mode where recall quietly
# gets worse and nothing looks broken.
RECIPE = "hashed-cjk-v1"

DEFAULT_DIM = 256
_MIN_DIM = 16
_MAX_DIM = 4096

# Feature-type weights. Concepts dominate because one concept token is the only
# evidence connecting two different scripts; characters are discounted because
# they are the noisiest feature and exist to soften bigram brittleness.
_TOKEN_WEIGHT = 1.0
_UNIGRAM_WEIGHT = 0.5
_CONCEPT_WEIGHT = 2.0


def _tf(count: float) -> float:
    """Sublinear term frequency: repeated evidence counts, but with a ceiling.

    A memory that says "白屏" five times is not five times more about white
    screens than one that says it once, and linear counts would let a long
    record's most repeated word dominate every cosine it participates in.
    """
    return 1.0 + math.log(count) if count > 1 else 1.0


def _hash_feature(feature: str, dim: int) -> tuple[int, float]:
    """Signed feature hashing: which dimension, and in which direction.

    The sign is what makes collisions tolerable. Two features landing on one
    dimension add noise when their signs differ and only reinforce when they
    agree, so expected error grows far slower than it would if every collision
    accumulated in the same direction.
    """
    digest = blake2b(feature.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "little") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


class HashedNgramEmbedder:
    """Offline :class:`~trace_agent.memory.embedding.ports.EmbeddingProvider`."""

    name = "local"

    def __init__(self, *, dim: int = DEFAULT_DIM, model: str | None = None) -> None:
        # Clamped rather than raising: this is built from an environment
        # variable, and a typo there must not stop memory from working.
        self.dim = min(_MAX_DIM, max(_MIN_DIM, int(dim)))
        self.model = model or f"{RECIPE}:d{self.dim}"

    def features(self, text: str) -> dict[str, float]:
        """Weighted feature bag before hashing; exposed for diagnostics.

        Public because "why did these two records score 0.4" is otherwise
        unanswerable: the vector is a lossy projection, and the features are the
        actual evidence.
        """
        if not text:
            return {}
        features: dict[str, float] = {}
        for token, count in Counter(tokenize(text)).items():
            features[token] = features.get(token, 0.0) + _TOKEN_WEIGHT * _tf(count)
        characters = Counter(
            character for run in cjk_runs(text) for character in run.lower()
        )
        for character, count in characters.items():
            features[character] = (
                features.get(character, 0.0) + _UNIGRAM_WEIGHT * _tf(count)
            )
        for concept in concepts_in(text):
            key = f"@{concept}"
            features[key] = features.get(key, 0.0) + _CONCEPT_WEIGHT
        return features

    def embed(self, text: str) -> Vector | None:
        """Project ``text`` onto ``dim`` signed buckets, then L2-normalise."""
        features = self.features(text)
        if not features:
            # No features means no opinion. Returning a zero vector instead would
            # look like an embedding that matches nothing, and a backfill would
            # store it and never retry.
            return None
        vector = [0.0] * self.dim
        for feature, weight in features.items():
            index, sign = _hash_feature(feature, self.dim)
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0.0:
            return None
        return [value / norm for value in vector]

    def embed_batch(self, texts: Sequence[str]) -> list[Vector | None]:
        return [self.embed(text) for text in texts]

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"HashedNgramEmbedder(model={self.model!r}, dim={self.dim})"


__all__ = ["DEFAULT_DIM", "HashedNgramEmbedder", "RECIPE"]
