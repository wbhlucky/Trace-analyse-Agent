"""Qoder Agent SDK adapter package.

The concrete implementation is split across ``client`` (SDK construction and
model policy), ``session`` (stream handling) and ``tools`` (MCP/tool bridging).
The legacy facade remains importable from ``trace_agent.agent.qoder``.
"""

from trace_agent.agent.adapters.qoder.client import (
    build_model_resolver,
    ensure_sdk,
    personal_access_token,
    resolve_cli_path,
)
from trace_agent.agent.adapters.qoder.session import collect_text_blocks
from trace_agent.agent.adapters.qoder.tools import (
    create_read_hook,
    create_sdk_tool,
    create_skill_hook,
    create_submission_tool,
    evaluate_skill_file,
    is_enabled_skill,
    is_allowed_skill_file,
    matched_skill_name,
    submission_rejection,
    tool_result,
)

__all__ = [
    "build_model_resolver",
    "collect_text_blocks",
    "create_read_hook",
    "create_sdk_tool",
    "create_skill_hook",
    "create_submission_tool",
    "ensure_sdk",
    "evaluate_skill_file",
    "is_enabled_skill",
    "is_allowed_skill_file",
    "matched_skill_name",
    "personal_access_token",
    "resolve_cli_path",
    "submission_rejection",
    "tool_result",
]
