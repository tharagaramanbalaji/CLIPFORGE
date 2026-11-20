"""Review dashboard API (Phase 8): approve/reject, audit trail, waveform, source preview, bulk download."""

from __future__ import annotations

import csv
import io
import itertools
import re
import uuid
import wave
import zipfile
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentPrincipal, DbSession, Principal, Store
from clipforge.api.routers.clips import ClipOut, _clip_out, get_clip
from clipforge.api.routers.media import get_media
from clipforge.db.models import Artifact, ArtifactKind, AuditEvent, Clip, ClipVersion
from clipforge.db.types import utcnow
from clipforge.engine.render.spec import RenderSpec
from clipforge.engine.subtitles.ass import to_srt
from clipforge.engine.subtitles.render_ass import captions_for
from clipforge.errors import Conflict, NotFound

router = APIRouter(tags=["review"])


def audit(
    db: Session, p: Principal, action: str, entity_type: str, entity_id: uuid.UUID, **data: Any
) -> None:
    """Record a user action (committed together with the change it describes)."""
    db.add(
        AuditEvent(
            workspace_id=p.workspace_id,
            actor_user_id=p.user.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            data={k: v for k, v in data.items() if v is not None},
        )
    )


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    action: str
    entity_type: str
    entity_id: uuid.UUID
    data: dict[str, Any]
    at: datetime


@router.post("/clips/{clip_id}/approve", response_model=ClipOut)
def approve(clip_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> ClipOut:
    clip = get_clip(db, p, clip_id)
    if clip.current_version_id is None:
        raise Conflict("Render the clip before approving it.")
    clip.review, clip.reject_reason, clip.reviewed_by, clip.reviewed_at = (
        "approved",
        None,
        p.user.id,
        utcnow(),
    )
    audit(db, p, "clip.approve", "clip", clip.id, version_id=str(clip.current_version_id))
    db.commit()
    return _clip_out(db, clip)


@router.post("/clips/{clip_id}/reject", response_model=ClipOut)
def reject(clip_id: uuid.UUID, body: RejectIn, db: DbSession, p: CurrentPrincipal) -> ClipOut:
    clip = get_clip(db, p, clip_id)
    clip.review, clip.reject_reason, clip.reviewed_by, clip.reviewed_at = (
        "rejected",
        body.reason,
        p.user.id,
        utcnow(),
    )
    audit(db, p, "clip.reject", "clip", clip.id, reason=body.reason)
    db.commit()
    return _clip_out(db, clip)


@router.post("/clips/{clip_id}/reopen", response_model=ClipOut)
def reopen(clip_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> ClipOut:
    clip = get_clip(db, p, clip_id)
    clip.review, clip.reject_reason, clip.reviewed_by, clip.reviewed_at = "pending", None, None, None
    audit(db, p, "clip.reopen", "clip", clip.id)
    db.commit()
    return _clip_out(db, clip)


@router.get("/audit", response_model=list[AuditOut])
def audit_log(
    db: DbSession,
    p: CurrentPrincipal,
    entity_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEvent]:
    q = select(AuditEvent).where(AuditEvent.workspace_id == p.workspace_id)
    if entity_id is not None:
        q = q.where(AuditEvent.entity_id == entity_id)
    return list(db.scalars(q.order_by(AuditEvent.at.desc()).limit(limit)))


def _artifact(db: DbSession, media_id: uuid.UUID, kind: ArtifactKind) -> Artifact:
    art = db.scalar(
        select(Artifact)
        .where(Artifact.media_asset_id == media_id, Artifact.kind == kind)
        .order_by(Artifact.created_at.desc())
        .limit(1)
    )
    if art is None:
        raise NotFound(f"No {kind.value} for this media yet.")
    return art


@router.get("/media/{media_id}/waveform")
def waveform(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    store: Store,
    from_ms: Annotated[int, Query(ge=0)],
    to_ms: Annotated[int, Query(ge=1)],
    points: Annotated[int, Query(ge=10, le=4000)] = 800,
) -> dict[str, Any]:
    """Peak amplitude (0..1) per bucket over [from_ms, to_ms), from the 16 kHz analysis audio."""
    asset = get_media(db, p, media_id)
    to_ms = min(to_ms, asset.duration_ms or to_ms)
    if to_ms <= from_ms:
        return {"from_ms": from_ms, "to_ms": to_ms, "peaks": []}
    import numpy as np

    path = store.local_path(_artifact(db, asset.id, ArtifactKind.AUDIO_WAV).storage_key)
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        w.setpos(min(w.getnframes(), from_ms * sr // 1000))
        pcm = np.frombuffer(w.readframes((to_ms - from_ms) * sr // 1000), dtype=np.int16).astype(np.float32)
    if pcm.size == 0:
        return {"from_ms": from_ms, "to_ms": to_ms, "peaks": []}
    n = min(points, pcm.size)
    edges = np.linspace(0, pcm.size, n + 1, dtype=int)
    peaks = [float(np.abs(pcm[a:b]).max()) / 32768 if b > a else 0.0 for a, b in itertools.pairwise(edges)]
    return {"from_ms": from_ms, "to_ms": to_ms, "peaks": [round(v, 3) for v in peaks]}


@router.get("/media/{media_id}/preview")
def preview(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, store: Store) -> FileResponse:
    """The 540p proxy (seekable): used by the trim editor to watch around a clip."""
    asset = get_media(db, p, media_id)
    return FileResponse(
        store.local_path(_artifact(db, asset.id, ArtifactKind.PROXY).storage_key), media_type="video/mp4"
    )


@router.get("/media/{media_id}/approved.zip")
def approved_zip(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, store: Store) -> Response:
    """Every approved clip's current version: MP4 + SRT + clips.csv (title, hook, description, times)."""
    asset = get_media(db, p, media_id)
    clips = list(
        db.scalars(
            select(Clip)
            .where(Clip.media_asset_id == asset.id, Clip.review == "approved")
            .order_by(Clip.start_ms)
        )
    )
    if not clips:
        raise Conflict("No approved clips yet.")
    buf, rows = io.BytesIO(), io.StringIO()
    table = csv.writer(rows)
    table.writerow(["file", "title", "hook_text", "description", "start_s", "end_s", "version", "preset"])
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:  # MP4s don't compress further
        for i, c in enumerate(clips, 1):
            v = db.get(ClipVersion, c.current_version_id) if c.current_version_id else None
            if v is None or v.video_storage_key is None or not store.exists(v.video_storage_key):
                continue
            base = f"{i:02d} " + (re.sub(r"[^A-Za-z0-9 _-]+", "", c.title)[:50].strip() or "clip")
            z.write(store.local_path(v.video_storage_key), f"{base}.mp4")
            caps = captions_for(RenderSpec.from_dict(v.render_spec))
            if caps:
                z.writestr(f"{base}.srt", to_srt(caps))
            table.writerow(
                [
                    f"{base}.mp4",
                    c.title,
                    c.hook_text or "",
                    c.description or "",
                    round(c.start_ms / 1000, 2),
                    round(c.end_ms / 1000, 2),
                    v.version,
                    v.preset,
                ]
            )
        z.writestr("clips.csv", rows.getvalue())
    audit(db, p, "media.export_approved", "media", asset.id, clips=len(clips))
    db.commit()
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", asset.original_filename.rsplit(".", 1)[0])[:60]
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}_approved.zip"'},
    )
