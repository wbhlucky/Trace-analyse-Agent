"""Vendor-neutral LLM runtime contracts.

The application and agent layers depend on these protocols only.  A concrete
SDK adapter supplies the :class:`ModelPolicy` (and, when relevant, the BYOK
catalog mapping) instead of sharing SDK-specific configuration here.
"""

from __future__ import annotations

from typing import Any, Protocol

from trace_agent.models import LlmProvider


class ModelPolicy(Protocol):
    """Resolves a provider-agnostic model request into adapter wire config."""

    provider: LlmProvider
    model: str
    api_key: str
    base_url: str | None

    def resolve(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the wire-level model configuration for one LLM turn."""
        ...


class ModelCatalog(Protocol):
    """Provider-specific mapping from human model names to catalog IDs."""

    def normalize_model(self, provider: LlmProvider, model: str) -> str:
        """Translate a human-friendly model name to the provider catalog ID."""
        ...

    def is_native_provider(self, provider: LlmProvider) -> bool:
        """Return True when the provider needs no custom base URL."""
        ...


class LlmRuntimeConfig(Protocol):
    """Provider-agnostic resolved runtime configuration."""

    provider: LlmProvider
    model: str
    base_url: str
    api_key: str
    model_policy: ModelPolicy
