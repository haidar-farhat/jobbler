"""Server-sent events: the live agent stream the dashboard renders.

Subscribers get the backlog first (so a dashboard opened mid-run catches up rather than
starting blank), then live events, then a periodic keepalive so proxies do not time the
connection out.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from ...events.bus import EventBus
from ..deps import get_bus

router = APIRouter(prefix="/agent", tags=["events"])

KEEPALIVE_SECONDS = 15


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    after: int = 0,
    bus: EventBus = Depends(get_bus),
):
    async def generator():
        async with bus.subscribe(run_id) as queue:
            for event in bus.history(run_id, after_seq=after):
                yield {"event": event.type.value, "id": str(event.seq),
                       "data": event.model_dump_json()}

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield {"event": "keepalive", "data": "{}"}
                    continue
                yield {"event": event.type.value, "id": str(event.seq),
                       "data": event.model_dump_json()}

    return EventSourceResponse(generator())


@router.get("/runs/{run_id}/events/history")
async def event_history(run_id: UUID, after: int = 0, bus: EventBus = Depends(get_bus)) -> list:
    """Polling fallback for clients without EventSource."""
    return [e.model_dump(mode="json") for e in bus.history(run_id, after_seq=after)]
