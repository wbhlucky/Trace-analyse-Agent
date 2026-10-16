"""Skill runtime wiring: memory injection, discovery bridge, access gating.

These tests exist because the previous failure mode was invisible to structure
tests: ``SkillCatalog``, ``ScenarioSkillRouter`` and the manifest fingerprint
all worked, yet nothing reached the model. Every assertion here is about what
the adapter actually hands to the SDK, not about what the repo contains.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from trace_agent.agent.adapters.claude import skills as claude_skills
from trace_agent.agent.adapters.claude import tools as claude_tools
from trace_agent.agent.claude import ClaudeAgentSdkAgent
from trace_agent.agent.core import (
    memory_section,
    open_skill_access_audit,
    record_memory_injection,
    system_prompt,
)
from trace_agent.agent.core.audit import SkillAccessAudit
from trace_agent.errors import AgentFailure
from trace_agent.memory.flags import (
    MEMORY_PROMPT_CHARS_DEFAULT,
    memory_prompt_chars,
)
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
    ScenarioType,
    TraceCapability,
)
from trace_agent.skills import ScenarioSkillRouter, SkillCatalog
from trace_agent.tools import ToolRegistry

MEMORY_MARKER = "MEMORY_MARKER_XYZ"
MEMORY_BUDGET_ENV = "TRACE_AGENT_MEMORY_PROMPT_CHARS"
# Mirrors MemoryRuntime.recall() output: block headers plus whole "- [role]"
# items. Truncation must respect those item boundaries.
MEMORY_ITEMS = "\n".join(
    [
        "Prior curated project knowledge:",
        "- [Procedure] alpha: " + "a" * 80,
        "- [Pattern] beta: " + "b" * 80,
        "- [Fact] gamma: " + "c" * 80,
    ]
)


def _skills() -> list[Any]:
    return ScenarioSkillRouter(SkillCatalog.project_default()).select(
        ScenarioType.COMPLETION_LATENCY,
        [TraceCapability.PERF_SAMPLES],
    )


# --- memory context -------------------------------------------------------


def test_system_prompt_injects_recalled_memory() -> None:
    prompt = system_prompt(
        _skills(),
        memory_context=MEMORY_MARKER,
        manual_submission=True,
    )

    assert MEMORY_MARKER in prompt


def test_system_prompt_omits_memory_section_when_absent() -> None:
    prompt = system_prompt(_skills(), memory_context=None, manual_submission=True)

    assert MEMORY_MARKER not in prompt
    assert "历史分析记忆" not in prompt


def test_system_prompt_caps_memory_at_the_default_budget() -> None:
    skills = _skills()
    base = system_prompt(skills, memory_context=None, manual_submission=True)
    prompt = system_prompt(
        skills,
        memory_context="z" * 5_000,
        manual_submission=True,
    )

    # "z" does not occur in the fixed rules, so it isolates the memory block.
    assert "z" * 3_000 in prompt
    assert "z" * 3_001 not in prompt
    assert len(prompt) - len(base) < 3_200
    assert prompt.endswith(base[-40:])


def test_memory_section_keeps_whole_items_and_marks_the_rest_elided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MEMORY_BUDGET_ENV, "200")

    section = memory_section(MEMORY_ITEMS)

    assert "- [Procedure] alpha: " + "a" * 80 in section
    assert "beta" not in section
    assert section.endswith("（其余召回项因长度预算被省略）")


def test_memory_section_falls_back_to_hard_cut_without_item_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MEMORY_BUDGET_ENV, "120")

    section = memory_section("z" * 5_000)

    assert "z" * 120 in section
    assert "z" * 121 not in section
    assert section.endswith("…")


def test_memory_section_uses_default_budget_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(MEMORY_BUDGET_ENV, raising=False)

    section = memory_section(MEMORY_ITEMS)

    # Everything fits under the 3000 char default, so recall is verbatim.
    assert section.endswith(MEMORY_ITEMS)
    assert "省略" not in section


def test_memory_section_falls_back_on_an_unusable_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in the budget env var degrades memory, it never fails a run.

    The budget is read by two callers - this prompt section and
    ``memory.context.composer`` - from one accessor. If the accessor raised, a
    stray ``TRACE_AGENT_MEMORY_PROMPT_CHARS=many`` in someone's shell would take
    down every analysis instead of just the recall section, and the composer
    would have to wrap prompt rendering in try/except to stay alive. Falling
    back to the default keeps both call sites total; ``memory stats`` prints the
    effective value so the typo is still visible.
    """
    monkeypatch.setenv(MEMORY_BUDGET_ENV, "0")
    assert memory_prompt_chars() == MEMORY_PROMPT_CHARS_DEFAULT
    assert memory_section(MEMORY_ITEMS).endswith(MEMORY_ITEMS)

    monkeypatch.setenv(MEMORY_BUDGET_ENV, "many")
    assert memory_prompt_chars() == MEMORY_PROMPT_CHARS_DEFAULT
    assert memory_section(MEMORY_ITEMS).endswith(MEMORY_ITEMS)

    monkeypatch.setenv(MEMORY_BUDGET_ENV, "8000")
    assert memory_prompt_chars() == 8000


