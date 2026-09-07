from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trace_agent.agent.qoder import QoderAgentSdkAgent
from trace_agent.config import LlmRuntimeConfig
from trace_agent.llm.deepseek import DeepSeekByokPolicy
from trace_agent.models import LlmProvider, ScenarioType, TraceCapability
from trace_agent.skills import ScenarioSkillRouter, SkillCatalog


def _cold_start_agent() -> QoderAgentSdkAgent:
    skills = ScenarioSkillRouter(
        SkillCatalog.project_default()
    ).select(ScenarioType.COLD_START)
    return QoderAgentSdkAgent(skills=skills)


def test_read_allows_reference_from_selected_skill() -> None:
    agent = _cold_start_agent()

    assert agent._is_allowed_skill_file(
        ".qoder/skills/trace-analysis/references/"
        "trace-streamer-schema.md"
    )


def test_read_denies_file_outside_selected_skills() -> None:
    assert not _cold_start_agent()._is_allowed_skill_file("README.md")


def test_read_denies_reference_from_unselected_scenario() -> None:
    assert not _cold_start_agent()._is_allowed_skill_file(
        ".qoder/skills/frame-jank-analysis/SKILL.md"
    )


def test_read_denies_missing_file() -> None:
    assert not _cold_start_agent()._is_allowed_skill_file(
        ".qoder/skills/trace-analysis/references/missing.md"
    )


def test_system_prompt_requires_validated_submission() -> None:
    prompt = _cold_start_agent()._system_prompt(manual_submission=True)

    assert len(prompt) < 4_000
    assert '"$defs"' not in prompt
    assert "submit_analysis_result" in prompt
    assert "Skill(trace-analysis)" in prompt
    assert "Skill(cold-start-analysis)" in prompt
    assert "运行时硬限制" in prompt


def test_structured_result_parser_falls_back_to_valid_text_json() -> None:
    result = SimpleNamespace(structured_output={"summary": 123})

    parsed, source, errors = QoderAgentSdkAgent._parse_analysis_result(
        result,
        ['{"summary":"valid fallback"}'],
    )

    assert parsed is not None
    assert parsed.summary == "valid fallback"
    assert source == "text_blocks[-1]"
    assert errors


def test_read_allows_perf_reference_when_perf_skill_selected() -> None:
    skills = ScenarioSkillRouter(SkillCatalog.project_default()).select(
        ScenarioType.COLD_START,
        [TraceCapability.PERF_SAMPLES],
    )
    agent = QoderAgentSdkAgent(skills=skills)

    assert agent._is_allowed_skill_file(
        ".qoder/skills/perf-sample-analysis/references/"
        "perf-trace-streamer-schema.md"
    )


def test_resolve_cli_path_uses_explicit_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "qodercli.exe"
    binary.touch()
    monkeypatch.setenv("QODER_CLI_EXECUTABLE", str(binary))

    assert QoderAgentSdkAgent._resolve_cli_path() == binary.resolve()


def test_runtime_config_builds_deepseek_byok_model_policy() -> None:
    config = LlmRuntimeConfig(
        provider=LlmProvider.DEEPSEEK,
        model="deepseek-v4-pro-pg",
        base_url="https://api.deepseek.com/anthropic",
        api_key="secret",
        model_policy=DeepSeekByokPolicy(
            provider=LlmProvider.DEEPSEEK,
            model="deepseek-v4-pro-pg",
            api_key="secret",
        ),
    )
    agent = QoderAgentSdkAgent(
        skills=ScenarioSkillRouter(SkillCatalog.project_default()).select(
            ScenarioType.COLD_START
        ),
        runtime_config=config,
    )

    resolver = agent._create_model_resolver()
    assert resolver is not None
    policy = resolver({})

    assert policy["model"]["provider"] == "deepseek"
    assert policy["model"]["api_key"] == "secret"
    assert policy["model"]["model"] == "deepseek-v4-pro-pg"
    assert "url" not in policy["model"]
    assert "style" not in policy["model"]
