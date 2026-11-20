"""Projects, media assets, upload sessions and artifacts (spec §5.2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clipforge.db.base import Base
from clipforge.db.types import IdMixin, Json, JsonDict, TimestampMixin, pg_enum


class MediaStatus(StrEnum):
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class SourceType(StrEnum):
    UPLOAD = "upload"
    INTEGRATION = "integration"


class ArtifactKind(StrEnum):
    ORIGINAL = "original"
    MEZZANINE = "mezzanine"
    PROXY = "proxy"
    AUDIO_WAV = "audio_wav"
    FRAME = "frame"
    TRANSCRIPT_JSON = "transcript_json"
    SCENE_LIST = "scene_list"
    SIGNALS = "signals"
    THUMBNAIL = "thumbnail"
    CLIP_VIDEO = "clip_video"
    CLIP_SUBTITLES = "clip_subtitles"
    CLIP_THUMBNAIL = "clip_thumbnail"
    EXPORT_BUNDLE = "export_bundle"


class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("creators.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="active")
    settings: Mapped[JsonDict] = mapped_column(Json, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAsset(IdMixin, TimestampMixin, Base):
    __tablename__ = "media_assets"
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    rights_grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rights_grants.id"))
    original_filename: Mapped[str] = mapped_column(String(500))
    source_type: Mapped[SourceType] = mapped_column(
        pg_enum(SourceType, "source_type"), default=SourceType.UPLOAD
    )
    source_ref: Mapped[str | None] = mapped_column(String(1000))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[MediaStatus] = mapped_column(
        pg_enum(MediaStatus, "media_status"), default=MediaStatus.UPLOADING
    )
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps_num: Mapped[int | None] = mapped_column(Integer)
    fps_den: Mapped[int | None] = mapped_column(Integer)
    is_vfr: Mapped[bool | None] = mapped_column(Boolean)
    video_codec: Mapped[str | None] = mapped_column(String(64))
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    audio_channels: Mapped[int | None] = mapped_column(Integer)
    audio_sample_rate: Mapped[int | None] = mapped_column(Integer)
    container: Mapped[str | None] = mapped_column(String(128))
    probe: Mapped[dict[str, Any] | None] = mapped_column(Json)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    error: Mapped[dict[str, Any] | None] = mapped_column(Json)

    project: Mapped[Project] = relationship(lazy="joined")


class UploadSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "upload_sessions"
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), unique=True
    )
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    chunk_size: Mapped[int] = mapped_column(Integer)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    parts: Mapped[list[int]] = mapped_column(Json, default=list)  # received chunk indexes
    storage_key: Mapped[str] = mapped_column(String(1000))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def total_chunks(self) -> int:
        return max(1, -(-self.total_bytes // self.chunk_size))

    def expected_chunk_len(self, n: int) -> int:
        return min(self.chunk_size, self.total_bytes - n * self.chunk_size)


class Artifact(IdMixin, TimestampMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("media_asset_id", "kind", "config_hash", "storage_key"),)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ArtifactKind] = mapped_column(pg_enum(ArtifactKind, "artifact_kind"))
    storage_key: Mapped[str] = mapped_column(String(1000))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    meta: Mapped[JsonDict] = mapped_column(Json, default=dict)
    produced_by_stage_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="SET NULL")
    )
