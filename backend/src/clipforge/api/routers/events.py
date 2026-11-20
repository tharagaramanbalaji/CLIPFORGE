"""Server-Sent Events: run/stage status for the caller's workspace (Redis pub/sub fan-out)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from clipforge.api.deps import AppSettings, CurrentPrincipal, DbSession
from clipforge.services.events import channel_for

router = APIRouter(tags=["events"])

HEARTBEAT_S = 15.0


@router.get("/events/stream")
async def stream(
    request: Request, p: CurrentPrincipal, settings: AppSettings, db: DbSession
) -> StreamingResponse:
    # Dependencies live until the response ends, i.e. as long as the browser tab is open. Release the DB
    # connection (same cached session the auth check used) so an idle stream never holds a transaction.
    db.close()
    channel = channel_for(p.workspace_id)

    async def gen() -> AsyncIterator[str]:
        client = aioredis.Redis.from_url(settings.redis_url)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=HEARTBEAT_S)
                if msg is None:
                    yield ": keep-alive\n\n"
                    continue
                data = msg["data"].decode() if isinstance(msg["data"], bytes) else str(msg["data"])
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await client.aclose()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
