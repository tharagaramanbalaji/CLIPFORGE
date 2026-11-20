"""Pipeline runs and stage runs (spec §5.3). State lives here, not in Celery (ADR-0003)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from clipforge.db.base import Base
from clipforge.db.types import IdMixin, Json, JsonDict, TimestampMixin, pg_enum


class PipelineKind(StrEnum):
    ANALYSIS = "analysis"
    HIGHLIGHTS = "highlights"
    RENDER = "render"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED)


class StageName(StrEnum):
    PROBE = "probe"
    MEZZANINE = "mezzanine"
    PROXY = "proxy"
    AUDIO_EXTRACT = "audio_extract"
    TRANSCRIBE = "transcribe"
    ALIGN = "align"
    DIARIZE = "diarize"
    SCENES = "scenes"
    SIGNALS = "signals"
    HIGHLIGHT_WINDOWS = "highlight_windows"
    HIGHLIGHT_REFINE = "highlight_refine"
    RANK = "rank"
    REFRAME = "reframe"
    SUBTITLES = "subtitles"
    RENDER = "render"
    EXPORT = "export"


class StageStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELED = "canceled"

    @property
    def is_done(self) -> bool:
        return self in (StageStatus.SUCCEEDED, StageStatus.SKIPPED)


class PipelineRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "pipeline_runs"
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[PipelineKind] = mapped_column(pg_enum(PipelineKind, "pipeline_kind"))
    status: Mapped[RunStatus] = mapped_column(pg_enum(RunStatus, "run_status"), default=RunStatus.PENDING)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    config: Mapped[JsonDict] = mapped_column(Json, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(Json)

    stages: Mapped[list[StageRun]] = relationship(
        back_populates="run", order_by="StageRun.created_at", lazy="selectin"
    )


class StageRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "stage_runs"
    # The spec marks idempotency_key UNIQUE, but that would forbid running a stage again in a new pipeline
    # run. It is indexed instead; a new run reuses ("skips") an earlier succeeded stage run with the same key.
    __table_args__ = (UniqueConstraint("pipeline_run_id", "stage"),)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline_runs.id", ondelete="CASCADE"))
    stage: Mapped[StageName] = mapped_column(pg_enum(StageName, "stage_name"))
    status: Mapped[StageStatus] = mapped_column(
        pg_enum(StageStatus, "stage_status"), default=StageStatus.PENDING
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), index=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    worker_host: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[JsonDict] = mapped_column(Json, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(Json)

    run: Mapped[PipelineRun] = relationship(back_populates="stages")
