"""Dependency providers; probes, storage and the task dispatcher are overridable in tests."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

import redis
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from clipforge.config import Settings, get_settings
from clipforge.db.models import MemberRole, Membership, User
from clipforge.db.session import get_engine, get_session
from clipforge.pipeline.orchestrator import Dispatch
from clipforge.services.auth import read_token
from clipforge.storage import LocalStorage

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


def settings_dep() -> Settings:
    return get_settings()


def storage_dep() -> LocalStorage:
    return LocalStorage(get_settings().data_dir)


def dispatch_dep() -> Dispatch:
    from clipforge.workers.tasks import celery_dispatch  # imported lazily: pulls in Celery

    return celery_dispatch


DbSession = Annotated[Session, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(settings_dep)]
Store = Annotated[LocalStorage, Depends(storage_dep)]
Dispatcher = Annotated[Dispatch, Depends(dispatch_dep)]


@dataclass(frozen=True, slots=True)
class Principal:
    user: User
    workspace_id: uuid.UUID
    role: MemberRole


def current_principal(request: Request, db: DbSession, settings: AppSettings) -> Principal:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not signed in.")
    token = request.cookies.get(settings.session_cookie_name)
    user_id = read_token(token, settings) if token else None
    if user_id is None:
        raise unauthorized
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id).limit(1))
    if membership is None:
        raise unauthorized
    return Principal(user=user, workspace_id=membership.workspace_id, role=membership.role)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
