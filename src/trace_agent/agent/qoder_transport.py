from __future__ import annotations

import asyncio
from typing import Any

from trace_agent.agent.adapters.qoder.client import (
    build_model_resolver,
    ensure_sdk,
    personal_access_token,
    resolve_cli_path,
)
from trace_agent.agent.adapters.qoder.session import collect_text_blocks
from trace_agent.agent.transport import (
    AgentRequest,
    AgentResponse,
    AgentSession,
    RunCapableTransport,
)
from trace_agent.config import LlmRuntimeConfig


class QoderTransport:
    """Single-turn Qoder-backed :class:`RunCapableTransport`.

    Qoder-specific construction/streaming lives in
    ``agent/adapters/qoder`` (``client`` and ``session``); this class only
    bridges the provider-agnostic :class:`AgentRequest` / :class:`AgentResponse`
    contract to one Qoder ``query`` round.  Full scenario orchestration stays in
    ``QoderAgentSdkAgent``, which is the adapter used by ``AnalyzeApplication``.
    """

    def __init__(self, *, options: dict[str, Any] | None = None) -> None:
        self._options = dict(options or {})
        self._runtime_config: LlmRuntimeConfig | None = self._options.get(
            "runtime_config"
        )
        self._active: dict[str, Any] = {}

    async def run(
        self,
        request: AgentRequest,
        *,
        session: AgentSession | None = None,
        timeout: float | None = None,
    ) -> AgentResponse:
        del timeout  # Qoder does not expose a per-turn wall-clock override.
        ensure_sdk()
        from qoder_agent_sdk import (  # local import keeps the SDK optional
            QoderAgentOptions,
            QoderSDKClient,
            access_token_from_env,
            qodercli_auth,
        )

        session_id = session.id if session is not None else None
        metadata = dict(request.metadata)
        runtime_config = self._runtime_config or metadata.get("runtime_config")
        model_resolver = (
            build_model_resolver(runtime_config)
            if runtime_config is not None
            else None
        )
        requested_model = (
            metadata.get("model")
            if model_resolver is None
            else None
        )
        options = QoderAgentOptions(
            model=(
                None if model_resolver is not None else requested_model
            ),
            system_prompt=metadata.get("system_prompt", ""),
            cwd=metadata.get("cwd"),
            cli_path=resolve_cli_path(),
            setting_sources=["project"],
            permission_mode="dontAsk",
            auth=(
                access_token_from_env()
                if personal_access_token()
                else qodercli_auth()
            ),
            resolve_model=model_resolver,
        )
        client = QoderSDKClient(options=options)
        self._active[session_id or "latest"] = client
        try:
            async with client:
                await client.query(request.prompt)
                text_blocks, result_message = await collect_text_blocks(
                    client.receive_response(),
                )
        finally:
            self._active.pop(session_id or "latest", None)

        text = "\n".join(block for block in text_blocks if block)
        response_metadata: dict[str, Any] = {
            "session_id": session_id,
        }
        if result_message is not None:
            subtype = getattr(result_message, "subtype", None)
            response_metadata["result_subtype"] = subtype
            response_metadata["is_error"] = bool(
                getattr(result_message, "is_error", False)
            )
        return AgentResponse(text=text, metadata=response_metadata)

    async def cancel(self, session_id: str) -> None:
        client = self._active.get(session_id)
        if client is not None and hasattr(client, "interrupt"):
            result = client.interrupt()
            if asyncio.iscoroutine(result):
                await result

    async def close(self, session_id: str) -> None:
        client = self._active.pop(session_id, None)
        if client is not None and hasattr(client, "close"):
            result = client.close()
            if asyncio.iscoroutine(result):
                await result
