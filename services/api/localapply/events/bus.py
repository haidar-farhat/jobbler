"""The append-only agent event stream.

Every observation, decision, verdict, and result becomes an `AgentEvent`. That log is the
single source for the live dashboard, the audit trail, and offline replay: if you can read
the events, you can reconstruct exactly what the agent saw, what it proposed, what policy
said, and what actually happened.

In-memory fan-out plus a bounded per-run backlog so a dashboard connecting mid-run catches
up rather than starting blind. Durable persistence is the run loop's job (`agent_events`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

from ..contracts import AgentEvent, EventType

logger = logging.getLogger(__name__)

#: Events retained per run for late subscribers.
BACKLOG_SIZE = 500
#: Slow-consumer bound. A dashboard that cannot keep up drops events rather than stalling
#: the agent -- the durable log in Postgres remains complete either way.
QUEUE_SIZE = 256


#: Signature of a durable sink: called once per event, after `seq` is assigned.
EventSink = Callable[[AgentEvent], Awaitable[None]]


class EventBus:
    def __init__(self, sink: EventSink | None = None) -> None:
        #: Writes each event to durable storage. Without one the stream is memory-only,
        #: which is fine for tests but means no replay after a restart.
        self._sink: EventSink | None = sink
        self._subscribers: dict[UUID, set[asyncio.Queue[AgentEvent]]] = defaultdict(set)
        self._backlog: dict[UUID, deque[AgentEvent]] = defaultdict(
            lambda: deque(maxlen=BACKLOG_SIZE)
        )
        self._seq: dict[UUID, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    def attach_sink(self, sink: EventSink) -> None:
        """Wire durable persistence. Done by the composition root, once."""
        self._sink = sink

    async def publish(self, event: AgentEvent) -> AgentEvent:
        async with self._lock:
            self._seq[event.run_id] += 1
            event = event.model_copy(update={"seq": self._seq[event.run_id]})
            self._backlog[event.run_id].append(event)
            subscribers = list(self._subscribers.get(event.run_id, ()))

        # Durable first, then fan out. A failed write must never take down the run, but it
        # must be visible rather than silently dropping the audit trail.
        if self._sink is not None:
            try:
                await self._sink(event)
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to persist agent event %s: %s", event.event_id, exc)

        for queue in subscribers:
            # Slow consumer: drop rather than stall the agent. The durable log in Postgres
            # stays complete either way, and the client can refetch history by seq.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return event

    async def emit(
        self,
        run_id: UUID,
        type_: EventType,
        message: str = "",
        *,
        agent: str = "orchestrator",
        payload: dict | None = None,
    ) -> AgentEvent:
        return await self.publish(
            AgentEvent(
                run_id=run_id,
                type=type_,
                agent=agent,
                message=message,
                payload=payload or {},
            )
        )

    def history(self, run_id: UUID, after_seq: int = 0) -> list[AgentEvent]:
        return [e for e in self._backlog.get(run_id, ()) if e.seq > after_seq]

    @asynccontextmanager
    async def subscribe(self, run_id: UUID) -> AsyncIterator[asyncio.Queue[AgentEvent]]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers[run_id].discard(queue)
                if not self._subscribers[run_id]:
                    del self._subscribers[run_id]

    def forget(self, run_id: UUID) -> None:
        """Drop in-memory state for a finished run. The DB keeps the durable copy."""
        self._backlog.pop(run_id, None)
        self._seq.pop(run_id, None)


#: Process-wide bus.
EVENT_BUS = EventBus()
