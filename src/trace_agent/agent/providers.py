"""Provider registrations.

Importing this module binds each supported agent provider (and its metadata)
in the registry. New SDKs register here only; no business layer changes are
required.
"""

from __future__ import annotations

from typing import Any

from trace_agent.agent.local import LocalAnalysisAgent
from trace_agent.agent.registry import register_provider
from trace_agent.agent.adapters.openai import OpenAIAnalysisAgent
from trace_agent.agent.claude import ClaudeAgentSdkAgent
from trace_agent.agent.qoder import QoderAgentSdkAgent


def _local_factory(**kwargs: Any) -> LocalAnalysisAgent:
    del kwargs
    return LocalAnalysisAgent()


def _qoder_factory(**kwargs: Any) -> QoderAgentSdkAgent:
    return QoderAgentSdkAgent(**kwargs)


def _openai_factory(**kwargs: Any) -> OpenAIAnalysisAgent:
    return OpenAIAnalysisAgent(**kwargs)


def _claude_factory(**kwargs: Any) -> ClaudeAgentSdkAgent:
    return ClaudeAgentSdkAgent(**kwargs)


def register_all() -> None:
    register_provider(
        "local",
        factory=_local_factory,
        description="Deterministic smoke agent; no model or SDK required.",
    )
    register_provider(
        "claude",
        factory=_claude_factory,
        requires_model_auth=True,
        requires_preflight=True,
        supports_checkpoint=True,
        requires_perf_evidence=True,
        strict_result_validation=True,
        extra_label="claude",
        description="Primary Claude Agent SDK adapter with an in-process MCP server.",
    )
    register_provider(
        "qoder",
        factory=_qoder_factory,
        requires_model_auth=True,
        requires_preflight=True,
        supports_checkpoint=True,
        requires_perf_evidence=True,
        strict_result_validation=True,
        extra_label="qoder",
        description="Qoder Agent SDK adapter with an in-process MCP server.",
    )
    register_provider(
        "openai",
        factory=_openai_factory,
        requires_model_auth=True,
        requires_preflight=True,
        supports_checkpoint=True,
        requires_perf_evidence=True,
        strict_result_validation=True,
        extra_label="openai",
        description="Example OpenAI adapter proving the adapter contract.",
    )


register_all()
