"""Phase 3 stage handlers: diarize (speakers -> words), scenes (+keyframes), signals (audio series)."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select

from clipforge.config import get_settings
from clipforge.db.models import (
    ArtifactKind,
    Creator,
    Scene,
    SignalSeries,
    Speaker,
    Transcript,
    TranscriptSegment,
)
from clipforge.engine.common.hashing import stable_hash
from clipforge.engine.common.tempfiles import scratch_dir
from clipforge.engine.diarize.assign import SpeakerTurn, assign_speakers
from clipforge.engine.scenes.detect import SceneConfig, detect_scenes, write_keyframes
from clipforge.engine.signals.audio import Series, energy_series, loudness_series, speech_rate_series
from clipforge.log import get_logger
from clipforge.pipeline.asr_config import cuda_usable
from clipforge.pipeline.orchestrator import StageContext
from clipforge.pipeline.stages import get_artifact, storage

log = get_logger(__name__)

DiarizeFn = Callable[[Path, Any, str | None], tuple[list[SpeakerTurn], dict[str, Any]]]
EventsFn = Callable[[Path, str], tuple[list[Series], dict[str, Any]]]


def _default_diarize(wav: Path, cfg: Any, token: str | None) -> tuple[list[SpeakerTurn], dict[str, Any]]:
    from clipforge.engine.diarize.pyannote_backend import diarize

    return diarize(wav, cfg, token)


def _default_events(wav: Path, device: str) -> tuple[list[Series], dict[str, Any]]:
    from clipforge.engine.signals.events import EventsConfig, detect_events

    return detect_events(wav, EventsConfig(model=get_settings().audio_events_model, device=device))


# Tests swap these for fakes so the pipeline runs without models.
diarize_fn: DiarizeFn = _default_diarize
events_fn: EventsFn = _default_events


def current_transcript(ctx: StageContext) -> Transcript | None:
    return ctx.session.scalar(
        select(Transcript)
        .where(Transcript.media_asset_id == ctx.asset.id, Transcript.is_current)
        .order_by(Transcript.created_at.desc())
        .limit(1)
    )


# --- diarize ---------------------------------------------------------------------------------------------


def diarize_stage(ctx: StageContext) -> dict[str, Any]:
    from clipforge.engine.diarize.pyannote_backend import DiarizeConfig

    settings = get_settings()
    token = settings.hf_token.get_secret_value() if settings.hf_token else ""
    if not token:
        log.warning("diarize_skipped_no_token", media_id=str(ctx.asset.id))
        return {
            "skipped_reason": "no_hf_token",
            "message": "Speaker detection skipped: set CLIPFORGE_HF_TOKEN (and accept the model terms).",
        }
    transcript = current_transcript(ctx)
    if transcript is None:
        return {"skipped_reason": "no_transcript"}
    creator = ctx.session.get(Creator, ctx.asset.project.creator_id)
    n = creator.default_speaker_count if creator else None
    cfg = DiarizeConfig(
        model=settings.diarize_model,
        device="cuda" if cuda_usable() else "cpu",
        min_speakers=n,
        max_speakers=n,
    )
    audio = get_artifact(ctx, ArtifactKind.AUDIO_WAV)
    ctx.report_progress(0.05)
    turns, metrics = diarize_fn(storage().local_path(audio.storage_key), cfg, token)
    ctx.report_progress(0.85)

    rows = list(
        ctx.session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.transcript_id == transcript.id)
            .order_by(TranscriptSegment.idx)
        )
    )
    result = assign_speakers([{"words": r.words} for r in rows], turns)
    for row, seg_spk, word_spks in zip(rows, result.segment_speakers, result.word_speakers, strict=True):
        row.speaker_label = seg_spk
        row.words = [{**w, "spk": spk} for w, spk in zip(row.words, word_spks, strict=True)]

    # Keep user renames (display_name) across re-runs, matched by label.
    old_names = {
        sp.label: sp.display_name
        for sp in ctx.session.scalars(select(Speaker).where(Speaker.media_asset_id == ctx.asset.id))
    }
    ctx.session.execute(delete(Speaker).where(Speaker.media_asset_id == ctx.asset.id))
    for label, total in sorted(result.totals_ms.items()):
        ctx.session.add(
            Speaker(
                media_asset_id=ctx.asset.id,
                label=label,
                display_name=old_names.get(label),
                total_speech_ms=total,
            )
        )

    key = f"assets/{ctx.asset.id}/diarization/{stable_hash(cfg.as_dict())[:16]}/turns.json"
    payload = [{"start_ms": t.start_ms, "end_ms": t.end_ms, "speaker": t.speaker} for t in turns]
    _put_bytes(ctx, key, json.dumps(payload).encode())
    return {
        **metrics,
        "device": cfg.device,
        "word_coverage": round(result.word_coverage, 4),
        "speech_ms": result.totals_ms,
        "turns_key": key,
    }


def _put_bytes(ctx: StageContext, key: str, data: bytes) -> None:
    settings = get_settings()
    with scratch_dir(settings.resolved_tmp_dir, prefix=f"blob-{ctx.stage_run.id}-") as tmp:
        p = tmp / "blob"
        p.write_bytes(data)
        storage().put_file(p, key, move=True)


# --- scenes ----------------------------------------------------------------------------------------------


def scenes_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    proxy = get_artifact(ctx, ArtifactKind.PROXY)
    cfg = SceneConfig(adaptive_threshold=settings.scene_threshold)
    video = storage().local_path(proxy.storage_key)
    scenes, metrics = detect_scenes(video, cfg)
    ctx.report_progress(0.7)
    prefix = f"assets/{ctx.asset.id}/scenes/{stable_hash(cfg.as_dict(), proxy.storage_key)[:16]}"
    keys: dict[int, str] = {}
    with scratch_dir(settings.resolved_tmp_dir, prefix=f"kf-{ctx.stage_run.id}-") as tmp:
        frames = write_keyframes(
            video, scenes, tmp, cfg.keyframe_quality, on_progress=lambda f: ctx.report_progress(0.7 + 0.3 * f)
        )
        for idx, path in frames.items():
            keys[idx] = f"{prefix}/{path.name}"
            storage().put_file(path, keys[idx], move=True)
    ctx.session.execute(delete(Scene).where(Scene.media_asset_id == ctx.asset.id))
    if scenes:
        ctx.session.execute(
            insert(Scene),
            [
                {
                    "media_asset_id": ctx.asset.id,
                    "idx": s.idx,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "start_frame": s.start_frame,
                    "end_frame": s.end_frame,
                    "keyframe_storage_key": keys.get(s.idx),
                }
                for s in scenes
            ],
        )
    return {**metrics, "keyframes": len(keys)}


# --- signals ---------------------------------------------------------------------------------------------


def _save_series(ctx: StageContext, series: Series, prefix: str) -> None:
    import numpy as np

    buf = io.BytesIO()
    np.save(buf, np.asarray(series.values, dtype=np.float32))
    key = f"{prefix}/{series.kind}.npy"
    _put_bytes(ctx, key, buf.getvalue())
    row = ctx.session.scalar(
        select(SignalSeries).where(
            SignalSeries.media_asset_id == ctx.asset.id, SignalSeries.kind == series.kind
        )
    )
    if row is None:
        row = SignalSeries(
            media_asset_id=ctx.asset.id,
            kind=series.kind,
            hop_ms=series.hop_ms,
            unit=series.unit,
            values_storage_key=key,
            stats={},
        )
        ctx.session.add(row)
    row.hop_ms, row.unit, row.values_storage_key, row.stats = series.hop_ms, series.unit, key, series.stats()


def signals_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    audio = get_artifact(ctx, ArtifactKind.AUDIO_WAV)
    wav = storage().local_path(audio.storage_key)
    duration = ctx.asset.duration_ms or 0
    series = [loudness_series(settings.ffmpeg_bin, wav, duration), energy_series(wav)]
    ctx.report_progress(0.2)
    transcript = current_transcript(ctx)
    starts: list[int] = []
    if transcript is not None:
        for words in ctx.session.scalars(
            select(TranscriptSegment.words).where(TranscriptSegment.transcript_id == transcript.id)
        ):
            starts.extend(int(w["s"]) for w in words)
    series.append(speech_rate_series(starts, duration))
    device = "cuda" if cuda_usable() else "cpu"
    events, ev_metrics = events_fn(wav, device)
    series.extend(events)
    ctx.report_progress(0.95)
    prefix = f"assets/{ctx.asset.id}/signals/{stable_hash(settings.audio_events_model, 1)[:16]}"
    for s in series:
        _save_series(ctx, s, prefix)
    return {"series": {s.kind: s.stats() for s in series}, "events": ev_metrics, "device": device}
