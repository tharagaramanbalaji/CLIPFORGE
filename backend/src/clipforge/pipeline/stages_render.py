"""Render pipeline stage: one clip version -> MP4 + thumbnail, verified against the platform preset."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from clipforge.config import get_settings
from clipforge.db.models import ArtifactKind, Clip, ClipVersion
from clipforge.engine.common.tempfiles import scratch_dir
from clipforge.engine.render.runner import render_clip, render_thumbnail, verify_output
from clipforge.engine.render.spec import RenderSpec
from clipforge.errors import ClipForgeError
from clipforge.pipeline.orchestrator import StageContext
from clipforge.pipeline.stages import get_artifact, resolve_encoder, storage


def render_stage(ctx: StageContext) -> dict[str, Any]:
    version = ctx.session.get(ClipVersion, uuid.UUID(str(ctx.run.config["clip_version_id"])))
    if version is None:
        raise ClipForgeError("clip version not found")
    clip = ctx.session.get(Clip, version.clip_id)
    assert clip is not None
    settings = get_settings()
    spec = RenderSpec.from_dict(version.render_spec)
    if spec.encoder == "nvenc" and resolve_encoder() != "nvenc":
        spec = replace(spec, encoder="x264")
    if spec.frame_mode == "auto":  # decide facecam layout vs fit+blur from this clip's own frames
        from clipforge.engine.render.facecam import auto_layout

        proxy = storage().local_path(get_artifact(ctx, ArtifactKind.PROXY).storage_key)
        aspect = (ctx.asset.width or 16) / (ctx.asset.height or 9)
        decided = auto_layout(proxy, spec.start_ms, spec.end_ms, aspect)
        spec = replace(
            spec,
            frame_mode=decided["frame_mode"],
            extra={**spec.extra, "layout": {**spec.extra.get("layout", {}), **decided["layout"]}},
        )
    mez = get_artifact(ctx, ArtifactKind.MEZZANINE)
    src = storage().local_path(mez.storage_key)
    src_fps = (ctx.asset.fps_num or 30) / (ctx.asset.fps_den or 1)
    if mez.meta.get("fps_num"):
        src_fps = mez.meta["fps_num"] / mez.meta["fps_den"]
    prefix = f"clips/{clip.id}/v{version.version}"
    with scratch_dir(settings.resolved_tmp_dir, prefix=f"render-{ctx.stage_run.id}-") as tmp:
        out, thumb = tmp / "clip.mp4", tmp / "thumb.jpg"
        used = render_clip(
            settings.ffmpeg_bin,
            src,
            out,
            spec,
            has_audio=ctx.asset.audio_codec is not None,
            on_progress=ctx.report_progress,
            src_fps=src_fps,
        )
        checks = verify_output(out, used, settings.ffprobe_bin)
        render_thumbnail(settings.ffmpeg_bin, src, thumb, used)
        storage().put_file(out, f"{prefix}/clip.mp4", move=True)
        storage().put_file(thumb, f"{prefix}/thumb.jpg", move=True)
    version.render_spec, version.render_spec_hash = used.as_dict(), used.hash()
    version.video_storage_key, version.thumbnail_storage_key = f"{prefix}/clip.mp4", f"{prefix}/thumb.jpg"
    version.status, version.checks = "ready", checks
    clip.current_version_id, clip.status = version.id, "rendered"
    return {"clip_id": str(clip.id), "version": version.version, "encoder": used.encoder, **checks}
