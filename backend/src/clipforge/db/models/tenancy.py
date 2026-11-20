"""Workspaces, users, creators and rights grants (spec §5.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from clipforge.db.base import Base
from clipforge.db.types import IdMixin, Json, JsonDict, TimestampMixin, pg_enum


class MemberRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class RightsBasis(StrEnum):
    OWN_CONTENT = "own_content"
    WRITTEN_PERMISSION = "written_permission"
    LICENSE_AGREEMENT = "license_agreement"
    PLATFORM_AUTHORIZED_INTEGRATION = "platform_authorized_integration"


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(200))
    settings: Mapped[JsonDict] = mapped_column(Json, default=dict)
    monthly_llm_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Membership(TimestampMixin, Base):
    __tablename__ = "memberships"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[MemberRole] = mapped_column(pg_enum(MemberRole, "member_role"))


class Creator(IdMixin, TimestampMixin, Base):
    __tablename__ = "creators"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    platform_handles: Mapped[JsonDict] = mapped_column(Json, default=dict)
    default_speaker_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)


class RightsGrant(IdMixin, TimestampMixin, Base):
    __tablename__ = "rights_grants"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("creators.id", ondelete="CASCADE"), index=True)
    basis: Mapped[RightsBasis] = mapped_column(pg_enum(RightsBasis, "rights_basis"))
    # platforms allowed, monetization allowed, attribution text, contains_music flag
    scope: Mapped[JsonDict] = mapped_column(Json, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_storage_key: Mapped[str | None] = mapped_column(String(1000))
    evidence_note: Mapped[str | None] = mapped_column(Text)
    recorded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def is_active_at(self, when: datetime) -> bool:
        if self.revoked_at is not None and self.revoked_at <= when:
            return False
        if self.valid_from > when:
            return False
        return self.valid_until is None or when < self.valid_until
