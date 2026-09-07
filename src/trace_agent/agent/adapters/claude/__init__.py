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
from trace_agent.agent.adapters.claude.tools import (
    create_read_hook,
    create_sdk_tool,
    create_submission_tool,
    is_allowed_skill_file,
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
    "create_submission_tool",
    "ensure_sdk",
    "is_allowed_skill_file",
    "resolve_cli_path",
    "submission_rejection",
    "tool_result",
]
