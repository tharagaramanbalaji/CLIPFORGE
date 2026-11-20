"""Clips and their immutable, reproducible render versions (spec §5.5)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from clipforge.db.base import Base
from clipforge.db.types import IdMixin, Json, JsonDict, TimestampMixin


class Clip(IdMixin, TimestampMixin, Base):
    __tablename__ = "clips"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("highlight_candidates.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    hook_text: Mapped[str | None] = mapped_column(String(120))
    start_ms: Mapped[int] = mapped_column(BigInteger)  # current (user-editable) bounds
    end_ms: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft|rendering|rendered|failed
    review: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )  # pending|approved|rejected
    reject_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clip_versions.id", ondelete="SET NULL", use_alter=True, name="fk_clips_current_version")
    )


class ClipVersion(IdMixin, TimestampMixin, Base):
    __tablename__ = "clip_versions"
    __table_args__ = (UniqueConstraint("clip_id", "version"),)
    clip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    render_spec: Mapped[JsonDict] = mapped_column(Json)  # final spec actually used (reproducible)
    render_spec_hash: Mapped[str] = mapped_column(String(64))
    preset: Mapped[str] = mapped_column(String(32))
    aspect: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued|rendering|ready|failed
    video_storage_key: Mapped[str | None] = mapped_column(String(1000))
    thumbnail_storage_key: Mapped[str | None] = mapped_column(String(1000))
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="SET NULL")
    )
    checks: Mapped[JsonDict] = mapped_column(Json, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(Json)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class SubtitleTrack(IdMixin, TimestampMixin, Base):
    """User-edited words for a clip (absolute ms on the media timeline).
    Without a row, the transcript is used."""

    __tablename__ = "subtitle_tracks"
    clip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), unique=True)
    words: Mapped[list[dict[str, Any]]] = mapped_column(Json)  # [{w, s, e}]
    edited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class AuditEvent(IdMixin, Base):
    """Who did what, when (spec §5.5). Written for every user action on clips."""

    __tablename__ = "audit_events"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(
        String(64)
    )  # clip.create, clip.update, clip.render, clip.approve, ...
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID] = mapped_column(index=True)
    data: Mapped[JsonDict] = mapped_column(Json, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
