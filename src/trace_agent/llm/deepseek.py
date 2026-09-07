"""DeepSeek BYOK model policy (vendor-neutral core).

The concrete Qoder catalog mapping stays in :mod:`trace_agent.qoder_config`;
this module only models the shared DeepSeek BYOK wiring that any Anthropic-style
agent SDK can reuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trace_agent.models import LlmProvider

DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"


@dataclass(frozen=True, slots=True)
class DeepSeekByokPolicy:
    """Resolve a DeepSeek/Bailian BYOK request into SDK wire config."""

    provider: LlmProvider
    model: str
    api_key: str = field(repr=False)
    base_url: str | None = DEEPSEEK_ANTHROPIC_BASE_URL
    model_id: str | None = None

    def resolve(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        del context
        custom_model: dict[str, Any] = {
            "provider": self.provider.value,
            "model": self.model_id or self.model,
            "api_key": self.api_key,
        }
        if self.base_url:
            custom_model["url"] = self.base_url
        return {"model": custom_model}
