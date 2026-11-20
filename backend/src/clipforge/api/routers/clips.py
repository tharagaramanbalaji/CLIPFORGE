"""Clips: create from candidates or a manual range, edit, render versions, serve video/thumbnail."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from clipforge.api.deps import CurrentPrincipal, DbSession, Dispatcher, Store
from clipforge.api.routers.media import get_media
from clipforge.db.models import (
    Clip,
    ClipVersion,
    HighlightCandidate,
    MediaStatus,
    PipelineKind,
    PipelineRun,
    SubtitleTrack,
    Transcript,
    TranscriptSegment,
)
from clipforge.db.types import utcnow
from clipforge.engine.render.runner import default_spec
from clipforge.engine.render.spec import PRESETS, RenderSpec
from clipforge.engine.subtitles.ass import to_srt, to_vtt
from clipforge.engine.subtitles.grouping import clip_words, group_words
from clipforge.engine.subtitles.render_ass import captions_for
from clipforge.engine.subtitles.styles import SAFE_AREAS, STYLES, max_line_chars
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.pipeline import orchestrator
from clipforge.services.uploads import require_active_grant

router = APIRouter(tags=["clips"])


class ClipsIn(BaseModel):
    candidate_ids: list[uuid.UUID] = Field(default_factory=list)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    title: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def one_source(self) -> ClipsIn:
        manual = self.start_ms is not None and self.end_ms is not None
        if bool(self.candidate_ids) == manual:
            raise ValueError("give either candidate_ids or start_ms+end_ms")
        return self


class ClipPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    hook_text: str | None = Field(default=None, max_length=120)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)


class RenderIn(BaseModel):
    preset: Literal["shorts", "reels", "tiktok", "wide", "square"] = "shorts"
    frame_mode: Literal["auto", "crop", "blur_pad", "facecam_split", "two_stack"] = "auto"
    captions_below: bool = True  # blur_pad: put captions in the blurred band under the picture
    facecam: Literal["top_left", "top_right", "bottom_left", "bottom_right"] | list[float] | None = None
    gameplay: list[float] | None = None  # facecam_split: region of the source shown below (default: whole)
    top_ratio: float = Field(default=0.35, ge=0.2, le=0.6)  # facecam_split: share of height for the facecam
    subtitle_style: Literal["bold_pop", "clean_box", "karaoke", "none"] = "bold_pop"
    hook_text: bool = True  # show the clip's hook text at the top for the first seconds
    watermark: str | None = Field(default=None, max_length=60)


class WordIn(BaseModel):
    w: str = Field(min_length=1, max_length=60)
    s: int = Field(ge=0)
    e: int = Field(ge=0)


class SubtitlesIn(BaseModel):
    words: list[WordIn] = Field(max_length=5000)


class SubtitlesOut(BaseModel):
    edited: bool
    words: list[dict[str, Any]]
    captions: list[dict[str, Any]]  # preview grouping (default style, shorts)


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: int
    preset: str
    aspect: str
    status: str
    render_spec: dict[str, Any]
    checks: dict[str, Any]
    error: dict[str, Any] | None
    created_at: datetime


class ClipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    media_asset_id: uuid.UUID
    candidate_id: uuid.UUID | None
    title: str
    hook_text: str | None
    description: str | None
    start_ms: int
    end_ms: int
    status: str
    current_version_id: uuid.UUID | None
    review: str = "pending"
    reject_reason: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    latest_version: VersionOut | None = None


def _version_status(db: DbSession, v: ClipVersion) -> VersionOut:
    out = VersionOut.model_validate(v)
    if v.status != "ready" and v.pipeline_run_id:
        run = db.get(PipelineRun, v.pipeline_run_id)
        if run is not None and run.status.value in ("failed", "canceled"):
            out.status, out.error = "failed", run.error
        elif run is not None and run.status.value == "running":
            out.status = "rendering"
    return out


def _clip_out(db: DbSession, c: Clip) -> ClipOut:
    out = ClipOut.model_validate(c)
    v = db.scalar(
        select(ClipVersion).where(ClipVersion.clip_id == c.id).order_by(ClipVersion.version.desc()).limit(1)
    )
    if v is not None:
        out.latest_version = _version_status(db, v)
        if out.latest_version.status == "failed" and c.status == "rendering":
            out.status = "failed"
    return out


def _audit(db: DbSession, p: CurrentPrincipal, action: str, etype: str, eid: uuid.UUID, **data: Any) -> None:
    from clipforge.api.routers.review import audit

    audit(db, p, action, etype, eid, **data)


def get_clip(db: DbSession, p: CurrentPrincipal, clip_id: uuid.UUID) -> Clip:
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise NotFound("Clip not found.")
    get_media(db, p, clip.media_asset_id)  # workspace check
    return clip


@router.post("/media/{media_id}/clips", response_model=list[ClipOut], status_code=status.HTTP_201_CREATED)
def create_clips(media_id: uuid.UUID, body: ClipsIn, db: DbSession, p: CurrentPrincipal) -> list[ClipOut]:
    asset = get_media(db, p, media_id)
    if asset.status is not MediaStatus.READY:
        raise Conflict(f"Media is {asset.status.value}; finish analysis first.")
    clips: list[Clip] = []
    if body.candidate_ids:
        for cid in body.candidate_ids:
            cand = db.get(HighlightCandidate, cid)
            if cand is None or cand.media_asset_id != asset.id:
                raise NotFound(f"Candidate {cid} not found for this media.")
            clips.append(
                Clip(
                    project_id=asset.project_id,
                    media_asset_id=asset.id,
                    candidate_id=cand.id,
                    title=(cand.title or cand.hook or "Clip")[:200],
                    hook_text=cand.hook_text,
                    description=cand.description,
                    start_ms=cand.start_ms,
                    end_ms=cand.end_ms,
                )
            )
    else:
        assert body.start_ms is not None and body.end_ms is not None
        _check_bounds(body.start_ms, body.end_ms, asset.duration_ms or 0)
        clips.append(
            Clip(
                project_id=asset.project_id,
                media_asset_id=asset.id,
                title=body.title or "Clip",
                start_ms=body.start_ms,
                end_ms=body.end_ms,
            )
        )
    db.add_all(clips)
    db.flush()
    for c in clips:
        _audit(
            db,
            p,
            "clip.create",
            "clip",
            c.id,
            candidate_id=str(c.candidate_id) if c.candidate_id else None,
            start_ms=c.start_ms,
            end_ms=c.end_ms,
        )
    db.commit()
    return [_clip_out(db, c) for c in clips]


def _check_bounds(start_ms: int, end_ms: int, duration_ms: int) -> None:
    if not 0 <= start_ms < end_ms <= duration_ms:
        raise ValidationFailed(f"Clip bounds must satisfy 0 <= start < end <= {duration_ms} ms.")
    if end_ms - start_ms < 1000:
        raise ValidationFailed("Clips must be at least 1 second long.")


@router.get("/media/{media_id}/clips", response_model=list[ClipOut])
def list_clips(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[ClipOut]:
    asset = get_media(db, p, media_id)
    rows = db.scalars(select(Clip).where(Clip.media_asset_id == asset.id).order_by(Clip.start_ms))
    return [_clip_out(db, c) for c in rows]


@router.patch("/clips/{clip_id}", response_model=ClipOut)
def update_clip(clip_id: uuid.UUID, body: ClipPatch, db: DbSession, p: CurrentPrincipal) -> ClipOut:
    clip = get_clip(db, p, clip_id)
    asset = get_media(db, p, clip.media_asset_id)
    start = body.start_ms if body.start_ms is not None else clip.start_ms
    end = body.end_ms if body.end_ms is not None else clip.end_ms
    _check_bounds(start, end, asset.duration_ms or 0)
    clip.start_ms, clip.end_ms = start, end
    if body.title is not None:
        clip.title = body.title
    if body.hook_text is not None:
        clip.hook_text = body.hook_text
    _audit(db, p, "clip.update", "clip", clip.id, **body.model_dump(exclude_none=True))
    db.commit()
    return _clip_out(db, clip)


def _layout(body: RenderIn, src_w: int, src_h: int) -> dict[str, Any]:
    from clipforge.engine.render.layouts import FACECAM_CORNERS, check_rect

    lay: dict[str, Any] = {"src_aspect": round(src_w / src_h, 4)}
    try:
        if body.frame_mode == "blur_pad" and body.captions_below:
            lay["captions"] = "below"
        if body.frame_mode == "facecam_split":
            if body.facecam is None:
                raise ValidationFailed("facecam_split needs a facecam corner or region [x, y, w, h].")
            lay["facecam"] = (
                FACECAM_CORNERS[body.facecam] if isinstance(body.facecam, str) else check_rect(body.facecam)
            )
            lay["top_ratio"] = body.top_ratio
            if body.gameplay:
                lay["gameplay"] = check_rect(body.gameplay)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return lay


@router.post("/clips/{clip_id}/render", response_model=ClipOut, status_code=status.HTTP_202_ACCEPTED)
def render(
    clip_id: uuid.UUID, body: RenderIn, db: DbSession, p: CurrentPrincipal, dispatch: Dispatcher
) -> ClipOut:
    """Each render is a new immutable version; old versions are kept."""
    clip = get_clip(db, p, clip_id)
    asset = get_media(db, p, clip.media_asset_id)
    require_active_grant(
        db, asset.project, asset.rights_grant_id, utcnow()
    )  # expired/revoked rights block renders
    preset = PRESETS[body.preset]
    if (clip.end_ms - clip.start_ms) / 1000 > preset.max_duration_s:
        raise ValidationFailed(f"Clip is longer than {preset.name}'s {preset.max_duration_s:.0f} s limit.")
    from fractions import Fraction

    src_fps = Fraction(asset.fps_num or 30, asset.fps_den or 1)
    # Ask for NVENC; the render worker (which has the GPU) falls back to x264 if it isn't available.
    spec = default_spec(clip.start_ms, clip.end_ms, src_fps, body.preset, body.frame_mode, "nvenc")
    spec = replace(spec, extra={"layout": _layout(body, asset.width or 16, asset.height or 9)})
    if body.subtitle_style != "none" or body.hook_text or body.watermark:
        words = (
            clip_words(
                clip_word_source(db, clip)[0],
                int(spec.start_s * 1000),
                int(spec.start_s * 1000) + int(spec.duration_s * 1000),
            )
            if body.subtitle_style != "none"
            else []
        )
        spec = replace(
            spec,
            extra={
                **spec.extra,
                "subtitles": {
                    "style": body.subtitle_style if body.subtitle_style != "none" else "bold_pop",
                    "words": [{"w": w.text, "s": w.s, "e": w.e} for w in words],
                    "hook_text": clip.hook_text if body.hook_text else None,
                    "hook_ms": 3000,
                    "watermark": body.watermark,
                },
            },
        )
    n = (db.scalar(select(func.max(ClipVersion.version)).where(ClipVersion.clip_id == clip.id)) or 0) + 1
    version = ClipVersion(
        clip_id=clip.id,
        version=n,
        render_spec=spec.as_dict(),
        render_spec_hash=spec.hash(),
        preset=preset.name,
        aspect=preset.aspect,
        status="queued",
        checks={},
        created_by=p.user.id,
    )
    db.add(version)
    db.flush()
    run = orchestrator.create_run(
        db, asset, PipelineKind.RENDER, config={"clip_version_id": str(version.id)}, requested_by=p.user.id
    )
    version.pipeline_run_id, clip.status = run.id, "rendering"
    _audit(
        db,
        p,
        "clip.render",
        "clip",
        clip.id,
        version=n,
        preset=body.preset,
        frame_mode=body.frame_mode,
        subtitle_style=body.subtitle_style,
    )
    db.commit()
    orchestrator.advance(db, run.id, dispatch)
    db.refresh(clip)
    return _clip_out(db, clip)


@router.get("/clips/{clip_id}/versions", response_model=list[VersionOut])
def list_versions(clip_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[VersionOut]:
    clip = get_clip(db, p, clip_id)
    rows = db.scalars(
        select(ClipVersion).where(ClipVersion.clip_id == clip.id).order_by(ClipVersion.version.desc())
    )
    return [_version_status(db, v) for v in rows]


def _version_file(
    db: DbSession, p: CurrentPrincipal, store: Store, version_id: uuid.UUID, kind: str
) -> FileResponse:
    v = db.get(ClipVersion, version_id)
    if v is None:
        raise NotFound("Clip version not found.")
    get_clip(db, p, v.clip_id)
    key = v.video_storage_key if kind == "video" else v.thumbnail_storage_key
    if not key or not store.exists(key):
        raise NotFound("Not rendered yet.")
    media_type = "video/mp4" if kind == "video" else "image/jpeg"
    return FileResponse(store.local_path(key), media_type=media_type)  # supports HTTP Range (seeking)


@router.get("/clip-versions/{version_id}/video")
def version_video(version_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, store: Store) -> FileResponse:
    return _version_file(db, p, store, version_id, "video")


@router.get("/clip-versions/{version_id}/thumbnail")
def version_thumbnail(
    version_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, store: Store
) -> FileResponse:
    return _version_file(db, p, store, version_id, "thumbnail")


# --- subtitles ------------------------------------------------------------------------------------------


def clip_word_source(db: DbSession, clip: Clip) -> tuple[list[dict[str, Any]], bool]:
    """(words in absolute ms, edited?): the clip's edited track if any,
    else the current transcript's words."""
    track = db.scalar(select(SubtitleTrack).where(SubtitleTrack.clip_id == clip.id))
    if track is not None:
        return list(track.words), True
    transcript = db.scalar(
        select(Transcript)
        .where(Transcript.media_asset_id == clip.media_asset_id, Transcript.is_current)
        .order_by(Transcript.created_at.desc())
    )
    if transcript is None:
        return [], False
    rows = db.scalars(
        select(TranscriptSegment)
        .where(
            TranscriptSegment.transcript_id == transcript.id,
            TranscriptSegment.end_ms > clip.start_ms - 2000,
            TranscriptSegment.start_ms < clip.end_ms + 2000,
        )
        .order_by(TranscriptSegment.idx)
    )
    words = [
        {"w": w["w"], "s": int(w["s"]), "e": int(w["e"])}
        for r in rows
        for w in r.words
        if clip.start_ms <= (int(w["s"]) + int(w["e"])) / 2 < clip.end_ms
    ]
    return words, False


