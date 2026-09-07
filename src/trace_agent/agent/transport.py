from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from trace_agent.models import AnalyzeRequest


class AgentMessage(Protocol):
    """One turn payload sent by the runtime into a model transport."""

    role: str
    content: str


class AgentTransport(Protocol):
    """Replaceable protocol boundary between the agent loop and a model SDK.

    A transport owns the wire format, authentication, streaming and session
    lifecycle.  The runtime only depends on this protocol, which makes the
    Qoder adapter a replaceable implementation instead of the architecture
    itself.
    """

    async def start(self, request: AnalyzeRequest) -> None:
        """Initialise a session for ``request`` without touching tools."""

    async def send(self, message: AgentMessage) -> None:
        """Deliver a turn payload; may raise transport-specific errors."""

    async def receive(self) -> AsyncIterator[Any]:
        """Yield model stream events until the turn completes."""
        raise NotImplementedError
        yield

    async def interrupt(self) -> None:
        """Best-effort cancellation of an in-flight turn."""

    async def close(self) -> None:
        """Release transport resources (sessions, subprocesses, sockets)."""


class FakeTransport:
    """Deterministic, in-memory transport used for offline verification."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.started: AnalyzeRequest | None = None
        self.sent: list[AgentMessage] = []
        self.interrupted = False
        self.closed = False

    async def start(self, request: AnalyzeRequest) -> None:
        self.started = request

    async def send(self, message: AgentMessage) -> None:
        self.sent.append(message)

    async def receive(self) -> AsyncIterator[Any]:
        for response in self._responses:
            yield response

    async def interrupt(self) -> None:
        self.interrupted = True

    async def close(self) -> None:
        self.closed = True


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single tool invocation requested by the model."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage counters, normalised across providers."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """Provider-agnostic request for a single agent invocation."""

    prompt: str
    tools: list[str] = field(default_factory=list)
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """Provider-agnostic response surface for a completed agent turn."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentSession:
    """Minimal session identifier exposed to the runtime."""

    id: str
    created_at: float | None = None


class RunCapableTransport(Protocol):
    """High-level, provider-agnostic boundary for one agent invocation.

    The runtime depends on this interface only; concrete SDK adapters (Qoder,
    OpenAI, Claude, local) own the wire protocol, streaming, authentication,
    and session lifecycle.
    """

    async def run(
        self,
        request: AgentRequest,
        *,
        session: AgentSession | None = None,
        timeout: float | None = None,
    ) -> AgentResponse:
        ...

    async def cancel(self, session_id: str) -> None:
        ...

    async def close(self, session_id: str) -> None:
        ...


class FakeRunTransport:
    """Deterministic implementation of :class:`RunCapableTransport`."""

    def __init__(self, *responses: AgentResponse) -> None:
        self._responses = list(responses)
        self.requests: list[AgentRequest] = []
        self.cancelled: list[str] = []
        self.closed: list[str] = []

    async def run(
        self,
        request: AgentRequest,
        *,
        session: AgentSession | None = None,
        timeout: float | None = None,
    ) -> AgentResponse:
        self.requests.append(request)
        if not self._responses:
            return AgentResponse(
                text="empty",
                metadata={"session_id": session.id if session else None},
            )
        return self._responses.pop(0)

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)
