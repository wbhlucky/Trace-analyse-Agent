from __future__ import annotations

from typing import Protocol

from trace_agent.agent.local import LocalAnalysisAgent
from trace_agent.agent.protocols import AgentSelection, AnalysisAgent, ProviderMetadata
from trace_agent.agent.registry import agent_for_kind, metadata_for_kind
from trace_agent.config import LlmRuntimeConfig
from trace_agent.models import AgentKind, AnalyzeRequest, TraceHandle
from trace_agent.skills import (
    ScenarioSkillRouter,
    SkillCatalog,
    SkillDefinition,
)

from trace_agent.agent import providers  # noqa: F401  (registers providers)


class AnalysisAgentFactory(Protocol):
    def create(
        self,
        request: AnalyzeRequest,
        trace: TraceHandle,
        *,
        memory_context: str | None = None,
    ) -> AgentSelection:
        """Create an agent after Trace capabilities are known."""


class DefaultAnalysisAgentFactory:
    def __init__(
        self,
        catalog: SkillCatalog | None = None,
        llm_config: LlmRuntimeConfig | None = None,
    ) -> None:
        self._catalog = catalog or SkillCatalog.project_default()
        self._llm_config = llm_config

    def create(
        self,
        request: AnalyzeRequest,
        trace: TraceHandle,
        *,
        memory_context: str | None = None,
    ) -> AgentSelection:
        if request.agent is AgentKind.LOCAL:
            return AgentSelection(
                agent=LocalAnalysisAgent(),
                skills=[],
                provider=metadata_for_kind(AgentKind.LOCAL),
            )

        skills = ScenarioSkillRouter(self._catalog).select(
            request.scenario_type,
            trace.capabilities,
        )
        provider = metadata_for_kind(request.agent)
        agent = agent_for_kind(
            request.agent,
            skills=skills,
            runtime_config=self._llm_config,
            memory_context=memory_context,
        )
        return AgentSelection(
            agent=agent,
            skills=skills,
            provider=provider,
        )