def test_agent_forwards_memory_context_into_system_prompt() -> None:
    agent = ClaudeAgentSdkAgent(
        skills=_skills(),
        memory_context=MEMORY_MARKER,
    )

    assert MEMORY_MARKER in agent._system_prompt(manual_submission=True)


def test_record_memory_injection_distinguishes_the_three_outcomes(
    tmp_path: Path,
) -> None:
    audit = SkillAccessAudit(tmp_path / "skill-access.jsonl")
    injected_prompt = system_prompt(
        _skills(),
        memory_context=MEMORY_MARKER,
        manual_submission=True,
    )

    record_memory_injection(
        audit,
        memory_context=MEMORY_MARKER,
        system_prompt_text=injected_prompt,
    )
    record_memory_injection(
        audit,
        memory_context=None,
        system_prompt_text=injected_prompt,
    )
    record_memory_injection(
        audit,
        memory_context=MEMORY_MARKER,
        system_prompt_text="a prompt that lost the memory block",
    )

    entries = audit.read_entries()
    assert [entry["decision"] for entry in entries] == [
        "injected",
        "no_recall",
        "dropped",
    ]
    assert all(entry["tool"] == "memory_context" for entry in entries)
    assert entries[0]["reason"].startswith("recall_chars=")
    assert "prompt_chars=0" in entries[2]["reason"]


# --- discovery bridge -----------------------------------------------------


def test_repo_qoder_is_declared_as_a_local_plugin() -> None:
    root = Path(__file__).resolve().parents[1]

    config, reason = claude_skills.ensure_skill_plugin(root)

    assert reason == "ok"
    assert config == [{"type": "local", "path": str((root / ".qoder").resolve())}]


def test_plugin_manifest_is_created_for_installed_layout(tmp_path: Path) -> None:
    (tmp_path / ".qoder" / "skills" / "trace-analysis").mkdir(parents=True)

    config, reason = claude_skills.ensure_skill_plugin(tmp_path)

    assert reason == "ok"
    assert config is not None
    manifest = tmp_path / ".qoder" / ".claude-plugin" / "plugin.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["name"] == (
        claude_skills.PLUGIN_NAME
    )


def test_plugin_bridge_reports_missing_skill_root(tmp_path: Path) -> None:
    config, reason = claude_skills.ensure_skill_plugin(tmp_path)

    assert config is None
    assert "missing" in reason


# --- access gating --------------------------------------------------------


def test_skill_gate_accepts_plain_and_namespaced_names() -> None:
    enabled = {skill.name for skill in _skills()}

    assert claude_tools.is_enabled_skill(enabled, "trace-analysis")
    assert claude_tools.is_enabled_skill(
        enabled, "diting-agent-skills:trace-analysis"
    )
    assert not claude_tools.is_enabled_skill(enabled, "frame-jank-analysis")
    assert not claude_tools.is_enabled_skill(enabled, None)


def test_read_gate_resolves_skill_membership() -> None:
    skills = _skills()
    root = skills[0].project_root

    allowed, resolved, reason = claude_tools.evaluate_skill_file(
        root,
        skills,
        ".qoder/skills/trace-analysis/references/trace-streamer-schema.md",
    )

    assert allowed
    assert reason == "ok"
    assert claude_tools.matched_skill_name(skills, resolved) == "trace-analysis"


def test_read_gate_distinguishes_missing_from_out_of_scope() -> None:
    skills = _skills()
    root = skills[0].project_root

    # frame-jank-analysis has no references/ dir; the model has previously
    # guessed rendering-analysis.md there (it lives under trace-analysis).
    missing = claude_tools.evaluate_skill_file(
        root,
        skills,
        ".qoder/skills/frame-jank-analysis/references/rendering-analysis.md",
    )
    outside = claude_tools.evaluate_skill_file(root, skills, "README.md")

    assert missing == (False, missing[1], "missing")
    assert outside[0] is False
    assert outside[2] == "outside_enabled_skills"
    assert claude_tools._read_deny_reason("missing") != (
        claude_tools._read_deny_reason("outside_enabled_skills")
    )


