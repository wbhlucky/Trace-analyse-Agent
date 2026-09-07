"""Pluggable-SDK contract tests.

These tests encode the requirement that swapping an SDK provider only touches
an adapter module, the optional-dependency extra, and the registry entry; the
application, validation and shared config layers must stay provider-agnostic.
"""

from __future__ import annotations

import pytest

from trace_agent.agent import available_providers, create_agent, get_metadata
from trace_agent.errors import ProviderUnavailable


def test_all_providers_are_registered() -> None:
    names = {provider.name for provider in available_providers()}
    assert {"local", "claude", "qoder", "openai"} <= names


def test_claude_adapter_implements_analysis_agent_protocol() -> None:
    from pathlib import Path

    from trace_agent.agent.claude import ClaudeAgentSdkAgent
    from trace_agent.skills import SkillDefinition

    skill = SkillDefinition(
        name="cold-start",
        directory=Path("."),
        project_root=Path("."),
        fingerprint="fake",
    )
    agent = ClaudeAgentSdkAgent(skills=[skill])
    assert callable(agent.analyze)


def test_claude_adapter_guards_optional_sdk() -> None:
    from trace_agent.agent.adapters.claude.client import ensure_sdk
    from trace_agent.agent.sdk_guard import require_sdk

    # A missing module surfaces as ProviderUnavailable with provider="claude".
    with pytest.raises(ProviderUnavailable) as excinfo:
        require_sdk(
            "trace_agent_sdk_definitely_missing",
            extra_label="claude",
            provider="claude",
        )

    assert excinfo.value.provider == "claude"
    assert callable(ensure_sdk)


def test_openai_adapter_implements_analysis_agent_protocol() -> None:
    from trace_agent.agent.adapters.openai import OpenAIAnalysisAgent

    agent = OpenAIAnalysisAgent()
    assert callable(agent.analyze)


def test_openai_adapter_guards_optional_sdk() -> None:
    from trace_agent.agent.adapters.openai.client import ensure_sdk

    with pytest.raises(ProviderUnavailable) as excinfo:
        ensure_sdk()

    assert excinfo.value.provider == "openai"


def test_registry_metadata_is_declarative() -> None:
    assert get_metadata("openai").requires_model_auth is True
    assert get_metadata("openai").extra_label == "openai"


def test_shared_config_has_no_provider_specific_auth() -> None:
    from trace_agent.config import LlmRuntimeConfig

    annotations = LlmRuntimeConfig.__dataclass_fields__
    assert "sdk_environment" not in annotations
    assert "use_qoder_personal_access_token" not in annotations
    assert "model_policy" in annotations


def test_qoder_transport_implements_run_capable_contract() -> None:
    from trace_agent.agent.qoder_transport import QoderTransport
    from trace_agent.agent.transport import AgentRequest

    transport = QoderTransport()
    assert callable(transport.run)
    assert callable(transport.cancel)
    assert callable(transport.close)

    # Module import must not require the optional SDK.
    request = AgentRequest(prompt="hello")
    result = transport.run(request)
    assert hasattr(result, "__await__")
    result.close()

