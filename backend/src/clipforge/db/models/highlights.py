"""LLM calls and highlight candidates (spec §5.5). Scores live on the candidate row (JSONB) instead of a
separate candidate_scores table: there is one scoring profile per run, so a join adds nothing yet."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from clipforge.db.base import Base
from clipforge.db.types import IdMixin, Json, JsonDict, TimestampMixin


class LlmCall(IdMixin, TimestampMixin, Base):
    __tablename__ = "llm_calls"
    stage_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stage_runs.id", ondelete="SET NULL"), index=True
    )
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32))  # discover | refine
    backend: Mapped[str] = mapped_column(String(32))  # claude_code | ollama | fake
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(64))
    window_idx: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)  # notional; plan usage is not billed per token
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_storage_key: Mapped[str | None] = mapped_column(String(1000))
    response_text: Mapped[str] = mapped_column(Text)


class HighlightCandidate(IdMixin, TimestampMixin, Base):
    __tablename__ = "highlight_candidates"
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), index=True
    )
    window_idx: Mapped[int] = mapped_column(Integer)
    start_segment_idx: Mapped[int] = mapped_column(Integer)  # what the model said (after refine)
    end_segment_idx: Mapped[int] = mapped_column(Integer)
    setup_segment_idx: Mapped[int | None] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(BigInteger)  # backend-resolved, then snapped to words
    end_ms: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(32))
    hook: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    why_interesting: Mapped[str] = mapped_column(Text)
    standalone_context: Mapped[str] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(200))
    hook_text: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32))  # proposed|rejected_validation|merged|refined|discarded
    scores: Mapped[dict[str, int]] = mapped_column(Json, default=dict)
    final_score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    validation_errors: Mapped[list[str]] = mapped_column(Json, default=list)
    extra: Mapped[JsonDict] = mapped_column(Json, default=dict)
    speaker_labels: Mapped[list[Any]] = mapped_column(Json, default=list)
