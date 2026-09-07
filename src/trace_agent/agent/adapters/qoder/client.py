"""Qoder SDK-specific client construction and model policy.

This module is the only place in the Qoder adapter that imports the
``qoder_agent_sdk`` package and builds its options/auth.  The shared runtime
config remains provider-agnostic; only values that Qoder itself needs are read
here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from trace_agent.config import LlmRuntimeConfig
from trace_agent.qoder_config import (
    QODER_TOKEN_ENV,
    is_native_byok_provider,
    normalize_qoder_byok_model,
)
from trace_agent.agent.sdk_guard import require_sdk


def resolve_cli_path() -> Path | None:
    """Honor an explicit Qoder CLI binary; otherwise use SDK bundled CLI."""
    explicit = os.environ.get("QODER_CLI_EXECUTABLE")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RuntimeError(
                "QODER_CLI_EXECUTABLE \u6307\u5411\u7684\u6587\u4ef6\u4e0d\u5b58\u5728\uff1a"
                f"{path}"
            )
        return path.resolve()
    return None


def build_model_resolver(
    runtime_config: LlmRuntimeConfig | None,
) -> Any | None:
    """Route every Qoder LLM turn through the configured BYOK provider."""
    if runtime_config is None:
        return None

    custom_model: dict[str, Any] = {
        "provider": runtime_config.provider.value,
        "model": normalize_qoder_byok_model(
            runtime_config.provider,
            runtime_config.model,
        ),
        "api_key": runtime_config.api_key,
    }
    if not is_native_byok_provider(runtime_config.provider):
        custom_model["url"] = runtime_config.base_url

    def resolve_model(context: dict[str, Any]) -> dict[str, Any]:
        del context
        return {"model": dict(custom_model)}

    return resolve_model


def personal_access_token() -> str | None:
    return os.environ.get(QODER_TOKEN_ENV) or None


def ensure_sdk() -> None:
    require_sdk("qoder_agent_sdk", extra_label="qoder", provider="qoder")