def test_read_hook_records_allow_and_deny(tmp_path: Path) -> None:
    skills = _skills()
    root = skills[0].project_root
    audit = SkillAccessAudit(tmp_path / "skill-access.jsonl")
    hook = claude_tools.create_read_hook(root, skills, audit)

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        allowed = await hook(
            {
                "tool_input": {
                    "file_path": str(
                        root
                        / ".qoder/skills/trace-analysis/references/"
                        "trace-streamer-schema.md"
                    )
                }
            },
            "tu_1",
            {},
        )
        denied = await hook(
            {"tool_input": {"file_path": "README.md"}}, "tu_2", {}
        )
        return allowed, denied

    allowed, denied = asyncio.run(exercise())

    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    entries = audit.read_entries()
    assert [entry["decision"] for entry in entries] == ["allow", "deny"]
    assert entries[0]["skill"] == "trace-analysis"
    assert entries[1]["tool_use_id"] == "tu_2"


def test_skill_hook_denies_unselected_skill(tmp_path: Path) -> None:
    skills = _skills()
    audit = SkillAccessAudit(tmp_path / "skill-access.jsonl")
    hook = claude_tools.create_skill_hook(skills, audit)

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        allowed = await hook(
            {"tool_input": {"skill": "diting-agent-skills:trace-analysis"}},
            "tu_1",
            {},
        )
        denied = await hook(
            {"tool_input": {"skill": "frame-jank-analysis"}}, "tu_2", {}
        )
        return allowed, denied

    allowed, denied = asyncio.run(exercise())

    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [entry["tool"] for entry in audit.read_entries()] == ["Skill", "Skill"]


def test_audit_swallows_write_failures(tmp_path: Path) -> None:
    audit = SkillAccessAudit(tmp_path / "blocked" / "skill-access.jsonl")
    audit._path = tmp_path / "not-a-dir" / "skill-access.jsonl"
    (tmp_path / "not-a-dir").write_text("x", encoding="utf-8")

    audit.record(tool="Read", decision="allow")

    assert audit.read_entries() == []


# --- adapter option shape -------------------------------------------------


class _FakeClient:
    captured: dict[str, Any] = {}

    def __init__(self, *, options: Any) -> None:
        type(self).captured["options"] = options

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def query(self, prompt: str) -> None:
        type(self).captured.setdefault("prompts", []).append(prompt)

    async def receive_response(self) -> Any:
        if False:  # pragma: no cover - makes this an async generator
            yield None


def test_claude_adapter_registers_skill_tool_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("claude_agent_sdk")
    import claude_agent_sdk as sdk

    _FakeClient.captured = {}
    monkeypatch.setattr(sdk, "ClaudeSDKClient", _FakeClient)

    skills = _skills()
    agent = ClaudeAgentSdkAgent(skills=skills, memory_context=MEMORY_MARKER)
    registry = ToolRegistry(set())
    request = AnalyzeRequest(
        trace_id="t1",
        trace_path=tmp_path / "trace.htrace",
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="open app",
        symptom="slow",
        output_dir=tmp_path / "out",
        agent=AgentKind.CLAUDE,
    )

    async def exercise() -> None:
        with pytest.raises(AgentFailure):
            await agent.analyze(request, registry)

    asyncio.run(exercise())

    options = _FakeClient.captured["options"]
    assert "Skill" in options.tools
    assert "Skill" in options.allowed_tools
    assert options.skills == [skill.name for skill in skills]
    assert options.plugins == [
        {
            "type": "local",
            "path": str(
                skills[0].project_root.joinpath(".qoder").resolve()
            ),
        }
    ]
    assert "Bash" in options.disallowed_tools

    matchers = [m.matcher for m in options.hooks["PreToolUse"]]
    assert matchers == ["Read", "Skill"]

    assert MEMORY_MARKER in options.system_prompt

    audit_path = tmp_path / "out" / "skill-access.jsonl"
    assert audit_path.is_file()
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert entries[0]["tool"] == "skill_discovery"
    assert entries[0]["decision"] == "ok"

    memory_entries = [e for e in entries if e["tool"] == "memory_context"]
    assert len(memory_entries) == 1
    assert memory_entries[0]["decision"] == "injected"
    assert "prompt_chars=" in memory_entries[0]["reason"]


def test_open_audit_returns_none_without_output_dir() -> None:
    assert open_skill_access_audit(None) is None
