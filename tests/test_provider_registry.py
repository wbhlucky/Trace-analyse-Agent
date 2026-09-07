from __future__ import annotations

from typing import Any

import pytest

from trace_agent.agent import (
    available_providers,
    create_agent,
    get_metadata,
    metadata_for_kind,
    register_provider,
    unregister_provider,
)
from trace_agent.errors import ProviderUnavailable
from trace_agent.models import AgentKind, AnalysisResult, AnalyzeRequest, ScenarioType

from trace_agent.agent.base import AnalysisAgent


class _FakeAgent:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def analyze(
        self,
        request: AnalyzeRequest,
        tools: Any,
    ) -> AnalysisResult:
        del request, tools
        return AnalysisResult(summary="fake", findings=[], limitations=[])


def test_available_providers_include_local_and_qoder() -> None:
    names = {provider.name for provider in available_providers()}
    assert "local" in names
    assert "qoder" in names


def test_qoder_metadata_is_capability_driven() -> None:
    metadata = metadata_for_kind(AgentKind.QODER)
    assert metadata.requires_model_auth is True
    assert metadata.requires_preflight is True
    assert metadata.supports_checkpoint is True
    assert metadata.strict_result_validation is True


def test_registry_supports_adding_a_second_provider() -> None:
    unregister_provider("fake")
    register_provider(
        "fake",
        factory=_FakeAgent,
        requires_model_auth=False,
        description="fake",
    )
    try:
        assert get_metadata("fake").description == "fake"
        agent = create_agent("fake", skills=[], runtime_config=None)
        assert isinstance(agent, _FakeAgent)
    finally:
        unregister_provider("fake")


def test_unregistered_provider_raises_uniform_error() -> None:
    with pytest.raises(ProviderUnavailable):
        create_agent("definitely-not-registered")
