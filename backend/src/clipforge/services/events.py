"""Workspace event bus (Redis pub/sub) feeding the SSE endpoint.

Publishing is best-effort: status lives in Postgres, events only tell the UI to refresh.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from typing import Any, Protocol

import redis

from clipforge.config import get_settings
from clipforge.log import get_logger

log = get_logger(__name__)


def channel_for(workspace_id: uuid.UUID | str) -> str:
    return f"events:{workspace_id}"


class Publisher(Protocol):
    def publish(self, workspace_id: uuid.UUID, event: dict[str, Any]) -> None: ...


class RedisPublisher:
    def __init__(self, url: str) -> None:
        self._client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)

    def publish(self, workspace_id: uuid.UUID, event: dict[str, Any]) -> None:
        try:
            self._client.publish(channel_for(workspace_id), json.dumps(event, default=str))
        except redis.RedisError as exc:
            log.warning("event_publish_failed", error=str(exc), type=event.get("type"))


class MemoryPublisher:
    """For tests: records events instead of sending them."""

    def __init__(self) -> None:
        self.events: list[tuple[uuid.UUID, dict[str, Any]]] = []

    def publish(self, workspace_id: uuid.UUID, event: dict[str, Any]) -> None:
        self.events.append((workspace_id, event))


_override: Publisher | None = None


@lru_cache
def _default() -> Publisher:
    return RedisPublisher(get_settings().redis_url)


def set_publisher(publisher: Publisher | None) -> None:
    global _override
    _override = publisher


def publish(workspace_id: uuid.UUID, event_type: str, **data: Any) -> None:
    (_override or _default()).publish(workspace_id, {"type": event_type, **data})
