from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trace_agent.agent.base import AnalysisAgent
from trace_agent.agent.local import LocalAnalysisAgent
from trace_agent.agent.qoder import QoderAgentSdkAgent
from trace_agent.config import LlmRuntimeConfig
from trace_agent.models import AgentKind, AnalyzeRequest, TraceHandle
from trace_agent.skills import (
    ScenarioSkillRouter,
    SkillCatalog,
    SkillDefinition,
)


@dataclass(frozen=True, slots=True)
class AgentSelection:
    agent: AnalysisAgent
    skills: list[SkillDefinition]


class AnalysisAgentFactory(Protocol):
    def create(
        self,
        request: AnalyzeRequest,
        trace: TraceHandle,
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
    ) -> AgentSelection:
        if request.agent is AgentKind.LOCAL:
            return AgentSelection(
                agent=LocalAnalysisAgent(),
                skills=[],
            )

        skills = ScenarioSkillRouter(self._catalog).select(
            request.scenario_type,
            trace.capabilities,
        )
        return AgentSelection(
            agent=QoderAgentSdkAgent(
                skills=skills,
                runtime_config=self._llm_config,
            ),
            skills=skills,
        )