def _subtitles_out(db: DbSession, clip: Clip) -> SubtitlesOut:
    words, edited = clip_word_source(db, clip)
    style, preset = STYLES["bold_pop"], PRESETS["shorts"]
    caps = group_words(
        clip_words(words, clip.start_ms, clip.end_ms),
        max_words=style.max_words,
        max_chars=max_line_chars(style, preset.width, SAFE_AREAS["shorts"]),
    )
    return SubtitlesOut(
        edited=edited,
        words=words,
        captions=[
            {
                "start_ms": c.start + clip.start_ms,
                "end_ms": c.end + clip.start_ms,
                "text": c.text,
                "first_word": i,
            }
            for i, c in enumerate(caps)
        ],
    )


@router.get("/clips/{clip_id}/subtitles", response_model=SubtitlesOut)
def get_subtitles(clip_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> SubtitlesOut:
    return _subtitles_out(db, get_clip(db, p, clip_id))


@router.put("/clips/{clip_id}/subtitles", response_model=SubtitlesOut)
def put_subtitles(clip_id: uuid.UUID, body: SubtitlesIn, db: DbSession, p: CurrentPrincipal) -> SubtitlesOut:
    """Replace the clip's subtitle words (absolute ms). Takes effect on the next render."""
    clip = get_clip(db, p, clip_id)
    prev_s = -1
    for w in body.words:
        if w.e <= w.s or w.s < prev_s:
            raise ValidationFailed("Words must be in time order, each with end > start.")
        if not clip.start_ms - 1000 <= w.s <= clip.end_ms + 1000:
            raise ValidationFailed(f"Word '{w.w}' is outside the clip.")
        prev_s = w.s
    track = db.scalar(select(SubtitleTrack).where(SubtitleTrack.clip_id == clip.id))
    if track is None:
        track = SubtitleTrack(clip_id=clip.id, words=[], edited_by=p.user.id)
        db.add(track)
    track.words, track.edited_by = [w.model_dump() for w in body.words], p.user.id
    _audit(db, p, "clip.subtitles_edit", "clip", clip.id, words=len(body.words))
    db.commit()
    return _subtitles_out(db, clip)


@router.delete("/clips/{clip_id}/subtitles", response_model=SubtitlesOut)
def reset_subtitles(clip_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> SubtitlesOut:
    """Drop edits and go back to the transcript's words."""
    clip = get_clip(db, p, clip_id)
    track = db.scalar(select(SubtitleTrack).where(SubtitleTrack.clip_id == clip.id))
    if track is not None:
        db.delete(track)
        _audit(db, p, "clip.subtitles_reset", "clip", clip.id)
        db.commit()
    return _subtitles_out(db, clip)


@router.get("/clip-versions/{version_id}/subtitles.{fmt}")
def version_subtitles(
    version_id: uuid.UUID, fmt: Literal["srt", "vtt"], db: DbSession, p: CurrentPrincipal
) -> Response:
    """SRT/VTT of exactly what was burned into this version (clip-relative times)."""
    v = db.get(ClipVersion, version_id)
    if v is None:
        raise NotFound("Clip version not found.")
    get_clip(db, p, v.clip_id)
    caps = captions_for(RenderSpec.from_dict(v.render_spec))
    text = to_srt(caps) if fmt == "srt" else to_vtt(caps)
    return Response(text, media_type="application/x-subrip" if fmt == "srt" else "text/vtt")


# --- editor export (Premiere / Resolve / Final Cut) ---------------------------------------------------------


@router.get("/media/{media_id}/export/premiere")
def export_premiere(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    source_path: str | None = None,
    top: int = 20,
) -> Response:
    """ZIP with a Final Cut Pro 7 XML project (Premiere: File > Import) + one SRT per clip + README.
    `source_path` = where the original video is on your editing PC (else Premiere asks you to locate it)."""
    import io
    import re
    import zipfile
    from fractions import Fraction

    from clipforge.engine.export.premiere import Moment, Source, build_project_xml
    from clipforge.engine.subtitles.ass import to_srt

    asset = get_media(db, p, media_id)
    if not asset.width or not asset.height or not asset.duration_ms:
        raise Conflict("Media isn't analyzed yet.")
    src = Source(
        filename=asset.original_filename,
        duration_ms=asset.duration_ms,
        width=asset.width,
        height=asset.height,
        fps=Fraction(asset.fps_num or 30, asset.fps_den or 1),
        local_path=source_path,
    )
    run = db.scalar(
        select(PipelineRun)
        .where(
            PipelineRun.media_asset_id == asset.id,
            PipelineRun.kind == PipelineKind.HIGHLIGHTS,
            PipelineRun.status == "succeeded",
        )
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    highlights: list[Moment] = []
    if run is not None:
        from clipforge.api.routers.highlights import _source_run_id

        cands = db.scalars(
            select(HighlightCandidate)
            .where(
                HighlightCandidate.pipeline_run_id == _source_run_id(db, run),
                HighlightCandidate.rank.is_not(None),
            )
            .order_by(HighlightCandidate.rank)
            .limit(top)
        )
        highlights = [
            Moment(
                c.start_ms,
                c.end_ms,
                f"#{c.rank} {c.title or c.hook}"[:120],
                f"score {c.final_score}, {c.category}: {c.description}"[:500],
            )
            for c in cands
        ]
    clips = list(db.scalars(select(Clip).where(Clip.media_asset_id == asset.id).order_by(Clip.start_ms)))
    if not highlights and not clips:
        raise Conflict("Nothing to export yet: find highlights or make clips first.")
    xml = build_project_xml(
        f"ClipForge - {asset.original_filename}",
        src,
        highlights,
        [Moment(c.start_ms, c.end_ms, c.title[:120]) for c in clips],
    )
    style, preset = STYLES["bold_pop"], PRESETS["shorts"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("clipforge_project.xml", xml)
        for i, c in enumerate(clips, 1):
            words, _ = clip_word_source(db, c)
            caps = group_words(
                clip_words(words, c.start_ms, c.end_ms),
                max_words=style.max_words,
                max_chars=max_line_chars(style, preset.width, SAFE_AREAS["shorts"]),
            )
            safe = re.sub(r"[^A-Za-z0-9 _-]+", "", c.title)[:40].strip() or "clip"
            z.writestr(f"captions/{i:02d} {safe}.srt", to_srt(caps))
        z.writestr(
            "README.txt",
            (
                "Premiere Pro: File > Import > clipforge_project.xml\n"
                "- 'All highlights': every ranked moment cut back-to-back, with a marker per moment.\n"
                "- One 9x16 sequence per clip (1080x1920, scaled to fill).\n"
                "  Captions: File > Import the matching .srt from captions/ and drag it onto that sequence\n"
                "  (it starts at 00:00:00).\n"
                f"- Source media: {asset.original_filename}. If Premiere shows it offline,\n"
                "  right-click > Link Media and pick your copy of that file once; every sequence relinks.\n"
            ),
        )
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", asset.original_filename.rsplit(".", 1)[0])[:60]
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}_premiere.zip"'},
    )


# --- auto-edit ---------------------------------------------------------------------------------------


class AutoEditIn(BaseModel):
    count: int = Field(default=5, ge=1, le=20)
    preset: Literal["shorts", "reels", "tiktok", "wide", "square"] = "shorts"
    subtitle_style: Literal["bold_pop", "clean_box", "karaoke", "none"] = "bold_pop"
    hook_text: bool = True
    watermark: str | None = Field(default=None, max_length=60)


@router.post(
    "/media/{media_id}/auto-edit", response_model=list[ClipOut], status_code=status.HTTP_202_ACCEPTED
)
def auto_edit(
    media_id: uuid.UUID, body: AutoEditIn, db: DbSession, p: CurrentPrincipal, dispatch: Dispatcher
) -> list[ClipOut]:
    """Finished clips from the top highlights: per clip, facecam on top + gameplay below when one facecam
    box is
    on screen for most of the clip, else the whole picture with captions under it; captions, hook text,
    watermark, loudness. Clips already made from a candidate are re-rendered, not duplicated."""
    from clipforge.api.routers.highlights import _source_run_id

    asset = get_media(db, p, media_id)
    run = db.scalar(
        select(PipelineRun)
        .where(
            PipelineRun.media_asset_id == asset.id,
            PipelineRun.kind == PipelineKind.HIGHLIGHTS,
            PipelineRun.status == "succeeded",
        )
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        raise Conflict("Find highlights first.")
    cands = list(
        db.scalars(
            select(HighlightCandidate)
            .where(
                HighlightCandidate.pipeline_run_id == _source_run_id(db, run),
                HighlightCandidate.rank.is_not(None),
            )
            .order_by(HighlightCandidate.rank)
            .limit(body.count)
        )
    )
    existing = {c.candidate_id: c for c in db.scalars(select(Clip).where(Clip.media_asset_id == asset.id))}
    new = [cand for cand in cands if cand.id not in existing]
    if new:
        create_clips(media_id, ClipsIn(candidate_ids=[c.id for c in new]), db, p)
        existing = {
            c.candidate_id: c for c in db.scalars(select(Clip).where(Clip.media_asset_id == asset.id))
        }
    out = []
    for cand in cands:
        clip = existing[cand.id]
        out.append(
            render(
                clip.id,
                RenderIn(
                    preset=body.preset,
                    frame_mode="auto",
                    subtitle_style=body.subtitle_style,
                    hook_text=body.hook_text,
                    watermark=body.watermark,
                ),
                db,
                p,
                dispatch,
            )
        )
    return out
