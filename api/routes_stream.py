"""Investigation trace stream.

``GET /stream/investigation/{id}`` -- server-sent events, one per hypothesis
formed, per query run, and per hypothesis rejected, emitted as the loop runs
rather than batched at the end. This stream is what makes the demo watchable.

Late subscribers get the replay buffer first, then live events. The UI
connects a moment after the investigation starts, and a stream that silently
skipped the opening hypotheses would show a half-finished story.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from api.runtime import event_bus

log = logging.getLogger("api.stream")

router = APIRouter(prefix="/stream", tags=["stream"])

#: Emitted periodically so proxies do not close an idle connection.
HEARTBEAT_SECONDS = 15.0


def sse(event: dict) -> str:
    """Format one server-sent event."""
    name = event.get("type", "message")
    return f"event: {name}\ndata: {json.dumps(event, default=str)}\n\n"


async def _events(investigation_id: str) -> AsyncGenerator[str, None]:
    queue = event_bus.subscribe(investigation_id)
    try:
        for past in event_bus.history(investigation_id):
            yield sse(past)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield sse(event)
            if event.get("type") == "complete":
                break
    except asyncio.CancelledError:  # client went away
        raise
    finally:
        event_bus.unsubscribe(investigation_id, queue)


@router.get("/investigation/{investigation_id}")
async def stream_investigation(investigation_id: str) -> StreamingResponse:
    """Stream one investigation's trace as it happens."""
    return StreamingResponse(
        _events(investigation_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this, a buffering proxy holds the whole stream until the
            # investigation ends -- which defeats the entire point.
            "X-Accel-Buffering": "no",
        },
    )
