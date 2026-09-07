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
) -> Any:
    async def validate_read(
        hook_input: dict[str, Any],
        tool_use_id: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del tool_use_id, context
        tool_input = hook_input.get("tool_input")
        raw_path = (
            tool_input.get("file_path")
            if isinstance(tool_input, dict)
            else None
        )
        allowed = is_allowed_skill_file(project_root, skills, raw_path)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": (
                    "\u5141\u8bb8\u8bfb\u53d6\u672c\u6b21\u542f\u7528\u7684 Skill \u6587\u4ef6"
                    if allowed
                    else "Read \u53ea\u80fd\u8bbf\u95ee\u672c\u6b21\u542f\u7528\u7684 Skill \u76ee\u5f55"
                ),
            }
        }

    return validate_read


def is_allowed_skill_file(
    project_root: Path,
    skills: list[SkillDefinition],
    raw_path: Any,
) -> bool:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False

    requested = Path(raw_path)
    if not requested.is_absolute():
        requested = project_root.resolve() / requested
    try:
        resolved = requested.resolve(strict=True)
    except OSError:
        return False

    return resolved.is_file() and any(
        resolved.is_relative_to(skill.directory.resolve())
        for skill in skills
    )
