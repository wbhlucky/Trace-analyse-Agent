from __future__ import annotations

from uuid import uuid4

from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import (
    SessionMemory,
    SessionMessage,
    SessionRole,
)


class SessionService:
    """Durable conversation/session memory with append-only message turns.

    Messages are append-only by design: callers append one turn at a time and
    the service rewrites the session file atomically enough for the project's
    single-dashboard use case. Cross-process write locking is intentionally
    outside this boundary because memory failures must stay non-fatal.
    """

    def __init__(self, backend: JsonMemoryBackend) -> None:
        self._backend = backend

    def create_session(
        self,
        *,
        title: str = "",
        topic: str = "",
        session_id: str | None = None,
    ) -> SessionMemory:
        session = SessionMemory(
            session_id=session_id or f"session-{uuid4().hex[:12]}",
            title=title,
            topic=topic,
        )
        self._backend.write_session(session)
        return session

    def append_message(
        self,
        session_id: str,
        content: str,
        *,
        role: SessionRole = SessionRole.USER,
        references: list[str] | None = None,
    ) -> SessionMemory:
        session = self._backend.load_session(session_id)
        if session is None:
            session = SessionMemory(session_id=session_id)
        message = SessionMessage(
            role=role,
            content=content,
            references=list(references or []),
        )
        session = session.model_copy(
            update={"messages": [*session.messages, message]}
        )
        # ``updated_at`` is refreshed by the backend writer.
        self._backend.write_session(session)
        return session

    def get_session(self, session_id: str) -> SessionMemory | None:
        return self._backend.load_session(session_id)

    def list_sessions(self) -> list[SessionMemory]:
        return self._backend.list_sessions()

    def render_session_context(self, session: SessionMemory, *, max_chars: int = 4000) -> str:
        blocks: list[str] = []
        if session.title:
            blocks.append(f"Session: {session.title}")
        for message in session.messages[-12:]:
            label = "User" if message.role is SessionRole.USER else "Assistant"
            blocks.append(f"{label}: {message.content}")
        return "\n".join(blocks)[:max_chars]
