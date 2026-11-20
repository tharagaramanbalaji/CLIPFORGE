"""Stage handlers for the analysis pipeline (Phase 1: probe, mezzanine, proxy).

Handlers load inputs from the DB/storage, call pure engine functions, and stage DB changes on
ctx.session. The orchestrator commits on success and rolls back on failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update

from clipforge.config import Device, get_settings
from clipforge.db.models import Artifact, ArtifactKind, MediaAsset, Transcript, TranscriptSegment
from clipforge.engine.asr.base import Transcriber
from clipforge.engine.common.gpu import detect_gpus
from clipforge.engine.common.hashing import sha256_file, stable_hash
from clipforge.engine.common.tempfiles import scratch_dir
from clipforge.engine.media.audio import SAMPLE_RATE, extract_audio
from clipforge.engine.media.ffmpeg import ProgressFn
from clipforge.engine.media.probe import detect_vfr, parse_probe, probe_media
from clipforge.engine.media.tools import ffmpeg_capabilities
from clipforge.engine.media.transcode import (
    Encoder,
    MezzanineConfig,
    ProxyConfig,
    make_mezzanine,
    make_proxy,
    needs_mezzanine,
    target_fps,
)
from clipforge.engine.media.validate import IngestLimits, validate_probe
from clipforge.errors import ExternalToolError
from clipforge.log import get_logger
from clipforge.pipeline.asr_config import asr_config_for_this_host
from clipforge.pipeline.orchestrator import StageContext
from clipforge.storage import LocalStorage

log = get_logger(__name__)


def storage() -> LocalStorage:
    return LocalStorage(get_settings().data_dir)


@lru_cache
def resolve_encoder() -> Encoder:
    setting = get_settings().video_encoder
    if setting in ("nvenc", "x264"):
        return setting  # type: ignore[return-value]
    try:
        has_nvenc = bool(detect_gpus()) and ffmpeg_capabilities(get_settings().ffmpeg_bin).has_nvenc_h264
    except ExternalToolError:
        has_nvenc = False
    return "nvenc" if has_nvenc else "x264"


def get_artifact(ctx: StageContext, kind: ArtifactKind) -> Artifact:
    art = ctx.session.scalar(
        select(Artifact)
        .where(Artifact.media_asset_id == ctx.asset.id, Artifact.kind == kind)
        .order_by(Artifact.created_at.desc())
        .limit(1)
    )
    if art is None:
        raise ExternalToolError(f"missing {kind.value} artifact for media {ctx.asset.id}")
    return art


def upsert_artifact(
    ctx: StageContext, kind: ArtifactKind, key: str, config_hash: str, meta: dict[str, Any]
) -> Artifact:
    art = ctx.session.scalar(
        select(Artifact).where(
            Artifact.media_asset_id == ctx.asset.id,
            Artifact.kind == kind,
            Artifact.config_hash == config_hash,
            Artifact.storage_key == key,
        )
    )
    if art is None:
        art = Artifact(
            media_asset_id=ctx.asset.id, kind=kind, storage_key=key, config_hash=config_hash, size_bytes=0
        )
        ctx.session.add(art)
    art.size_bytes = storage().size(key)
    art.meta = meta
    art.produced_by_stage_run_id = ctx.stage_run.id
    return art


def _scaled(report: ProgressFn, start: float, end: float) -> ProgressFn:
    return lambda f: report(start + (end - start) * f)


# --- probe -----------------------------------------------------------------------------------------


def probe_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    original = get_artifact(ctx, ArtifactKind.ORIGINAL)
    path = storage().local_path(original.storage_key)

    def hash_progress(fraction: float) -> None:
        ctx.report_progress(0.8 * fraction)

    digest = sha256_file(path, on_progress=hash_progress)
    probe = probe_media(path, settings.ffprobe_bin)
    validate_probe(
        probe, IngestLimits(max_duration_s=settings.max_duration_s, min_duration_s=settings.min_duration_s)
    )
    vfr = detect_vfr(path, probe, settings.ffprobe_bin)
    ctx.report_progress(1.0)

    a: MediaAsset = ctx.asset
    assert probe.video is not None and probe.fps is not None
    a.sha256, original.sha256 = digest, digest
    a.duration_ms, a.width, a.height = probe.duration_ms, probe.video.width, probe.video.height
    a.fps_num, a.fps_den = probe.fps.numerator, probe.fps.denominator
    a.is_vfr, a.video_codec, a.container = vfr.is_vfr, probe.video.codec, probe.format_name
    if probe.audio is not None:
        a.audio_codec, a.audio_channels, a.audio_sample_rate = (
            probe.audio.codec,
            probe.audio.channels,
            probe.audio.sample_rate,
        )
    a.probe = {"ffprobe": probe.raw, "vfr": asdict(vfr)}

    duplicate = ctx.session.scalar(
        select(MediaAsset.id).where(MediaAsset.sha256 == digest, MediaAsset.id != a.id).limit(1)
    )
    return {
        "sha256": digest,
        "duration_ms": probe.duration_ms,
        "is_vfr": vfr.is_vfr,
        "vfr": asdict(vfr),
        "duplicate_of": str(duplicate) if duplicate else None,
    }


# --- mezzanine / proxy -----------------------------------------------------------------------------


def _encode_with_fallback(encode: Any, cfg: Any, x264_cfg: Any) -> Any:
    """NVENC can fail on odd inputs or when the consumer-GPU session cap is hit; fall back to x264."""
    try:
        encode(cfg)
        return cfg
    except ExternalToolError as exc:
        if cfg.encoder != "nvenc":
            raise
        log.warning("nvenc_failed_falling_back_to_x264", error=exc.message)
        encode(x264_cfg)
        return x264_cfg


def mezzanine_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    assert ctx.asset.probe is not None and ctx.asset.is_vfr is not None
    probe = parse_probe(ctx.asset.probe["ffprobe"])
    original = get_artifact(ctx, ArtifactKind.ORIGINAL)
    needed, reason = needs_mezzanine(probe, ctx.asset.is_vfr)

    if not needed:
        config_hash = stable_hash("passthrough", 1)
        upsert_artifact(
            ctx,
            ArtifactKind.MEZZANINE,
            original.storage_key,
            config_hash,
            {"passthrough": True, "reason": reason},
        )
        return {"transcoded": False, "reason": reason}

    cfg = MezzanineConfig(encoder=resolve_encoder())
    config_hash = stable_hash(cfg.as_dict())
    key = f"assets/{ctx.asset.id}/mezzanine/{config_hash[:16]}/mezzanine.mp4"
    store = storage()
    used = cfg
    if not store.exists(key):
        with scratch_dir(settings.resolved_tmp_dir, prefix=f"mez-{ctx.stage_run.id}-") as tmp:
            out = tmp / "mezzanine.mp4"
            src = store.local_path(original.storage_key)
            used = _encode_with_fallback(
                lambda c: make_mezzanine(settings.ffmpeg_bin, src, out, probe, c, ctx.report_progress),
                cfg,
                MezzanineConfig(encoder="x264"),
            )
            store.put_file(out, key, move=True)
    fps = target_fps(probe, cfg.max_fps)
    upsert_artifact(
        ctx,
        ArtifactKind.MEZZANINE,
        key,
        config_hash,
        {
            "passthrough": False,
            "reason": reason,
            "encoder": used.encoder,
            "fps_num": fps.numerator,
            "fps_den": fps.denominator,
        },
    )
    return {"transcoded": True, "reason": reason, "encoder": used.encoder}


def proxy_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    assert ctx.asset.probe is not None
    probe = parse_probe(ctx.asset.probe["ffprobe"])
    mezzanine = get_artifact(ctx, ArtifactKind.MEZZANINE)
    cfg = ProxyConfig(encoder=resolve_encoder(), height=settings.proxy_height)
    config_hash = stable_hash(cfg.as_dict(), mezzanine.storage_key)
    key = f"assets/{ctx.asset.id}/proxy/{config_hash[:16]}/proxy.mp4"
    store = storage()
    used = cfg
    if not store.exists(key):
        with scratch_dir(settings.resolved_tmp_dir, prefix=f"proxy-{ctx.stage_run.id}-") as tmp:
            out = tmp / "proxy.mp4"
            src: Path = store.local_path(mezzanine.storage_key)
            used = _encode_with_fallback(
                lambda c: make_proxy(settings.ffmpeg_bin, src, out, probe, c, ctx.report_progress),
                cfg,
                ProxyConfig(encoder="x264", height=settings.proxy_height),
            )
            store.put_file(out, key, move=True)
    upsert_artifact(
        ctx, ArtifactKind.PROXY, key, config_hash, {"encoder": used.encoder, "height": cfg.height}
    )
    return {"encoder": used.encoder}


# --- audio / transcription -------------------------------------------------------------------------


def audio_extract_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    mezzanine = get_artifact(ctx, ArtifactKind.MEZZANINE)
    config_hash = stable_hash("audio", SAMPLE_RATE, 1, mezzanine.storage_key)
    key = f"assets/{ctx.asset.id}/audio/{config_hash[:16]}/audio_16k.wav"
    store = storage()
    if not store.exists(key):
        with scratch_dir(settings.resolved_tmp_dir, prefix=f"audio-{ctx.stage_run.id}-") as tmp:
            out = tmp / "audio_16k.wav"
            extract_audio(
                settings.ffmpeg_bin,
                store.local_path(mezzanine.storage_key),
                out,
                ctx.asset.duration_ms or 0,
                ctx.report_progress,
            )
            store.put_file(out, key, move=True)
    upsert_artifact(
        ctx, ArtifactKind.AUDIO_WAV, key, config_hash, {"sample_rate": SAMPLE_RATE, "channels": 1}
    )
    return {"size_bytes": store.size(key)}


def _default_transcriber() -> Transcriber:
    from clipforge.engine.asr.whisperx_backend import WhisperXTranscriber

    return WhisperXTranscriber()


# Tests replace this with a fake so the pipeline can be exercised without models.
transcriber_factory: Callable[[], Transcriber] = _default_transcriber


def transcribe_stage(ctx: StageContext) -> dict[str, Any]:
    settings = get_settings()
    audio = get_artifact(ctx, ArtifactKind.AUDIO_WAV)
    cfg = asr_config_for_this_host()
    cpu_fallback = cfg.device == "cpu" and settings.device is not Device.CPU
    if cpu_fallback:
        log.warning("asr_cpu_fallback", model=cfg.model, media_id=str(ctx.asset.id))
    store = storage()
    result = transcriber_factory().transcribe(store.local_path(audio.storage_key), cfg, ctx.report_progress)

    config_hash = stable_hash(cfg.as_dict())
    key = f"assets/{ctx.asset.id}/transcript/{config_hash[:16]}/transcript.json"
    with scratch_dir(settings.resolved_tmp_dir, prefix=f"tr-{ctx.stage_run.id}-") as tmp:
        out = tmp / "transcript.json"
        out.write_text(json.dumps(result.as_json(), ensure_ascii=False), encoding="utf-8")
        store.put_file(out, key, move=True)
    upsert_artifact(
        ctx,
        ArtifactKind.TRANSCRIPT_JSON,
        key,
        config_hash,
        {"model": cfg.model, "device": cfg.device, "segments": len(result.segments)},
    )

    s = ctx.session
    s.execute(
        update(Transcript)
        .where(Transcript.media_asset_id == ctx.asset.id, Transcript.is_current)
        .values(is_current=False)
    )
    transcript = Transcript(
        media_asset_id=ctx.asset.id,
        stage_run_id=ctx.stage_run.id,
        engine=result.engine,
        model=result.model,
        language=result.language,
        config_hash=config_hash,
        is_current=True,
        segment_count=len(result.segments),
        word_count=result.word_count,
        metrics=result.metrics,
    )
    s.add(transcript)
    s.flush()
    if result.segments:
        s.execute(
            insert(TranscriptSegment),
            [
                {
                    "transcript_id": transcript.id,
                    "idx": seg.idx,
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "text": seg.text,
                    "words": [w.as_dict() for w in seg.words],
                    "confidence": seg.confidence,
                    "flags": list(seg.flags),
                }
                for seg in result.segments
            ],
        )
    return {
        **result.metrics,
        "device": cfg.device,
        "model": cfg.model,
        "compute_type": cfg.compute_type,
        "cpu_fallback": cpu_fallback,
        "transcript_id": str(transcript.id),
    }
