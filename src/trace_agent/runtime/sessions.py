from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any, Protocol

from trace_agent.runtime.events import AgentEvent
from trace_agent.runtime.state import RuntimeState


@dataclass(slots=True)
class AgentSessionRecord:
    """Durable, transpilar execution context for one runtime-owned task.

    This is intentionally a plain dataclass (not Pydantic): the session store
    is a hot path and never parses untrusted input. Transport-specific state
    lives in ``metadata`` so a provider swap cannot change the schema.
    """

    session_id: str
    task_id: str
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    runtime_state: RuntimeState = RuntimeState.CREATED
    last_event_id: str | None = None
    transport_session_id: str | None = None
    repair_round: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["runtime_state"] = self.runtime_state.value
        payload["created_at"] = _iso(self.created_at)
        payload["updated_at"] = _iso(self.updated_at)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentSessionRecord":
        data = dict(payload)
        data["runtime_state"] = RuntimeState(
            data.get("runtime_state", RuntimeState.CREATED.value)
        )
        data["created_at"] = _from_iso(data.get("created_at"))
        data["updated_at"] = _from_iso(data.get("updated_at"))
        data["metadata"] = dict(data.get("metadata") or {})
        return cls(**data)


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat()
    )


def _from_iso(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return time()
    return time()


class SessionStore(Protocol):
    """Persistence boundary for resumable execution context.

    Implementations must be safe to call from multiple threads and must treat
    IO/parse failures as best-effort, exactly like the event store.
    """

    def create(self, session: AgentSessionRecord) -> None: ...

    def load(self, session_id: str) -> AgentSessionRecord | None: ...

    def save(self, session: AgentSessionRecord) -> None: ...

    def append_event(
        self,
        session_id: str,
        event: AgentEvent,
    ) -> None: ...

    def close(self, session_id: str) -> None: ...

    def last_event_id(self, session_id: str) -> str | None: ...


class InMemorySessionStore:
    """Thread-safe process-local session store for tests and embedded runs."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSessionRecord] = {}
        self._events: dict[str, list[AgentEvent]] = {}
        self._lock = threading.Lock()

    def create(self, session: AgentSessionRecord) -> None:
        with self._lock:
            self._sessions[session.session_id] = session
            self._events.setdefault(session.session_id, [])

    def load(self, session_id: str) -> AgentSessionRecord | None:
        with self._lock:
            return self._sessions.get(session_id)

    def save(self, session: AgentSessionRecord) -> None:
        with self._lock:
            session.updated_at = time()
            self._sessions[session.session_id] = session

    def append_event(self, session_id: str, event: AgentEvent) -> None:
        with self._lock:
            if session_id not in self._sessions:
                return
            self._events.setdefault(session_id, []).append(event)

    def close(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._events.pop(session_id, None)

    def last_event_id(self, session_id: str) -> str | None:
        with self._lock:
            events = self._events.get(session_id)
            if not events:
                return None
            return events[-1].event_id


class JsonlSessionStore:
    """File-backed session store for cross-restart resume.

    Sessions are written as one JSON document per line. ``append_event`` updates
    ``last_event_id`` on the owning record and re-serialises it, which keeps the
    store trivially loadable without a database dependency.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def _read_all(self) -> dict[str, AgentSessionRecord]:
        sessions: dict[str, AgentSessionRecord] = {}
        if not self._path.is_file():
            return sessions
        try:
            for raw_line in self._path.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                try:
                    record = AgentSessionRecord.from_dict(json.loads(raw_line))
                except (TypeError, ValueError, KeyError):
                    continue
                sessions[record.session_id] = record
        except OSError:
            return sessions
        return sessions

    def _write_all(self, sessions: dict[str, AgentSessionRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                record.to_dict(),
                ensure_ascii=False,
                default=str,
            )
            for record in sessions.values()
        ]
        self._path.write_text(
            "\n".join(lines) + "\n" if lines else "",
            encoding="utf-8",
        )

    def create(self, session: AgentSessionRecord) -> None:
        with self._lock:
            sessions = self._read_all()
            sessions[session.session_id] = session
            self._write_all(sessions)

    def load(self, session_id: str) -> AgentSessionRecord | None:
        with self._lock:
            return self._read_all().get(session_id)

    def save(self, session: AgentSessionRecord) -> None:
        session.updated_at = time()
        self.create(session)

    def append_event(self, session_id: str, event: AgentEvent) -> None:
        with self._lock:
            sessions = self._read_all()
            session = sessions.get(session_id)
            if session is None:
                return
            session.last_event_id = event.event_id
            session.updated_at = time()
            self._write_all(sessions)

    def close(self, session_id: str) -> None:
        with self._lock:
            sessions = self._read_all()
            sessions.pop(session_id, None)
            self._write_all(sessions)

    def last_event_id(self, session_id: str) -> str | None:
        return self.load(session_id).last_event_id if self.load(session_id) else None
