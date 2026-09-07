"""Claude Agent SDK-specific client construction and model policy.

This module is the only place in the Claude adapter that imports the
``claude_agent_sdk`` package and builds its options/auth. The shared runtime
config stays provider-agnostic; only values Claude Code itself needs are read
here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from trace_agent.config import LlmRuntimeConfig
from trace_agent.agent.sdk_guard import require_sdk

# Claude Code authenticates through the standard Anthropic environment
# variables. We intentionally read both spellings so that an Anthropic API key
# or an Anthropic-compatible token (for example the project's DeepSeek
# Anthropic-compatible endpoint) can be used without leaking into shared config.
_CLAUDE_AUTH_TOKEN_ENVS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)


def ensure_sdk() -> None:
    require_sdk(
        "claude_agent_sdk",
        extra_label="claude",
        provider="claude",
    )


def resolve_cli_path() -> Path | None:
    """Honor an explicit Claude Code CLI binary, when provided."""
    explicit = os.environ.get("CLAUDE_CLI_EXECUTABLE")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise RuntimeError(
                "CLAUDE_CLI_EXECUTABLE 指向的文件不存在："
                f"{path}"
            )
        return path.resolve()
    return None


def anthropic_auth_token() -> str | None:
    for name in _CLAUDE_AUTH_TOKEN_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def anthropic_base_url() -> str | None:
    return (
        os.environ.get("ANTHROPIC_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL_API")
    )


def build_runtime_env(
    runtime_config: LlmRuntimeConfig | None,
) -> dict[str, str]:
    """Bridge the resolved BYOK runtime config into Claude Code env vars.

    Claude Code already speaks the Anthropic Messages protocol, so routing a
    provider through it only requires ``ANTHROPIC_AUTH_TOKEN`` and
    ``ANTHROPIC_BASE_URL``. When no runtime config is supplied we fall back to
    whatever the local Claude Code session has configured (CLI login or env).
    """
    if runtime_config is None:
        return {}

    env: dict[str, str] = {}
    if runtime_config.api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = runtime_config.api_key
        env["ANTHROPIC_API_KEY"] = runtime_config.api_key
    if runtime_config.base_url:
        env["ANTHROPIC_BASE_URL"] = runtime_config.base_url
    return env
