from __future__ import annotations

from uuid import uuid4

from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.flags import (
    memory_agent_id,
    memory_project_id,
    memory_user_id,
)
from trace_agent.memory.models import (
    SessionMemory,
    SessionMessage,
    SessionRole,
)


def _resolve_ownership(
    agent_id: str | None,
    user_id: str | None,
    project_id: str | None,
) -> dict[str, str | None]:
    """Section-4 ``sessions`` ownership columns for one new session.

    An explicit argument always wins over the environment-derived default, so the
    caller that does know (the web endpoint) is never overridden by the process
    default - and a CLI-only deployment, which has no user directory to consult,
    writes ``None`` for what it genuinely cannot know instead of failing a
    bookkeeping write.
    """
    return {
        "agent_id": agent_id if agent_id is not None else memory_agent_id(),
        "user_id": user_id if user_id is not None else memory_user_id(),
        "project_id": (
            project_id if project_id is not None else memory_project_id()
        ),
    }


class SessionService:
    """Durable conversation/session memory with append-only message turns.

    Messages are append-only by design: callers append one turn at a time and
    the service rewrites the session file atomically enough for the project's
    single-dashboard use case. Cross-process write locking is intentionally
    outside this boundary because memory failures must stay non-fatal.
    """

    def __init__(self, backend: MemoryStore) -> None:
        self._backend = backend

    def create_session(
        self,
        *,
        title: str = "",
        topic: str = "",
        session_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> SessionMemory:
        """Open a session, stamping whose it is.

        Ownership defaults to the environment-derived values rather than to
        ``None``: a dashboard that never sends them still produces rows a
        project-scoped query can find, and a CLI-only deployment - which has no
        user directory to consult - writes ``None`` for what it genuinely cannot
        know instead of failing a bookkeeping write. An explicit argument always
        wins, so the caller that does know (the web endpoint) is never
        overridden by the process default.
        """
        session = SessionMemory(
            session_id=session_id or f"session-{uuid4().hex[:12]}",
            title=title,
            topic=topic,
            **_resolve_ownership(agent_id, user_id, project_id),
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
            # Implicit create: a turn arriving before its session exists is
            # normal (a resumed dashboard tab), and the row still needs an owner
            # or it is invisible to every project-scoped listing afterwards.
            # Constructed rather than routed through ``create_session`` so the
            # turn below is the session's only write.
            session = SessionMemory(
                session_id=session_id,
                **_resolve_ownership(None, None, None),
            )
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

    def render_session_context(
        self, session: SessionMemory, *, max_chars: int = 4000
    ) -> str:
        """Render a session for a prompt: digest first, then the recent turns.

        The rolling summary leads because it is what survives truncation. A long
        session rendered tail-first would lose its own conclusion to the
        character cap and leave the model with the newest fragments and no idea
        what had already been established.
        """
        blocks: list[str] = []
        if session.title:
            blocks.append(f"Session: {session.title}")
        summary = (session.summary or "").strip()
        if summary:
            blocks.append("摘要:\n" + summary)
        for message in session.messages[-12:]:
            label = "User" if message.role is SessionRole.USER else "Assistant"
            blocks.append(f"{label}: {message.content}")
        return "\n".join(blocks)[:max_chars]
