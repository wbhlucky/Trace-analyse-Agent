"""Choosing an embedding provider from configuration.

One environment variable decides whether semantic recall exists at all, and the
answer has to travel with a *reason*. "Semantic recall is off" is not an
actionable message; "you asked for ``http`` but never set a base URL" is, and
without the reason the operator's only way to find out is to read this file.

The factory never raises. Memory is best-effort by contract, and a typo in
``TRACE_AGENT_MEMORY_EMBEDDING_DIM`` must not turn into a failed analysis run -
it must turn into lexical-only recall plus a line in ``memory stats``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trace_agent.memory.flags import (
    MEMORY_EMBEDDING_HTTP,
    MEMORY_EMBEDDING_LOCAL,
    MEMORY_EMBEDDING_OFF,
    memory_embedding_api_key,
    memory_embedding_base_url,
    memory_embedding_batch_size,
    memory_embedding_dim,
    memory_embedding_kind,
    memory_embedding_model,
    memory_embedding_timeout,
)
from trace_agent.memory.embedding.local import HashedNgramEmbedder
from trace_agent.memory.embedding.ports import EmbeddingProvider
from trace_agent.memory.embedding.remote import HttpEmbeddingProvider


@dataclass(frozen=True, slots=True)
class EmbeddingSetup:
    """The provider that was built, and why - or why not.

    ``kind`` records what was *asked for* and ``reason`` explains any gap between
    that and what was built, so a requested-but-unavailable provider is
    distinguishable from one that was never requested. ``memory stats`` prints
    both; conflating them is how "semantic recall silently stopped working"
    becomes a week-long mystery.
    """

    provider: EmbeddingProvider | None = None
    kind: str = MEMORY_EMBEDDING_OFF
    reason: str = ""

    @property
    def available(self) -> bool:
        """Whether a usable provider exists.

        The only gate callers need: everything downstream of it treats a missing
        provider as "lexical-only", which is a supported configuration rather
        than an error.
        """
        return self.provider is not None

    @property
    def model(self) -> str:
        return getattr(self.provider, "model", "") or ""

    @property
    def dim(self) -> int:
        return int(getattr(self.provider, "dim", 0) or 0)

    def describe(self) -> dict[str, Any]:
        """JSON-safe summary for ``memory stats`` and the dashboard."""
        return {
            "kind": self.kind,
            "available": self.available,
            "model": self.model,
            "dim": self.dim,
            "reason": self.reason,
        }


OFF = EmbeddingSetup(provider=None, kind=MEMORY_EMBEDDING_OFF, reason="disabled")


def build_embedding_provider(*, kind: str | None = None) -> EmbeddingSetup:
    """Build the configured provider, degrading to ``None`` on any problem.

    ``local`` is the default and is built unconditionally: it is offline,
    deterministic and dependency-free, so semantic recall is a property of the
    product rather than of whether somebody configured a key. ``http`` needs a
    base URL, and asking for it without one is a configuration mistake the
    caller is told about rather than a silent fallback to the offline recipe -
    falling back quietly would produce vectors from a different model than the
    operator thinks, which is worse than producing none.
    """
    resolved = (kind or memory_embedding_kind()).strip().lower()

    if resolved == MEMORY_EMBEDDING_OFF:
        return OFF

    if resolved == MEMORY_EMBEDDING_LOCAL:
        try:
            return EmbeddingSetup(
                provider=HashedNgramEmbedder(dim=memory_embedding_dim()),
                kind=MEMORY_EMBEDDING_LOCAL,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return EmbeddingSetup(
                provider=None,
                kind=MEMORY_EMBEDDING_LOCAL,
                reason=f"local embedder failed to build: {exc}",
            )

    if resolved == MEMORY_EMBEDDING_HTTP:
        base_url = memory_embedding_base_url()
        if not base_url:
            return EmbeddingSetup(
                provider=None,
                kind=MEMORY_EMBEDDING_HTTP,
                reason="TRACE_AGENT_MEMORY_EMBEDDING_BASE_URL is not set",
            )
        try:
            return EmbeddingSetup(
                provider=HttpEmbeddingProvider(
                    base_url=base_url,
                    api_key=memory_embedding_api_key(),
                    model=memory_embedding_model(),
                    dim=memory_embedding_dim(),
                    timeout=memory_embedding_timeout(),
                    batch_size=memory_embedding_batch_size(),
                ),
                kind=MEMORY_EMBEDDING_HTTP,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return EmbeddingSetup(
                provider=None,
                kind=MEMORY_EMBEDDING_HTTP,
                reason=f"http embedder failed to build: {exc}",
            )

    # An unrecognised value is treated as "off" rather than as the default.
    # Guessing ``local`` here would mean an operator who typed ``openai`` gets
    # hashed n-gram vectors and no indication that their setting was ignored.
    return EmbeddingSetup(
        provider=None,
        kind=resolved or MEMORY_EMBEDDING_OFF,
        reason=f"unknown TRACE_AGENT_MEMORY_EMBEDDING value {resolved!r}",
    )


__all__ = ["EmbeddingSetup", "build_embedding_provider"]
