"""Password hashing and session tokens (JWT in an httpOnly cookie). Single-operator bootstrap."""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
from pwdlib import PasswordHash
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db.models import MemberRole, Membership, User, Workspace
from clipforge.db.types import utcnow
from clipforge.errors import Conflict, PermissionDenied
from clipforge.log import get_logger

_hasher = PasswordHash.recommended()
_ALG = "HS256"
# Docker Desktop/WSL2 VM clocks get stepped back slightly when they resync with the host, which made freshly
# issued tokens look "issued in the future" (ImmatureSignatureError) for a moment. Allow normal clock skew.
_CLOCK_LEEWAY_S = 60
log = get_logger(__name__)


def hash_password(password: str) -> str:
    return str(_hasher.hash(password))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(_hasher.verify(password, password_hash))
    except Exception:
        return False


def issue_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = utcnow()
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=settings.session_ttl_hours)}
    return jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=_ALG)


def read_token(token: str, settings: Settings) -> uuid.UUID | None:
    try:
        payload = jwt.decode(
            token, settings.secret_key.get_secret_value(), algorithms=[_ALG], leeway=_CLOCK_LEEWAY_S
        )
        return uuid.UUID(str(payload["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        log.debug("session_token_rejected", reason=type(exc).__name__, detail=str(exc)[:200])
        return None


def register_owner(
    session: Session, *, email: str, password: str, display_name: str, workspace_name: str
) -> User:
    """Creates the one owner account + workspace. Closed once any user exists (single operator)."""
    if session.scalar(select(func.count()).select_from(User)):
        raise PermissionDenied("Registration is closed: this ClipForge install already has an owner.")
    email = email.strip().lower()
    if session.scalar(select(User.id).where(User.email == email)):
        raise Conflict("A user with this email already exists.")
    ws = Workspace(name=workspace_name, settings={})
    user = User(email=email, password_hash=hash_password(password), display_name=display_name, is_active=True)
    session.add_all([ws, user])
    session.flush()
    session.add(Membership(workspace_id=ws.id, user_id=user.id, role=MemberRole.OWNER))
    session.commit()
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user
