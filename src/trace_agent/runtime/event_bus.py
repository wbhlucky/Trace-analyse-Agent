from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from trace_agent.runtime.events import AgentEvent, EventType


class EventStore(Protocol):
    """Optional persistence boundary used by the bus.

    Implementations must be safe to call from multiple threads and must never
    raise on storage failures; the bus treats persistence as best-effort.
    """

    def append(self, event: AgentEvent) -> None: ...
    def replay(self, run_id: str, *, from_seq: int = 0) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class EventSubscription:
    run_id: str
    queue: "queue.Queue[AgentEvent | None]"
    unsubscribe: Callable[[], None] = field(compare=False, repr=False)


class TraceAgentEventBus:
    """In-memory pub/sub bus with per-run replay and monotonic cursors."""

    def __init__(
        self,
        *,
        store: EventStore | None = None,
        max_subscriber_queue: int = 2048,
    ) -> None:
        self._store = store
        self._events: dict[str, list[AgentEvent]] = {}
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, set[EventSubscription]] = {}
        self._lock = threading.Lock()
        self._max_subscriber_queue = max_subscriber_queue

    def publish(
        self,
        event: AgentEvent,
    ) -> AgentEvent:
        """Assign the next run cursor and fan out to subscribers.

        Subscriber delivery and persistence are intentionally best-effort:
        an event must be accepted by the bus but a slow or failed consumer must
        never interrupt the analysis worker.
        """
        run_id = event.run_id
        with self._lock:
            if event.seq <= 0:
                next_seq = self._seq.get(run_id, 0) + 1
                event = replace(event, seq=next_seq)
            self._seq[run_id] = max(self._seq.get(run_id, 0), event.seq)
            self._events.setdefault(run_id, []).append(event)
            for subscription in self._subscribers.get(run_id, ()):
                self._offer(subscription.queue, event)
            store = self._store
        self._write_store(store, event)
        return event

    def subscribe(
        self,
        run_id: str,
        *,
        from_seq: int = 0,
    ) -> EventSubscription:
        """Replay history and continue receiving live events on one queue.

        History replay and live registration happen under the same lock, so
        there is no gap or duplicated event across refresh/reconnect.
        """
        live_queue: queue.Queue[AgentEvent | None] = queue.Queue(
            maxsize=self._max_subscriber_queue
        )
        subscription = EventSubscription(
            run_id=run_id,
            queue=live_queue,
            unsubscribe=lambda: self._discard(run_id, live_queue),
        )
        with self._lock:
            for event in self._events.get(run_id, ()):
                if event.seq >= from_seq:
                    self._offer(live_queue, event)
            self._subscribers.setdefault(run_id, set()).add(subscription)
        return subscription

    def replay(self, run_id: str, *, from_seq: int = 0) -> list[AgentEvent]:
        with self._lock:
            return [
                event
                for event in self._events.get(run_id, ())
                if event.seq >= from_seq
            ]

    def store_replay(
        self,
        run_id: str,
        *,
        from_seq: int = 1,
    ) -> list[dict[str, Any]]:
        """Replay persisted events when the bus process was restarted."""
        if self._store is None:
            return []
        try:
            return self._store.replay(run_id, from_seq=from_seq)
        except Exception:
            return []

    def register_store(self, store: EventStore) -> None:
        self._store = store

    def _discard(
        self,
        run_id: str,
        live_queue: "queue.Queue[AgentEvent | None]",
    ) -> None:
        with self._lock:
            subscribers = self._subscribers.get(run_id)
            if not subscribers:
                return
            subscribers.discard(
                EventSubscription(run_id, live_queue)
            )

    def _offer(
        self,
        live_queue: "queue.Queue[AgentEvent | None]",
        event: AgentEvent,
    ) -> None:
        try:
            live_queue.put_nowait(event)
        except queue.Full:
            try:
                live_queue.get_nowait()
                live_queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                return

    @staticmethod
    def _write_store(store: EventStore | None, event: AgentEvent) -> None:
        if store is None:
            return
        try:
            store.append(event)
        except Exception:
            return


_default_bus: TraceAgentEventBus | None = None
_default_bus_lock = threading.Lock()


def get_event_bus() -> TraceAgentEventBus:
    global _default_bus
    with _default_bus_lock:
        if _default_bus is None:
            _default_bus = TraceAgentEventBus()
        return _default_bus


def reset_event_bus() -> TraceAgentEventBus:
    """Replace the module singleton, primarily for tests and server restarts."""
    global _default_bus
    with _default_bus_lock:
        _default_bus = TraceAgentEventBus()
        return _default_bus
