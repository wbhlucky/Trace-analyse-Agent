"""Claude Agent SDK adapter package.

The concrete implementation is split across ``client`` (SDK construction and
model policy), ``session`` (stream handling) and ``tools`` (MCP/tool bridging).
The primary agent remains importable from ``trace_agent.agent.claude``.
"""

from trace_agent.agent.adapters.claude.client import (
    anthropic_auth_token,
    anthropic_base_url,
    build_runtime_env,
    ensure_sdk,
    resolve_cli_path,
)
from trace_agent.agent.adapters.claude.session import collect_text_blocks
from trace_agent.agent.adapters.claude.skills import ensure_skill_plugin
from trace_agent.agent.adapters.claude.tools import (
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
    "anthropic_auth_token",
    "anthropic_base_url",
    "build_runtime_env",
    "collect_text_blocks",
    "create_read_hook",
    "create_sdk_tool",
    "create_skill_hook",
    "create_submission_tool",
    "ensure_sdk",
    "ensure_skill_plugin",
    "evaluate_skill_file",
    "is_enabled_skill",
    "is_allowed_skill_file",
    "matched_skill_name",
    "resolve_cli_path",
    "submission_rejection",
    "tool_result",
]
