from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from trace_agent.runtime.events import AgentEvent


class EventSink(Protocol):
    """Outbound telemetry boundary for structured runtime events."""

    def emit(self, event: AgentEvent) -> None: ...


class JsonlEventSink:
    """Append-only JSONL sink preserving strict per-run ordering."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def emit(self, event: AgentEvent) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            event.to_dict(),
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
            except OSError:
                return
