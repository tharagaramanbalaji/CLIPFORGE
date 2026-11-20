"""Media assets and pipeline runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from clipforge.api.deps import CurrentPrincipal, DbSession, Dispatcher
from clipforge.api.routers.catalog import get_project
from clipforge.db.models import (
    MediaAsset,
    MediaStatus,
    PipelineKind,
    PipelineRun,
    RunStatus,
    Transcript,
    TranscriptSegment,
)
from clipforge.db.types import utcnow
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.pipeline import orchestrator
from clipforge.services.uploads import require_active_grant

router = APIRouter(tags=["media"])


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StageOut(_Out):
    stage: str
    status: str
    attempt: int
    progress: float
    started_at: datetime | None
    finished_at: datetime | None
    metrics: dict[str, Any]
    error: dict[str, Any] | None


class RunOut(_Out):
    id: uuid.UUID
    media_asset_id: uuid.UUID
    kind: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    stages: list[StageOut]


class MediaOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    rights_grant_id: uuid.UUID
    original_filename: str
    status: str
    size_bytes: int
    sha256: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps_num: int | None
    fps_den: int | None
    is_vfr: bool | None
    video_codec: str | None
    audio_codec: str | None
    audio_channels: int | None
    audio_sample_rate: int | None
    container: str | None
    error: dict[str, Any] | None
    created_at: datetime
    latest_run: RunOut | None = None


def get_media(db: DbSession, p: CurrentPrincipal, media_id: uuid.UUID) -> MediaAsset:
    asset = db.get(MediaAsset, media_id)
    if asset is None or asset.project.workspace_id != p.workspace_id:
        raise NotFound("Media not found.")
    return asset


def _media_out(db: DbSession, asset: MediaAsset) -> MediaOut:
    out = MediaOut.model_validate(asset)
    run = orchestrator.latest_run(db, asset.id, PipelineKind.ANALYSIS)
    out.latest_run = RunOut.model_validate(run) if run else None
    return out


@router.get("/projects/{project_id}/media", response_model=list[MediaOut])
def list_media(project_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[MediaOut]:
    project = get_project(db, p, project_id)
    assets = db.scalars(
        select(MediaAsset)
        .where(MediaAsset.project_id == project.id, MediaAsset.status != MediaStatus.ARCHIVED)
        .order_by(MediaAsset.created_at.desc())
    )
    return [_media_out(db, a) for a in assets]


@router.get("/media/{media_id}", response_model=MediaOut)
def read_media(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> MediaOut:
    return _media_out(db, get_media(db, p, media_id))


@router.delete("/media/{media_id}", response_model=MediaOut)
def archive_media(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> MediaOut:
    """Soft delete. Files stay until the retention job (later phase) removes them."""
    asset = get_media(db, p, media_id)
    run = orchestrator.latest_run(db, asset.id, PipelineKind.ANALYSIS)
    if run is not None and not run.status.is_terminal:
        orchestrator.cancel_run(db, run)
    asset.status = MediaStatus.ARCHIVED
    db.commit()
    return _media_out(db, asset)


@router.post("/media/{media_id}/analyze", response_model=RunOut)
def analyze(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, dispatch: Dispatcher) -> PipelineRun:
    """Start a new analysis run. Stages whose inputs and config are unchanged are reused (skipped)."""
    asset = get_media(db, p, media_id)
    if asset.status in (MediaStatus.UPLOADING, MediaStatus.ARCHIVED):
        raise Conflict(f"Media is {asset.status.value}; it cannot be analyzed.")
    current = orchestrator.latest_run(db, asset.id, PipelineKind.ANALYSIS)
    if current is not None and not current.status.is_terminal:
        raise Conflict("An analysis run is already in progress for this media.")
    require_active_grant(db, asset.project, asset.rights_grant_id, utcnow())
    if asset.status is MediaStatus.FAILED:
        asset.status, asset.error = MediaStatus.VALIDATING, None
    run = orchestrator.create_run(db, asset, PipelineKind.ANALYSIS, requested_by=p.user.id)
    db.commit()
    return orchestrator.advance(db, run.id, dispatch)


def get_run(db: DbSession, p: CurrentPrincipal, run_id: uuid.UUID) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise NotFound("Pipeline run not found.")
    get_media(db, p, run.media_asset_id)
    return run


@router.get("/pipeline-runs/{run_id}", response_model=RunOut)
def read_run(run_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> PipelineRun:
    return get_run(db, p, run_id)


@router.post("/pipeline-runs/{run_id}/cancel", response_model=RunOut)
def cancel(run_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> PipelineRun:
    return orchestrator.cancel_run(db, get_run(db, p, run_id))


@router.post("/pipeline-runs/{run_id}/retry", response_model=RunOut)
def retry(run_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, dispatch: Dispatcher) -> PipelineRun:
    run = get_run(db, p, run_id)
    if run.status not in (RunStatus.FAILED, RunStatus.CANCELED):
        raise Conflict("Only failed or canceled runs can be retried.")
    asset = get_media(db, p, run.media_asset_id)
    require_active_grant(db, asset.project, asset.rights_grant_id, utcnow())
    return orchestrator.retry_run(db, run, dispatch)


# --- transcript ----------------------------------------------------------------------------------------


class WordOut(BaseModel):
    w: str
    s: int
    e: int
    p: float | None = None
    spk: str | None = None  # speaker label (after diarization)


class SegmentOut(_Out):
    idx: int
    start_ms: int
    end_ms: int
    speaker_label: str | None
    text: str
    confidence: float | None
    flags: list[str]
    words: list[WordOut] | None = None


class TranscriptOut(BaseModel):
    transcript_id: uuid.UUID
    engine: str
    model: str
    language: str
    segment_count: int
    word_count: int
    from_ms: int | None
    to_ms: int | None
    segments: list[SegmentOut]


@router.get("/media/{media_id}/transcript", response_model=TranscriptOut)
def read_transcript(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    from_ms: Annotated[int | None, Query(ge=0)] = None,
    to_ms: Annotated[int | None, Query(ge=0)] = None,
    include_words: bool = True,
) -> TranscriptOut:
    """Current transcript. With from_ms/to_ms, only segments overlapping [from_ms, to_ms) are returned."""
    asset = get_media(db, p, media_id)
    if from_ms is not None and to_ms is not None and to_ms <= from_ms:
        raise ValidationFailed("to_ms must be greater than from_ms.")
    transcript = db.scalar(
        select(Transcript)
        .where(Transcript.media_asset_id == asset.id, Transcript.is_current)
        .order_by(Transcript.created_at.desc())
        .limit(1)
    )
    if transcript is None:
        raise NotFound("No transcript yet for this media.")
    q = select(TranscriptSegment).where(TranscriptSegment.transcript_id == transcript.id)
    if from_ms is not None:
        q = q.where(TranscriptSegment.end_ms > from_ms)
    if to_ms is not None:
        q = q.where(TranscriptSegment.start_ms < to_ms)
    rows = db.scalars(q.order_by(TranscriptSegment.idx))
    segments = []
    for row in rows:
        seg = SegmentOut.model_validate(row)
        seg.words = [WordOut(**w) for w in row.words] if include_words else None
        segments.append(seg)
    return TranscriptOut(
        transcript_id=transcript.id,
        engine=transcript.engine,
        model=transcript.model,
        language=transcript.language,
        segment_count=transcript.segment_count,
        word_count=transcript.word_count,
        from_ms=from_ms,
        to_ms=to_ms,
        segments=segments,
    )
