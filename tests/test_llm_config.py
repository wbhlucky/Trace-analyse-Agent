from __future__ import annotations

from trace_agent.config import (
    DEEPSEEK_ANTHROPIC_BASE_URL,
    LlmRuntimeConfig,
    save_project_llm_config,
)
from trace_agent.models import LlmProvider
from trace_agent.qoder_config import qoder_personal_access_token


def test_deepseek_project_config_is_preserved_for_qoder_byok(
    monkeypatch,
    tmp_path,
) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "QODER_PERSONAL_ACCESS_TOKEN",
        "TRACE_AGENT_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "TRACE_AGENT_LLM_PROVIDER=deepseek\n"
        "TRACE_AGENT_LLM_MODEL=deepseek-v4-pro[1m]\n"
        "DEEPSEEK_API_KEY=test-secret\n",
        encoding="utf-8",
    )

    config = LlmRuntimeConfig.resolve(project_root=tmp_path)

    assert config.provider is LlmProvider.DEEPSEEK
    assert config.model == "deepseek-v4-pro[1m]"
    assert config.base_url == DEEPSEEK_ANTHROPIC_BASE_URL
    assert config.api_key == "test-secret"
    assert config.model_policy is not None
    assert qoder_personal_access_token() is None
    assert "test-secret" not in repr(config)


def test_shell_deepseek_key_overrides_project_file(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shell-secret")
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=file-secret\n",
        encoding="utf-8",
    )

    config = LlmRuntimeConfig.resolve(project_root=tmp_path)

    assert config.api_key == "shell-secret"


def test_qoder_pat_is_independent_from_deepseek_key(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "qoder-secret")

    config = LlmRuntimeConfig.resolve(project_root=tmp_path)

    assert config.api_key == "deepseek-secret"
    assert qoder_personal_access_token() == "qoder-secret"
    assert "qoder-secret" not in repr(config)


def test_save_project_config_keeps_deepseek_settings(tmp_path) -> None:
    path = save_project_llm_config(
        project_root=tmp_path,
        provider=LlmProvider.DEEPSEEK,
        api_key="saved-secret",
        model="deepseek-v4-pro[1m]",
    )

    text = path.read_text(encoding="utf-8")
    assert "TRACE_AGENT_LLM_PROVIDER=deepseek" in text
    assert "TRACE_AGENT_LLM_MODEL=deepseek-v4-pro[1m]" in text
    assert "TRACE_AGENT_LLM_BASE_URL=" + DEEPSEEK_ANTHROPIC_BASE_URL in text
    assert "DEEPSEEK_API_KEY=saved-secret" in text
