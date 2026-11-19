"""Dependency providers. Readiness probes are dependencies so tests can override them."""

from __future__ import annotations

from collections.abc import Callable

import redis
from sqlalchemy import text

from clipforge.config import get_settings
from clipforge.db.session import get_engine

Probe = Callable[[], None]


def _db_probe() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


def _redis_probe() -> None:
    client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        client.ping()
    finally:
        client.close()


def readiness_probes() -> dict[str, Probe]:
    return {"database": _db_probe, "redis": _redis_probe}
