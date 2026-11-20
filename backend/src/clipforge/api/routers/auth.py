from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field

from clipforge.api.deps import AppSettings, CurrentPrincipal, DbSession
from clipforge.config import Environment, Settings
from clipforge.db.models import User
from clipforge.services.auth import authenticate, issue_token, register_owner

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    workspace_name: str = Field(default="My channel", min_length=1, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    workspace_id: uuid.UUID
    role: str


def _set_cookie(response: Response, user: User, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        issue_token(user.id, settings),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.env is Environment.PROD,
        path="/",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, response: Response, db: DbSession, settings: AppSettings) -> dict[str, str]:
    user = register_owner(
        db,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        workspace_name=body.workspace_name,
    )
    _set_cookie(response, user, settings)
    return {"status": "ok"}


@router.post("/login")
def login(body: LoginIn, response: Response, db: DbSession, settings: AppSettings) -> dict[str, str]:
    user = authenticate(db, body.email, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Wrong email or password.")
    _set_cookie(response, user, settings)
    return {"status": "ok"}


@router.post("/logout")
def logout(response: Response, settings: AppSettings) -> dict[str, str]:
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=MeOut)
def me(p: CurrentPrincipal) -> MeOut:
    return MeOut(
        id=p.user.id,
        email=p.user.email,
        display_name=p.user.display_name,
        workspace_id=p.workspace_id,
        role=p.role.value,
    )
