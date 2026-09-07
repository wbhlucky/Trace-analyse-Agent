"""Provider-independent agent core.

Shared, SDK-free logic extracted from the Qoder adapter: prompt building,
structured-result parsing, diagnostic artifacts, evidence payload compaction,
and environment helpers. Concrete SDK adapters import these instead of
duplicating them.
"""

from trace_agent.agent.core.diagnostics import result_diagnostic, write_agent_result
from trace_agent.agent.core.helpers import (
    bounded_error,
    bounded_preview,
    extract_json,
    positive_int_env,
)
from trace_agent.agent.core.parsing import parse_analysis_result, parse_with_submission
from trace_agent.agent.core.payload import (
    agent_tool_payload,
    compact_completion_phases,
    compact_perf_profile,
    select,
)
from trace_agent.agent.core.prompts import repair_prompt, system_prompt, user_prompt

__all__ = [
    "agent_tool_payload",
    "bounded_error",
    "bounded_preview",
    "compact_completion_phases",
    "compact_perf_profile",
    "extract_json",
    "parse_analysis_result",
    "parse_with_submission",
    "positive_int_env",
    "repair_prompt",
    "result_diagnostic",
    "select",
    "system_prompt",
    "user_prompt",
    "write_agent_result",
]
