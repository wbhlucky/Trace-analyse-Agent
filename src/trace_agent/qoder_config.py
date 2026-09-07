"""Qoder provider-specific BYOK configuration and auth policy.

Kept out of :mod:`trace_agent.config` so the shared runtime config remains
provider-agnostic. A different SDK provider supplies its own mapping/auth
policy instead of extending this module.
"""

from __future__ import annotations

import os

from trace_agent.llm.ports import ModelCatalog
from trace_agent.models import LlmProvider

QODER_TOKEN_ENV = "QODER_PERSONAL_ACCESS_TOKEN"

# Qoder BYOK providers that the CLI already routes natively (fixed endpoint).
_NATIVE_BYOK_PROVIDERS = {"deepseek", "bailian"}

# Human-friendly model names accepted in .env -> Qoder BYOK catalog IDs.
_BAILIAN_DEEPSEEK_MODEL_IDS = {
    "deepseek-v4-pro": "deepseek-v4-pro-pg",
    "deepseek-v4-flash": "deepseek-v4-flash-pg",
    "deepseek-v4-pro-tp": "deepseek-v4-pro-tp",
    "deepseek-v4-flash-tp": "deepseek-v4-flash-tp",
}

_DEEPSEEK_MODEL_IDS = {
    "deepseek-v4-pro": "deepseek-v4-pro-pg",
    "deepseek-v4-pro[1m]": "deepseek-v4-pro-pg",
    "deepseek-v4-flash": "deepseek-v4-flash-pg",
}


class QoderModelCatalog:
    """Qoder BYOK catalog mapping used by the Qoder adapter."""

    def normalize_model(
        self,
        provider: LlmProvider,
        model: str,
    ) -> str:
        if provider is LlmProvider.BAILIAN:
            return _BAILIAN_DEEPSEEK_MODEL_IDS.get(model, model)
        if provider is LlmProvider.DEEPSEEK:
            return _DEEPSEEK_MODEL_IDS.get(model, model)
        return model

    def is_native_provider(self, provider: LlmProvider) -> bool:
        return provider.value in _NATIVE_BYOK_PROVIDERS


def normalize_qoder_byok_model(provider: LlmProvider, model: str) -> str:
    """Translate human-friendly model names to Qoder's catalog IDs."""
    return QODER_MODEL_CATALOG.normalize_model(provider, model)


def is_native_byok_provider(provider: LlmProvider) -> bool:
    """True when Qoder routes this provider natively (no custom url)."""
    return QODER_MODEL_CATALOG.is_native_provider(provider)


def qoder_personal_access_token() -> str | None:
    """Read the Qoder PAT exactly once, from the process environment."""
    token = os.environ.get(QODER_TOKEN_ENV)
    return token or None


QODER_MODEL_CATALOG: ModelCatalog = QoderModelCatalog()
