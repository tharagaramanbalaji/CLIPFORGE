"""Run clip renders: loudness pass 1, encode (NVENC with x264 fallback), thumbnail, output verification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from clipforge.engine.common.process import run_command
from clipforge.engine.media.ffmpeg import ProgressFn, run_ffmpeg
from clipforge.engine.media.probe import probe_media
from clipforge.engine.render.filtergraph import (
    build_loudness_measure_args,
    build_render_args,
    build_thumbnail_args,
)
from clipforge.engine.render.spec import PRESETS, RenderSpec
from clipforge.errors import ExternalToolError, ValidationFailed
from clipforge.log import get_logger

log = get_logger(__name__)
_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def measure_loudness(ffmpeg_bin: str, src: Path, spec: RenderSpec) -> dict[str, float] | None:
    """loudnorm pass 1 on exactly the clip's span; None if the segment is silent / unmeasurable."""
    res = run_command(
        [ffmpeg_bin, *build_loudness_measure_args(src, spec, spec.preset_obj)],
        timeout_s=max(60, spec.duration_s * 2),
        check=False,
    )
    m = _JSON_BLOCK.search(res.stderr)
    if not m:
        return None
    data = json.loads(m.group(0))
    try:
        values = {k: float(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh")}
    except (KeyError, ValueError):
        return None
    return None if values["input_i"] < -70 else values  # -inf/-70 = silence: skip normalization


@dataclass
class RenderResult:
    spec: RenderSpec  # final spec actually used (encoder fallback, measured loudness)
    checks: dict[str, Any]


def render_clip(
    ffmpeg_bin: str,
    src: Path,
    dst: Path,
    spec: RenderSpec,
    has_audio: bool,
    on_progress: ProgressFn | None = None,
    src_fps: float | None = None,
) -> RenderSpec:
    """Returns the spec actually used (loudness filled in; encoder may fall back to x264)."""
    if has_audio and spec.loudness is None:
        spec = replace(spec, loudness=measure_loudness(ffmpeg_bin, src, spec))
    duration_ms = int(spec.duration_s * 1000)
    ass = None
    from clipforge.engine.subtitles.render_ass import ass_for

    ass_text = ass_for(spec)
    if ass_text is not None:
        ass = dst.with_suffix(".ass")
        ass.write_text(ass_text, encoding="utf-8")
    from clipforge.engine.subtitles.styles import isolated_font_env

    env = isolated_font_env() if ass is not None else None
    try:
        run_ffmpeg(
            ffmpeg_bin,
            build_render_args(src, dst, spec, spec.preset_obj, has_audio, src_fps, ass),
            duration_ms=duration_ms,
            timeout_s=max(120, spec.duration_s * 10),
            on_progress=on_progress,
            env=env,
        )
    except ExternalToolError as exc:
        if spec.encoder != "nvenc":
            raise
        log.warning("nvenc_render_failed_falling_back", error=exc.message)
        spec = replace(spec, encoder="x264")
        run_ffmpeg(
            ffmpeg_bin,
            build_render_args(src, dst, spec, spec.preset_obj, has_audio, src_fps, ass),
            duration_ms=duration_ms,
            timeout_s=max(300, spec.duration_s * 30),
            on_progress=on_progress,
            env=env,
        )
    return spec


def render_thumbnail(
    ffmpeg_bin: str, src: Path, dst: Path, spec: RenderSpec, at_ms: int | None = None
) -> None:
    at = (at_ms / 1000) if at_ms is not None else spec.start_s + spec.duration_s / 3
    run_command(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            *build_thumbnail_args(src, dst, spec, spec.preset_obj, at),
        ],
        timeout_s=60,
    )


def verify_output(path: Path, spec: RenderSpec, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    """Platform checks: H.264 high/yuv420p at the preset size and fps, AAC 48 kHz stereo, exact duration,
    within the platform's max length, moov atom first (faststart)."""
    preset = spec.preset_obj
    p = probe_media(path, ffprobe_bin)
    v, a = p.video, p.audio
    expected_s = spec.duration_s
    problems: list[str] = []
    if v is None or v.codec != "h264" or v.pix_fmt != "yuv420p":
        problems.append(
            f"video must be h264/yuv420p, got {v.codec if v else None}/{v.pix_fmt if v else None}"
        )
    elif (v.width, v.height) != (preset.width, preset.height):
        problems.append(f"size {v.width}x{v.height} != {preset.width}x{preset.height}")
    if p.fps is None or abs(float(p.fps) - float(spec.fps)) > 0.01:
        problems.append(f"fps {p.fps} != {spec.fps}")
    if a is None or a.codec != "aac" or a.sample_rate != 48000 or a.channels != 2:
        problems.append("audio must be AAC 48 kHz stereo")
    if abs(p.duration_ms / 1000 - expected_s) > 1.5 / float(spec.fps) + 0.05:
        problems.append(f"duration {p.duration_ms / 1000:.3f}s != {expected_s:.3f}s")
    if expected_s > preset.max_duration_s:
        problems.append(f"{expected_s:.0f}s exceeds {preset.name} max {preset.max_duration_s:.0f}s")
    with path.open("rb") as f:
        head = f.read(64 * 1024)
    if head.find(b"moov") == -1 or (head.find(b"mdat") != -1 and head.find(b"moov") > head.find(b"mdat")):
        problems.append("moov atom not at start (faststart missing)")
    nb = run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            path,
        ],
        timeout_s=60,
    ).stdout.strip()
    frames = int(nb) if nb.isdigit() else -1
    if frames != spec.frame_count:
        problems.append(f"{frames} frames != {spec.frame_count}")
    checks = {
        "ok": not problems,
        "problems": problems,
        "frames": frames,
        "duration_ms": p.duration_ms,
        "size": f"{v.width}x{v.height}" if v else None,
        "preset": preset.name,
        "bytes": path.stat().st_size,
    }
    if problems:
        raise ValidationFailed("Rendered clip failed platform checks: " + "; ".join(problems), details=checks)
    return checks


def default_spec(
    start_ms: int, end_ms: int, src_fps: Any, preset: str, frame_mode: str, encoder: str
) -> RenderSpec:
    from clipforge.engine.render.spec import output_fps

    fps = output_fps(src_fps, PRESETS[preset])
    return RenderSpec(
        start_ms=start_ms,
        end_ms=end_ms,
        preset=preset,
        frame_mode=frame_mode,  # type: ignore[arg-type]
        fps_num=fps.numerator,
        fps_den=fps.denominator,
        encoder=encoder,  # type: ignore[arg-type]
    )
