"""Ingest validation: turn a ProbeResult into clear, user-readable rejection reasons."""

from __future__ import annotations

from dataclasses import dataclass

from clipforge.engine.media.probe import IMAGE_CODECS, ProbeResult
from clipforge.errors import ValidationFailed

# ffprobe format_name values are comma-separated families, e.g. "mov,mp4,m4a,3gp,3g2,mj2"
SUPPORTED_CONTAINERS = ("mov", "mp4", "matroska", "webm", "flv", "mpegts", "avi")
SUPPORTED_VIDEO_CODECS = ("h264", "hevc", "vp9", "vp8", "av1", "mpeg4", "mpeg2video", "prores", "dnxhd")


@dataclass(frozen=True, slots=True)
class IngestLimits:
    max_duration_s: float = 12 * 3600
    min_duration_s: float = 1.0
    max_width: int = 7680
    max_height: int = 4320
    require_audio: bool = True


def validation_errors(probe: ProbeResult, limits: IngestLimits) -> list[str]:
    errors: list[str] = []
    families = set(probe.format_name.split(","))
    if not families & set(SUPPORTED_CONTAINERS):
        errors.append(
            f"Unsupported file type '{probe.format_name}'. Upload MP4, MOV, MKV, WebM, FLV, TS or AVI video."
        )
    v = probe.video
    if v is None or v.is_attached_pic or (v.codec in IMAGE_CODECS and probe.duration_ms < 1000):
        errors.append("No video track found. The file looks like audio-only or a still image.")
    else:
        if v.codec not in SUPPORTED_VIDEO_CODECS:
            errors.append(
                f"Unsupported video codec '{v.codec}'. Supported: {', '.join(SUPPORTED_VIDEO_CODECS)}."
            )
        if v.width <= 0 or v.height <= 0:
            errors.append("The video track has no valid frame size.")
        elif v.width > limits.max_width or v.height > limits.max_height:
            errors.append(
                f"Resolution {v.width}x{v.height} is above the {limits.max_width}x{limits.max_height} limit."
            )
        if probe.fps is None:
            errors.append("The video track has no readable frame rate.")
    if limits.require_audio and probe.audio is None:
        errors.append("No audio track found. ClipForge needs the audio to transcribe and find highlights.")
    duration_s = probe.duration_ms / 1000
    if duration_s < limits.min_duration_s:
        errors.append(f"The video is too short ({duration_s:.1f}s).")
    elif duration_s > limits.max_duration_s:
        errors.append(
            f"The video is {duration_s / 3600:.1f} h long; the limit is {limits.max_duration_s / 3600:.0f} h."
        )
    return errors


def validate_probe(probe: ProbeResult, limits: IngestLimits) -> None:
    errors = validation_errors(probe, limits)
    if errors:
        raise ValidationFailed(" ".join(errors), details={"errors": errors})
