"""Highlight detection: dry-run estimate, start a run, ranked candidates, LLM usage."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from clipforge.api.deps import CurrentPrincipal, DbSession, Dispatcher
from clipforge.api.routers.media import RunOut, get_media
from clipforge.db.models import (
    HighlightCandidate,
    LlmCall,
    MediaStatus,
    PipelineKind,
    PipelineRun,
    StageName,
    StageRun,
    StageStatus,
)
from clipforge.db.types import utcnow
from clipforge.engine.highlights.run import estimate
from clipforge.errors import Conflict, NotFound
from clipforge.pipeline import orchestrator
from clipforge.pipeline.stages_highlights import (
    build_inputs,
    current_transcript,
    highlight_config,
    run_settings_snapshot,
)
from clipforge.services.uploads import require_active_grant

router = APIRouter(tags=["highlights"])


class EstimateOut(BaseModel):
    backend: str
    discover_model: str
    refine_model: str
    windows_total: int
    windows_analyzed: int
    requests: int
    discover_requests: int
    refine_requests: int
    judge_requests: int = 0
    signal_moments: int = 0
    input_tokens: int
    output_tokens: int
    media_hours: float


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    rank: int | None
    final_score: float | None
    status: str
    start_ms: int
    end_ms: int
    start_segment_idx: int
    end_segment_idx: int
    category: str
    title: str | None
    hook_text: str | None
    hook: str
    description: str
    why_interesting: str
    standalone_context: str
    scores: dict[str, int]
    validation_errors: list[str]
    extra: dict[str, Any]
    created_at: datetime


class UsageOut(BaseModel):
    pipeline_run_id: uuid.UUID
    backend: str
    models: list[str]
    requests: int
    input_tokens: int
    output_tokens: int
    latency_s: float
    notional_cost_usd: float | None
    media_hours: float
    requests_per_media_hour: float | None
    tokens_per_media_hour: float | None


@router.post("/media/{media_id}/highlights", response_model=EstimateOut | RunOut)
def start_highlights(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    dispatch: Dispatcher,
    dry_run: bool = False,
    backend: Literal["claude_code", "ollama"] | None = None,
    content_type: Literal["talk", "gaming"] | None = None,
) -> Any:
    """`dry_run=true` returns the request/token estimate without calling any model."""
    asset = get_media(db, p, media_id)
    if asset.status is not MediaStatus.READY:
        raise Conflict(f"Media is {asset.status.value}; finish analysis first.")
    transcript = current_transcript(db, asset.id)
    if transcript is None:
        raise Conflict("No transcript yet.")
    config = run_settings_snapshot(transcript.id, backend, content_type)
    if dry_run:
        est = estimate(build_inputs(db, asset), highlight_config(config))
        return EstimateOut(
            backend=config["backend"],
            discover_model=config["discover_model"],
            refine_model=config["refine_model"],
            media_hours=round((asset.duration_ms or 0) / 3.6e6, 3),
            **est,
        )
    require_active_grant(db, asset.project, asset.rights_grant_id, utcnow())
    current = orchestrator.latest_run(db, asset.id, PipelineKind.HIGHLIGHTS)
    if current is not None and not current.status.is_terminal:
        raise Conflict("A highlights run is already in progress for this media.")
    run = orchestrator.create_run(db, asset, PipelineKind.HIGHLIGHTS, config=config, requested_by=p.user.id)
    db.commit()
    return RunOut.model_validate(orchestrator.advance(db, run.id, dispatch))


def _source_run_id(db: DbSession, run: PipelineRun) -> uuid.UUID:
    """Candidates belong to the run that actually produced them; a fully-reused run points back to it."""
    rank = db.scalar(
        select(StageRun).where(StageRun.pipeline_run_id == run.id, StageRun.stage == StageName.RANK)
    )
    if rank is not None and rank.status is StageStatus.SKIPPED:
        reused = db.get(StageRun, uuid.UUID(str(rank.metrics.get("reused_stage_run_id"))))
        if reused is not None:
            return reused.pipeline_run_id
    return run.id


def _latest_succeeded(db: DbSession, asset_id: uuid.UUID) -> PipelineRun | None:
    return db.scalar(
        select(PipelineRun)
        .where(
            PipelineRun.media_asset_id == asset_id,
            PipelineRun.kind == PipelineKind.HIGHLIGHTS,
            PipelineRun.status == "succeeded",
        )
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )


@router.get("/media/{media_id}/candidates", response_model=list[CandidateOut])
def list_candidates(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    include_rejected: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[HighlightCandidate]:
    """Ranked candidates of the latest successful highlights run (best first)."""
    asset = get_media(db, p, media_id)
    run = _latest_succeeded(db, asset.id)
    if run is None:
        raise NotFound("No completed highlights run for this media.")
    q = select(HighlightCandidate).where(HighlightCandidate.pipeline_run_id == _source_run_id(db, run))
    if not include_rejected:
        q = q.where(HighlightCandidate.rank.is_not(None))
    q = q.order_by(HighlightCandidate.rank.asc().nulls_last(), HighlightCandidate.start_ms).limit(limit)
    return list(db.scalars(q))


@router.get("/media/{media_id}/llm-usage", response_model=list[UsageOut])
def llm_usage(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[UsageOut]:
    asset = get_media(db, p, media_id)
    hours = (asset.duration_ms or 0) / 3.6e6
    rows = db.execute(
        select(
            StageRun.pipeline_run_id,
            LlmCall.backend,
            func.array_agg(func.distinct(LlmCall.model)),
            func.count(),
            func.sum(LlmCall.input_tokens + LlmCall.cache_read_tokens + LlmCall.cache_write_tokens),
            func.sum(LlmCall.output_tokens),
            func.sum(LlmCall.latency_ms),
            func.sum(LlmCall.cost_usd),
        )
        .join(StageRun, StageRun.id == LlmCall.stage_run_id)
        .where(LlmCall.media_asset_id == asset.id)
        .group_by(StageRun.pipeline_run_id, LlmCall.backend)
    ).all()
    out = []
    for run_id, backend, models, n, tin, tout, lat, cost in rows:
        tin, tout = int(tin or 0), int(tout or 0)
        out.append(
            UsageOut(
                pipeline_run_id=run_id,
                backend=backend,
                models=sorted(models),
                requests=n,
                input_tokens=tin,
                output_tokens=tout,
                latency_s=round((lat or 0) / 1000, 1),
                notional_cost_usd=round(float(cost), 4) if cost is not None else None,
                media_hours=round(hours, 3),
                requests_per_media_hour=round(n / hours, 1) if hours else None,
                tokens_per_media_hour=round((tin + tout) / hours) if hours else None,
            )
        )
    return out
