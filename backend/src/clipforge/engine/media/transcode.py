"""Mezzanine (CFR edit master) and proxy (small preview / analysis copy) encodes.

All later timing (ASR words, scenes, renders) refers to the mezzanine timeline. When the source is
already constant-frame-rate H.264/HEVC in a seekable container, the original *is* the mezzanine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from clipforge.engine.media.ffmpeg import ProgressFn, run_ffmpeg
from clipforge.engine.media.probe import ProbeResult

Encoder = Literal["nvenc", "x264"]

PASSTHROUGH_CONTAINERS = ("mov", "mp4", "matroska")
PASSTHROUGH_VIDEO_CODECS = ("h264", "hevc")
_STANDARD_RATES = (
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
)


@dataclass(frozen=True, slots=True)
class MezzanineConfig:
    encoder: Encoder
    max_fps: int = 60
    x264_crf: int = 18
    nvenc_cq: int = 19
    audio_bitrate: str = "192k"
    version: int = 1  # bump when the recipe changes (invalidates idempotency keys)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    encoder: Encoder
    height: int = 540
    max_fps: int = 30
    x264_crf: int = 28
    nvenc_cq: int = 30
    audio_bitrate: str = "96k"
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def needs_mezzanine(probe: ProbeResult, is_vfr: bool) -> tuple[bool, str]:
    if is_vfr:
        return True, "variable frame rate"
    if probe.video is None or probe.video.codec not in PASSTHROUGH_VIDEO_CODECS:
        return True, f"video codec {probe.video.codec if probe.video else 'none'}"
    if not set(probe.format_name.split(",")) & set(PASSTHROUGH_CONTAINERS):
        return True, f"container {probe.format_name}"
    if probe.video.pix_fmt not in (None, "yuv420p", "yuvj420p"):
        return True, f"pixel format {probe.video.pix_fmt}"
    return False, "already CFR H.264/HEVC"


def target_fps(probe: ProbeResult, max_fps: int) -> Fraction:
    """Snap the measured rate to a standard broadcast rate when within 2%, else round; cap at max_fps."""
    fps = probe.fps or Fraction(30)
    for std in _STANDARD_RATES:
        if abs(float(fps) - float(std)) / float(std) < 0.02:
            fps = std
            break
    else:
        fps = Fraction(max(1, round(float(fps))))
    return min(fps, Fraction(max_fps))


def _video_codec_args(encoder: Encoder, quality: int, gop: int, preset_x264: str) -> list[str]:
    if encoder == "nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "vbr",
            "-cq",
            str(quality),
            "-b:v",
            "0",
            "-g",
            str(gop),
            "-bf",
            "2",
        ]
    return ["-c:v", "libx264", "-preset", preset_x264, "-crf", str(quality), "-g", str(gop)]


def build_mezzanine_args(src: Path, dst: Path, probe: ProbeResult, cfg: MezzanineConfig) -> list[str]:
    fps = target_fps(probe, cfg.max_fps)
    gop = max(1, round(float(fps) * 2))  # keyframe every ~2 s: fast, accurate seeks for clip renders
    quality = cfg.nvenc_cq if cfg.encoder == "nvenc" else cfg.x264_crf
    args: list[str] = ["-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    args += ["-vf", f"fps={fps.numerator}/{fps.denominator},format=yuv420p", "-fps_mode", "cfr"]
    args += _video_codec_args(cfg.encoder, quality, gop, "veryfast")
    args += [
        "-c:a",
        "aac",
        "-b:a",
        cfg.audio_bitrate,
        "-ar",
        "48000",
        "-af",
        "aresample=async=1:first_pts=0",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    return args


def build_proxy_args(src: Path, dst: Path, probe: ProbeResult, cfg: ProxyConfig) -> list[str]:
    fps = target_fps(probe, cfg.max_fps)
    height = min(cfg.height, probe.video.height if probe.video else cfg.height)
    height -= height % 2
    gop = max(1, round(float(fps)))
    quality = cfg.nvenc_cq if cfg.encoder == "nvenc" else cfg.x264_crf
    vf = f"fps={fps.numerator}/{fps.denominator},scale=-2:{height},format=yuv420p"
    args: list[str] = [
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-vf",
        vf,
        "-fps_mode",
        "cfr",
    ]
    args += _video_codec_args(cfg.encoder, quality, gop, "veryfast")
    args += ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ac", "2", "-movflags", "+faststart", str(dst)]
    return args


def encode_timeout_s(duration_ms: int, factor: float = 3.0, minimum: float = 600) -> float:
    return max(minimum, duration_ms / 1000 * factor)


def make_mezzanine(
    ffmpeg_bin: str,
    src: Path,
    dst: Path,
    probe: ProbeResult,
    cfg: MezzanineConfig,
    on_progress: ProgressFn | None = None,
) -> None:
    run_ffmpeg(
        ffmpeg_bin,
        build_mezzanine_args(src, dst, probe, cfg),
        duration_ms=probe.duration_ms,
        timeout_s=encode_timeout_s(probe.duration_ms),
        on_progress=on_progress,
    )


def make_proxy(
    ffmpeg_bin: str,
    src: Path,
    dst: Path,
    probe: ProbeResult,
    cfg: ProxyConfig,
    on_progress: ProgressFn | None = None,
) -> None:
    run_ffmpeg(
        ffmpeg_bin,
        build_proxy_args(src, dst, probe, cfg),
        duration_ms=probe.duration_ms,
        timeout_s=encode_timeout_s(probe.duration_ms),
        on_progress=on_progress,
    )
