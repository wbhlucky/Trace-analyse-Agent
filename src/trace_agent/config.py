from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trace_agent.llm.deepseek import (
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DeepSeekByokPolicy,
)
from trace_agent.llm.ports import ModelPolicy
from trace_agent.models import LlmProvider


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read the small KEY=VALUE subset used by the project config."""
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        value = raw_value.strip()
        if not name:
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[name] = value
    return values


def save_project_llm_config(
    *,
    project_root: Path,
    provider: LlmProvider,
    api_key: str,
    model: str | None = None,
) -> Path:
    """Persist the existing DeepSeek BYOK config without exposing its key."""
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("DeepSeek API Key 涓嶈兘涓虹┖")

    path = project_root / ".env"
    existing = _read_dotenv(path)
    existing["TRACE_AGENT_LLM_PROVIDER"] = provider.value
    existing["TRACE_AGENT_LLM_MODEL"] = model or DEEPSEEK_DEFAULT_MODEL
    existing["TRACE_AGENT_LLM_BASE_URL"] = DEEPSEEK_ANTHROPIC_BASE_URL
    existing["DEEPSEEK_API_KEY"] = api_key

    preferred_order = [
        "TRACE_AGENT_LLM_PROVIDER",
        "TRACE_AGENT_LLM_MODEL",
        "TRACE_AGENT_LLM_BASE_URL",
        "DEEPSEEK_API_KEY",
    ]
    ordered_names = [name for name in preferred_order if name in existing]
    ordered_names.extend(
        name for name in existing if name not in preferred_order
    )
    path.write_text(
        "\n".join(f"{name}={existing[name]}" for name in ordered_names)
        + "\n",
        encoding="utf-8",
    )
    return path


@dataclass(frozen=True, slots=True)
class LlmRuntimeConfig:
    """Resolved BYOK model config with a provider-supplied model policy.

    This class is provider-agnostic: SDK-specific auth (for example the Qoder
    PAT) is read by that SDK's adapter rather than leaking into shared config.
    """

    provider: LlmProvider
    model: str
    base_url: str
    api_key: str = field(repr=False)
    model_policy: ModelPolicy = field(repr=False)
    source_path: Path | None = None

    @classmethod
    def resolve(
        cls,
        *,
        project_root: Path,
        provider: LlmProvider | None = None,
        model: str | None = None,
        model_policy: ModelPolicy | None = None,
    ) -> "LlmRuntimeConfig":
        dotenv_path = project_root / ".env"
        dotenv = _read_dotenv(dotenv_path)

        def value(name: str) -> str | None:
            shell_value = os.environ.get(name)
            if shell_value:
                return shell_value
            file_value = dotenv.get(name)
            return file_value or None

        raw_provider = provider or LlmProvider(
            (value("TRACE_AGENT_LLM_PROVIDER") or "deepseek").lower()
        )
        api_key = (
            value("DEEPSEEK_API_KEY")
            or value("ANTHROPIC_AUTH_TOKEN")
            or value("ANTHROPIC_API_KEY")
        )
        if not api_key:
            raise ValueError(
                "DeepSeek API Key 鏈厤缃紱璇峰湪椤圭洰 .env 涓～鍐?"
                "DEEPSEEK_API_KEY"
            )

        configured_model = (
            model
            or value("TRACE_AGENT_LLM_MODEL")
            or DEEPSEEK_DEFAULT_MODEL
        )
        base_url = (
            value("TRACE_AGENT_LLM_BASE_URL")
            or value("ANTHROPIC_BASE_URL")
            or DEEPSEEK_ANTHROPIC_BASE_URL
        )
        policy = model_policy or DeepSeekByokPolicy(
            provider=raw_provider,
            model=configured_model,
            api_key=api_key,
            base_url=base_url,
        )
        return cls(
            provider=raw_provider,
            model=configured_model,
            base_url=base_url,
            api_key=api_key,
            model_policy=policy,
            source_path=(dotenv_path if dotenv_path.is_file() else None),
        )
