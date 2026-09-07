"""Claude SDK-specific tool and MCP bridging.

Shared deterministic compression stays in
:mod:`trace_agent.agent.core.payload`; this module only owns the Claude ``tool``
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
            "提交最终 AgentAnalysisDraft。完成取证后必须调用一次；analysis "
            "只传语义 JSON，不含 perf 和 critical_threads。工具会执行严格 "
            "Schema 校验；若返回 "
            "accepted=false，按 validation_error 修正后再次提交。"
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
                        "不得继续取证；仅修正字段并再次提交。"
                    ),
                }
            )

        rejection = policy.validate_analysis(parsed)
        if rejection is not None:
            return submission_rejection(state, rejection)

        state["result"] = parsed
        registry.seal("最终 AnalysisResult 已通过 Schema 校验")
        return tool_result(
            {
                "accepted": True,
                "instruction": "结果已接收，不得再调用任何工具。",
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
                    "允许读取本次启用的 Skill 文件"
                    if allowed
                    else "Read 只能访问本次启用的 Skill 目录"
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
