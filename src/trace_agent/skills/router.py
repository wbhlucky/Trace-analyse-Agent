from __future__ import annotations

from collections.abc import Iterable

from trace_agent.models import ScenarioType, TraceCapability
from trace_agent.scenarios import scenario_definition
from trace_agent.skills.catalog import SkillCatalog, SkillDefinition


# Backward-compatible public view; ScenarioCatalog remains the source of truth.
SCENARIO_SKILLS: dict[ScenarioType, str] = {
    scenario_type: scenario_definition(scenario_type).skill_name
    for scenario_type in ScenarioType
}


class ScenarioSkillRouter:
    """Select scenario and capability-dependent analysis methodologies."""

    def __init__(self, catalog: SkillCatalog) -> None:
        self._catalog = catalog

    def select(
        self,
        scenario_type: ScenarioType,
        capabilities: Iterable[TraceCapability] = (),
    ) -> list[SkillDefinition]:
        scenario_skill = scenario_definition(scenario_type).skill_name
        names = ["trace-analysis", scenario_skill]
        if TraceCapability.PERF_SAMPLES in set(capabilities):
            names.append("perf-sample-analysis")
        return self._catalog.require_many(names)
