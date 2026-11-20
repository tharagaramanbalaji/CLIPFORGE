"""Transcripts and transcript segments (spec §5.4). `transcript_segments.idx` is the ID shown to the LLM."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from clipforge.db.base import Base
from clipforge.db.types import IdMixin, Json, JsonDict, TimestampMixin


class Transcript(IdMixin, TimestampMixin, Base):
    __tablename__ = "transcripts"
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    stage_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stage_runs.id", ondelete="SET NULL"))
    engine: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(16))
    config_hash: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[JsonDict] = mapped_column(Json, default=dict)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (
        UniqueConstraint("transcript_id", "idx"),
        Index("ix_transcript_segments_transcript_id_start_ms", "transcript_id", "start_ms"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transcript_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    speaker_label: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    words: Mapped[list[dict[str, Any]]] = mapped_column(Json)  # [{w, s, e, p, spk?}]
    confidence: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[list[str]] = mapped_column(Json, default=list)
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)


class Speaker(IdMixin, TimestampMixin, Base):
    __tablename__ = "speakers"
    __table_args__ = (UniqueConstraint("media_asset_id", "label"),)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(64))  # S1, S2, ... (stable within one diarization run)
    display_name: Mapped[str | None] = mapped_column(String(200))  # user rename, kept across re-runs by label
    total_speech_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    face_track_id: Mapped[str | None] = mapped_column(String(64))  # Phase 7


class Scene(Base):
    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("media_asset_id", "idx"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    start_frame: Mapped[int] = mapped_column(Integer)
    end_frame: Mapped[int] = mapped_column(Integer)
    keyframe_storage_key: Mapped[str | None] = mapped_column(String(1000))


class SignalSeries(IdMixin, TimestampMixin, Base):
    __tablename__ = "signal_series"
    __table_args__ = (UniqueConstraint("media_asset_id", "kind"),)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(
        String(32)
    )  # loudness, energy, speech_rate, laughter, excitement, music
    hop_ms: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String(32))
    values_storage_key: Mapped[str] = mapped_column(String(1000))  # float32 .npy
    stats: Mapped[JsonDict] = mapped_column(Json, default=dict)
