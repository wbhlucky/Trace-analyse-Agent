from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from trace_agent.models import LlmProvider


DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro"
QODER_TOKEN_ENV = "QODER_PERSONAL_ACCESS_TOKEN"

# Qoder BYOK providers that the CLI already routes natively (fixed endpoint).
# For these, the SDK must NOT inject a custom `url`, otherwise the platform
# fails with "Failed to generate custom pool".
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


def normalize_qoder_byok_model(provider: LlmProvider, model: str) -> str:
    """Translate human-friendly model names to Qoder's catalog IDs."""
    if provider is LlmProvider.BAILIAN:
        return _BAILIAN_DEEPSEEK_MODEL_IDS.get(model, model)
    if provider is LlmProvider.DEEPSEEK:
        return _DEEPSEEK_MODEL_IDS.get(model, model)
    return model


def is_native_byok_provider(provider: LlmProvider) -> bool:
    """True when Qoder routes this provider natively (no custom url)."""
    return provider.value in _NATIVE_BYOK_PROVIDERS


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
        raise ValueError("DeepSeek API Key 不能为空")

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
        QODER_TOKEN_ENV,
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
    """Resolved DeepSeek BYOK model config plus independent Qoder auth."""

    provider: LlmProvider
    model: str
    base_url: str
    api_key: str = field(repr=False)
    sdk_environment: dict[str, str] = field(default_factory=dict, repr=False)
    use_qoder_personal_access_token: bool = False
    source_path: Path | None = None

    @classmethod
    def resolve(
        cls,
        *,
        project_root: Path,
        provider: LlmProvider | None = None,
        model: str | None = None,
    ) -> LlmRuntimeConfig:
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
                "DeepSeek API Key 未配置；请在项目 .env 中填写 "
                "DEEPSEEK_API_KEY"
            )

        configured_model = (
            model
            or value("TRACE_AGENT_LLM_MODEL")
            or DEEPSEEK_DEFAULT_MODEL
        )
        resolved_model = normalize_qoder_byok_model(
            raw_provider,
            configured_model,
        )
        base_url = (
            value("TRACE_AGENT_LLM_BASE_URL")
            or value("ANTHROPIC_BASE_URL")
            or DEEPSEEK_ANTHROPIC_BASE_URL
        )
        qoder_token = value(QODER_TOKEN_ENV)
        environment = {QODER_TOKEN_ENV: qoder_token} if qoder_token else {}
        return cls(
            provider=raw_provider,
            model=resolved_model,
            base_url=base_url,
            api_key=api_key,
            sdk_environment=environment,
            use_qoder_personal_access_token=bool(qoder_token),
            source_path=(dotenv_path if dotenv_path.is_file() else None),
        )
