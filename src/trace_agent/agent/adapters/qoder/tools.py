"""Qoder SDK-specific tool and MCP bridging.

Shared deterministic compression stays in
:mod:`trace_agent.agent.core.payload`; this module only owns the Qoder ``tool``
decorator shape, the MCP result envelope, the submission tool, and the
read-only Skill file hook.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trace_agent.agent.core import agent_tool_payload, bounded_error
from trace_agent.agent.core.audit import SkillAccessAudit
from trace_agent.models import AgentAnalysisDraft, AnalyzeRequest
from trace_agent.policies import (
    EvidenceSubmissionPolicy,
    SubmissionRejection,
)
from trace_agent.skills import SkillDefinition
from trace_agent.tools import ToolDefinition, ToolRegistry


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
    }


def create_sdk_tool(
    tool_decorator: Any,
    definition: ToolDefinition,
    registry: ToolRegistry,
) -> Any:
    @tool_decorator(
        definition.name,
        definition.description,
        definition.input_schema,
    )
    async def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = registry.invoke(definition.name, arguments)
        return tool_result(
            agent_tool_payload(definition.name, payload)
        )

    return invoke


def create_submission_tool(
    tool_decorator: Any,
    state: dict[str, Any],
    registry: ToolRegistry,
    request: AnalyzeRequest,
) -> Any:
    @tool_decorator(
        "submit_analysis_result",
        (
            "\u63d0\u4ea4\u6700\u7ec8 AgentAnalysisDraft\u3002\u5b8c\u6210\u53d6\u8bc1\u540e\u5fc5\u987b\u8c03\u7528\u4e00\u6b21\uff1banalysis "
            "\u53ea\u4f20\u8bed\u4e49 JSON\uff0c\u4e0d\u542b perf \u548c critical_threads\u3002\u5de5\u5177\u4f1a\u6267\u884c\u4e25\u683c "
            "Schema \u6821\u9a8c\uff1b\u82e5\u8fd4\u56de "
            "accepted=false\uff0c\u6309 validation_error \u4fee\u6b63\u540e\u518d\u6b21\u63d0\u4ea4\u3002"
        ),
        {"analysis": dict},
    )
    async def submit(arguments: dict[str, Any]) -> dict[str, Any]:
        policy = EvidenceSubmissionPolicy(
            request=request,
            registry=registry,
        )
        rejection = policy.validate_collected_evidence()
        if rejection is not None:
            return submission_rejection(state, rejection)
        raw_analysis = arguments.get("analysis")
        try:
            draft = AgentAnalysisDraft.model_validate(raw_analysis)
            parsed = draft.to_analysis_result()
        except ValueError as exc:
            error = bounded_error(exc)
            state.setdefault("errors", []).append(
                "submit_analysis_result: " + error
            )
            return tool_result(
                {
                    "accepted": False,
                    "validation_error": error,
                    "instruction": (
                        "\u4e0d\u5f97\u7ee7\u7eed\u53d6\u8bc1\uff1b\u4ec5\u4fee\u6b63\u5b57\u6bb5\u5e76\u518d\u6b21\u63d0\u4ea4\u3002"
                    ),
                }
            )

        rejection = policy.validate_analysis(parsed)
        if rejection is not None:
            return submission_rejection(state, rejection)

        state["result"] = parsed
        registry.seal("\u6700\u7ec8 AnalysisResult \u5df2\u901a\u8fc7 Schema \u6821\u9a8c")
        return tool_result(
            {
                "accepted": True,
                "instruction": "\u7ed3\u679c\u5df2\u63a5\u6536\uff0c\u4e0d\u5f97\u518d\u8c03\u7528\u4efb\u4f55\u5de5\u5177\u3002",
            }
        )

    return submit


def submission_rejection(
    state: dict[str, Any],
    rejection: SubmissionRejection,
) -> dict[str, Any]:
    state.setdefault("errors", []).append(
        "submit_analysis_result: " + rejection.error
    )
    return tool_result(
        {
            "accepted": False,
            "validation_error": rejection.error,
            "instruction": rejection.instruction,
        }
    )


def create_read_hook(
    project_root: Path,
    skills: list[SkillDefinition],
    audit: SkillAccessAudit | None = None,
) -> Any:
    async def validate_read(
        hook_input: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del context
        tool_input = hook_input.get("tool_input")
        raw_path = (
            tool_input.get("file_path")
            if isinstance(tool_input, dict)
            else None
        )
        allowed, resolved, reason = evaluate_skill_file(
            project_root, skills, raw_path
        )
        if audit is not None:
            audit.record(
                tool="Read",
                decision="allow" if allowed else "deny",
                requested_path=raw_path if isinstance(raw_path, str) else None,
                resolved_path=resolved,
                skill=matched_skill_name(skills, resolved),
                reason=None if allowed else reason,
                tool_use_id=tool_use_id,
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": (
                    "\u5141\u8bb8\u8bfb\u53d6\u672c\u6b21\u542f\u7528\u7684 Skill \u6587\u4ef6"
                    if allowed
                    else _read_deny_reason(reason)
                ),
            }
        }

    return validate_read


def _read_deny_reason(reason: str) -> str:
    if reason == "missing":
        return (
            "\u8be5\u8def\u5f84\u4e0d\u5b58\u5728\u3002Skill \u7684 references/ "
            "\u8d44\u6599\u4f4d\u4e8e\u58f0\u660e\u5b83\u7684 Skill \u76ee\u5f55\u4e0b\uff0c"
            "\u8bf7\u6539\u7528\u6b63\u786e\u5f52\u5c5e\u7684 Skill \u8def\u5f84\u540e\u91cd\u8bd5\u3002"
        )
    if reason == "empty_path":
        return "Read \u7f3a\u5c11 file_path \u53c2\u6570\u3002"
    return "Read \u53ea\u80fd\u8bbf\u95ee\u672c\u6b21\u542f\u7528\u7684 Skill \u76ee\u5f55\u3002"


def create_skill_hook(
    skills: list[SkillDefinition],
    audit: SkillAccessAudit | None = None,
) -> Any:
    """Gate and audit ``Skill`` invocations.

    ``skills=`` is a context filter, not a sandbox: the CLI still exposes every
    discovered skill to the ``Skill`` tool. This hook keeps the runtime surface
    equal to the selected methodology by denying any skill the router did not
    pick, and records each load so progressive disclosure is measurable.
    """
    enabled = {skill.name for skill in skills}

    async def validate_skill(
        hook_input: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del context
        tool_input = hook_input.get("tool_input")
        requested = (
            tool_input.get("skill")
            if isinstance(tool_input, dict)
            else None
        )
        allowed = is_enabled_skill(enabled, requested)
        if audit is not None:
            audit.record(
                tool="Skill",
                decision="allow" if allowed else "deny",
                skill=requested if isinstance(requested, str) else None,
                tool_use_id=tool_use_id,
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": (
                    "\u5141\u8bb8\u52a0\u8f7d\u672c\u6b21\u542f\u7528\u7684 Skill"
                    if allowed
                    else "Skill \u53ea\u80fd\u52a0\u8f7d\u672c\u6b21\u573a\u666f\u9009\u4e2d\u7684 Skill"
                ),
            }
        }

    return validate_skill


def is_enabled_skill(enabled: set[str], raw_name: Any) -> bool:
    if not isinstance(raw_name, str) or not raw_name.strip():
        return False
    candidate = raw_name.strip()
    if candidate in enabled:
        return True
    # Plugin discovery namespaces skills as "<plugin>:<name>".
    return candidate.rsplit(":", 1)[-1] in enabled


def is_allowed_skill_file(
    project_root: Path,
    skills: list[SkillDefinition],
    raw_path: Any,
) -> bool:
    allowed, _, _ = evaluate_skill_file(project_root, skills, raw_path)
    return allowed


def evaluate_skill_file(
    project_root: Path,
    skills: list[SkillDefinition],
    raw_path: Any,
) -> tuple[bool, str | None, str]:
    """Classify a Read target so the denial tells the model how to recover."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False, None, "empty_path"

    requested = Path(raw_path)
    if not requested.is_absolute():
        requested = project_root.resolve() / requested
    try:
        resolved = requested.resolve(strict=True)
    except OSError:
        return False, str(requested), "missing"

    if not resolved.is_file():
        return False, str(resolved), "missing"
    allowed = any(
        resolved.is_relative_to(skill.directory.resolve())
        for skill in skills
    )
    return allowed, str(resolved), "ok" if allowed else "outside_enabled_skills"


def matched_skill_name(
    skills: list[SkillDefinition],
    resolved_path: str | None,
) -> str | None:
    if not resolved_path:
        return None
    path = Path(resolved_path)
    for skill in skills:
        if path.is_relative_to(skill.directory.resolve()):
            return skill.name
    return None
