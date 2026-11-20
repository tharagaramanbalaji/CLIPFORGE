"""Phase 3 analysis results: speakers, scenes (+keyframes), signal series and a debug SVG plot."""

from __future__ import annotations

import html
import io
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from clipforge.api.deps import CurrentPrincipal, DbSession, Store
from clipforge.api.routers.media import get_media
from clipforge.db.models import Scene, SignalSeries, Speaker, Transcript, TranscriptSegment
from clipforge.errors import NotFound

router = APIRouter(tags=["analysis"])


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SpeakerOut(_Out):
    label: str
    display_name: str | None
    total_speech_ms: int


class SpeakerPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)


class SceneOut(_Out):
    idx: int
    start_ms: int
    end_ms: int
    start_frame: int
    end_frame: int
    has_keyframe: bool = False


class SeriesOut(BaseModel):
    kind: str
    unit: str
    hop_ms: int
    stats: dict[str, Any]
    step: int  # each returned value aggregates `step` original values (max), so hop_ms * step per point
    values: list[float]


@router.get("/media/{media_id}/speakers", response_model=list[SpeakerOut])
def list_speakers(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[Speaker]:
    asset = get_media(db, p, media_id)
    return list(db.scalars(select(Speaker).where(Speaker.media_asset_id == asset.id).order_by(Speaker.label)))


@router.patch("/media/{media_id}/speakers/{label}", response_model=SpeakerOut)
def rename_speaker(
    media_id: uuid.UUID, label: str, body: SpeakerPatch, db: DbSession, p: CurrentPrincipal
) -> Speaker:
    asset = get_media(db, p, media_id)
    sp = db.scalar(select(Speaker).where(Speaker.media_asset_id == asset.id, Speaker.label == label))
    if sp is None:
        raise NotFound("Speaker not found.")
    sp.display_name = body.display_name.strip() if body.display_name else None
    db.commit()
    return sp


@router.get("/media/{media_id}/scenes", response_model=list[SceneOut])
def list_scenes(media_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[SceneOut]:
    asset = get_media(db, p, media_id)
    rows = db.scalars(select(Scene).where(Scene.media_asset_id == asset.id).order_by(Scene.idx))
    out = []
    for r in rows:
        s = SceneOut.model_validate(r)
        s.has_keyframe = r.keyframe_storage_key is not None
        out.append(s)
    return out


@router.get("/media/{media_id}/scenes/{idx}/keyframe")
def scene_keyframe(
    media_id: uuid.UUID, idx: int, db: DbSession, p: CurrentPrincipal, store: Store
) -> FileResponse:
    asset = get_media(db, p, media_id)
    row = db.scalar(select(Scene).where(Scene.media_asset_id == asset.id, Scene.idx == idx))
    if row is None or row.keyframe_storage_key is None or not store.exists(row.keyframe_storage_key):
        raise NotFound("Keyframe not found.")
    return FileResponse(store.local_path(row.keyframe_storage_key), media_type="image/jpeg")


def _load_series(store: Store, row: SignalSeries, max_points: int) -> SeriesOut:
    import numpy as np

    values = np.load(io.BytesIO(store.local_path(row.values_storage_key).read_bytes()))
    step = max(1, -(-len(values) // max_points))
    if step > 1:
        pad = (-len(values)) % step
        padded = np.pad(values, (0, pad), constant_values=np.nan)
        values = np.nanmax(padded.reshape(-1, step), axis=1)
    return SeriesOut(
        kind=row.kind,
        unit=row.unit,
        hop_ms=row.hop_ms,
        stats=row.stats,
        step=step,
        values=[round(float(v), 3) for v in values],
    )


@router.get("/media/{media_id}/signals", response_model=list[SeriesOut])
def list_signals(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    store: Store,
    max_points: Annotated[int, Query(ge=10, le=20000)] = 2000,
) -> list[SeriesOut]:
    asset = get_media(db, p, media_id)
    rows = db.scalars(
        select(SignalSeries).where(SignalSeries.media_asset_id == asset.id).order_by(SignalSeries.kind)
    )
    return [_load_series(store, r, max_points) for r in rows]


# --- debug plot ------------------------------------------------------------------------------------------

_ORDER = ("loudness", "energy", "speech_rate", "laughter", "excitement", "music")
_COLORS = {
    "loudness": "#2563eb",
    "energy": "#0891b2",
    "speech_rate": "#16a34a",
    "laughter": "#d97706",
    "excitement": "#dc2626",
    "music": "#7c3aed",
}
_SPEAKER_COLORS = ("#2563eb", "#d97706", "#16a34a", "#dc2626", "#7c3aed", "#0891b2")


def _fmt_t(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


@router.get("/media/{media_id}/signals/plot", response_class=Response)
def plot_signals(
    media_id: uuid.UUID,
    db: DbSession,
    p: CurrentPrincipal,
    store: Store,
    width: Annotated[int, Query(ge=400, le=4000)] = 1400,
) -> Response:
    """Debug view: one lane per signal (normalized to its own min..max), scene cuts, speaker turns."""
    asset = get_media(db, p, media_id)
    duration = asset.duration_ms or 1
    rows = {
        r.kind: r for r in db.scalars(select(SignalSeries).where(SignalSeries.media_asset_id == asset.id))
    }
    kinds = [k for k in _ORDER if k in rows] + sorted(k for k in rows if k not in _ORDER)
    left, lane_h, gap, top = 110, 70, 14, 30
    plot_w = width - left - 20
    parts: list[str] = []
    y = top

    def x_of(ms: float) -> float:
        return left + plot_w * min(1.0, max(0.0, ms / duration))

    for kind in kinds:
        s = _load_series(store, rows[kind], plot_w)
        vals = [v for v in s.values if v == v]
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
        if s.unit == "probability":
            lo, hi = 0.0, 1.0
        span = (hi - lo) or 1.0
        pts = " ".join(
            f"{x_of(i * s.hop_ms * s.step):.1f},{y + lane_h - lane_h * (v - lo) / span:.1f}"
            for i, v in enumerate(s.values)
            if v == v
        )
        parts.append(
            f'<text x="8" y="{y + 16}" class="l">{html.escape(kind)}</text>'
            f'<text x="8" y="{y + 32}" class="u">{html.escape(s.unit)}</text>'
            f'<text x="8" y="{y + lane_h}" class="u">{lo:.1f}..{hi:.1f}</text>'
            f'<rect x="{left}" y="{y}" width="{plot_w}" height="{lane_h}" class="lane"/>'
            f'<polyline points="{pts}" fill="none" stroke="{_COLORS.get(kind, "#555")}" stroke-width="1"/>'
        )
        y += lane_h + gap

    # speakers lane from the current transcript's segments
    transcript = db.scalar(
        select(Transcript)
        .where(Transcript.media_asset_id == asset.id, Transcript.is_current)
        .order_by(Transcript.created_at.desc())
        .limit(1)
    )
    if transcript is not None:
        segs = db.execute(
            select(
                TranscriptSegment.start_ms, TranscriptSegment.end_ms, TranscriptSegment.speaker_label
            ).where(TranscriptSegment.transcript_id == transcript.id)
        ).all()
        labels = sorted({seg.speaker_label for seg in segs if seg.speaker_label})
        parts.append(
            f'<text x="8" y="{y + 14}" class="l">speakers</text>'
            f'<rect x="{left}" y="{y}" width="{plot_w}" height="20" class="lane"/>'
        )
        for seg in segs:
            color = _SPEAKER_COLORS[labels.index(seg.speaker_label) % 6] if seg.speaker_label else "#bbb"
            parts.append(
                f'<rect x="{x_of(seg.start_ms):.1f}" y="{y}" '
                f'width="{max(0.5, x_of(seg.end_ms) - x_of(seg.start_ms)):.1f}" height="20" fill="{color}"/>'
            )
        y += 20 + gap

    scene_rows = db.execute(
        select(Scene.start_ms).where(Scene.media_asset_id == asset.id, Scene.idx > 0)
    ).all()
    parts.append(f'<text x="8" y="{y + 12}" class="l">scene cuts</text>')
    for (start,) in scene_rows:
        parts.append(
            f'<line x1="{x_of(start):.1f}" x2="{x_of(start):.1f}" y1="{top}" y2="{y + 16}" class="cut"/>'
        )
    y += 24
    for i in range(11):
        ms = duration * i / 10
        parts.append(
            f'<text x="{x_of(ms):.1f}" y="{y + 12}" class="u" text-anchor="middle">{_fmt_t(ms)}</text>'
        )
    height = y + 24
    title = html.escape(f"{asset.original_filename} - signals ({len(kinds)}), scenes ({len(scene_rows) + 1})")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">'
        "<style>.l{font-size:12px;fill:#111}.u{font-size:10px;fill:#666}.lane{fill:#f6f6f6;stroke:#ddd}"
        ".cut{stroke:#111;stroke-opacity:.25;stroke-width:1}</style>"
        f'<rect width="100%" height="100%" fill="#fff"/><text x="8" y="18" class="l">{title}</text>'
        + "".join(parts)
        + "</svg>"
    )
    return Response(svg, media_type="image/svg+xml")
