"""ffprobe wrapper: container/stream metadata plus a timestamp-based VFR check."""

from __future__ import annotations

import itertools
import json
import statistics
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from clipforge.engine.common.process import run_command
from clipforge.errors import ExternalToolError, ValidationFailed

IMAGE_CODECS = {"png", "mjpeg", "bmp", "gif", "webp", "tiff", "jpeg2000"}


@dataclass(frozen=True, slots=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    r_frame_rate: str  # ffprobe "num/den" (lowest common multiple rate)
    avg_frame_rate: str  # ffprobe "num/den" (frames / duration)
    pix_fmt: str | None
    is_attached_pic: bool


@dataclass(frozen=True, slots=True)
class AudioStream:
    index: int
    codec: str
    channels: int
    sample_rate: int


@dataclass(frozen=True, slots=True)
class ProbeResult:
    format_name: str
    duration_ms: int
    size_bytes: int
    bit_rate: int | None
    video: VideoStream | None
    audio: AudioStream | None
    video_stream_count: int
    audio_stream_count: int
    raw: dict[str, Any] = field(repr=False)

    @property
    def fps(self) -> Fraction | None:
        if self.video is None:
            return None
        for rate in (self.video.avg_frame_rate, self.video.r_frame_rate):
            frac = parse_rate(rate)
            if frac is not None:
                return frac
        return None

    def summary(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("raw")
        return out


@dataclass(frozen=True, slots=True)
class VfrReport:
    is_vfr: bool
    method: str  # "timestamps" | "metadata"
    sampled_frames: int
    median_frame_ms: float | None
    irregular_ratio: float | None
    metadata_rates_differ: bool


def parse_rate(rate: str | None) -> Fraction | None:
    if not rate or rate in ("0/0", "0"):
        return None
    try:
        frac = Fraction(rate)
    except (ValueError, ZeroDivisionError):
        return None
    return frac if frac > 0 else None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_probe(data: dict[str, Any]) -> ProbeResult:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    # Prefer a real video stream over cover art (attached_pic)
    videos.sort(key=lambda s: (s.get("disposition") or {}).get("attached_pic", 0))

    video = None
    if videos:
        v = videos[0]
        video = VideoStream(
            index=_int(v.get("index")),
            codec=str(v.get("codec_name", "unknown")),
            width=_int(v.get("width")),
            height=_int(v.get("height")),
            r_frame_rate=str(v.get("r_frame_rate", "0/0")),
            avg_frame_rate=str(v.get("avg_frame_rate", "0/0")),
            pix_fmt=v.get("pix_fmt"),
            is_attached_pic=bool((v.get("disposition") or {}).get("attached_pic", 0)),
        )
    audio = None
    if audios:
        a = audios[0]
        audio = AudioStream(
            index=_int(a.get("index")),
            codec=str(a.get("codec_name", "unknown")),
            channels=_int(a.get("channels")),
            sample_rate=_int(a.get("sample_rate")),
        )

    duration_s = float(fmt.get("duration") or 0) or max(
        (float(s.get("duration") or 0) for s in streams), default=0.0
    )
    bit_rate = _int(fmt.get("bit_rate"), default=-1)
    return ProbeResult(
        format_name=str(fmt.get("format_name", "unknown")),
        duration_ms=round(duration_s * 1000),
        size_bytes=_int(fmt.get("size")),
        bit_rate=bit_rate if bit_rate >= 0 else None,
        video=video,
        audio=audio,
        video_stream_count=len(videos),
        audio_stream_count=len(audios),
        raw=data,
    )


def _first_error_line(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else "unknown error"


def probe_media(path: Path, ffprobe_bin: str = "ffprobe", timeout_s: float = 120) -> ProbeResult:
    try:
        res = run_command(
            [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            timeout_s=timeout_s,
        )
    except ExternalToolError as exc:
        reason = _first_error_line(str(exc.details.get("stderr", ""))).replace(str(path), path.name)
        raise ValidationFailed(
            f"The file could not be read as a video ({reason}). It may be corrupt or incomplete.",
            details={"reason": reason},
        ) from exc
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationFailed("The file could not be read as a video (unreadable probe output).") from exc
    if not data.get("format"):
        raise ValidationFailed("The file could not be read as a video (no container detected).")
    return parse_probe(data)


def classify_frame_timing(
    pts_ms: list[float], *, rates_differ: bool = False, tolerance_ratio: float = 0.15, min_abs_ms: float = 2.0
) -> VfrReport:
    """Decide VFR from presentation timestamps.

    CFR content has (nearly) identical frame intervals; containers with millisecond timebases (MKV, FLV)
    jitter by ±1 ms, which the absolute tolerance absorbs. More than 2% irregular intervals => VFR.
    """
    pts = sorted(pts_ms)
    deltas = [b - a for a, b in itertools.pairwise(pts) if b > a]
    if len(deltas) < 10:
        return VfrReport(rates_differ, "metadata", len(pts), None, None, rates_differ)
    median = statistics.median(deltas)
    tol = max(min_abs_ms, tolerance_ratio * median)
    irregular = sum(1 for d in deltas if abs(d - median) > tol) / len(deltas)
    return VfrReport(
        irregular > 0.02, "timestamps", len(pts), round(median, 3), round(irregular, 4), rates_differ
    )


def detect_vfr(
    path: Path, probe: ProbeResult, ffprobe_bin: str = "ffprobe", sample_s: int = 60, timeout_s: float = 120
) -> VfrReport:
    if probe.video is None:
        return VfrReport(False, "metadata", 0, None, None, False)
    r, avg = parse_rate(probe.video.r_frame_rate), parse_rate(probe.video.avg_frame_rate)
    rates_differ = r is not None and avg is not None and abs(float(r) - float(avg)) / float(r) > 0.01
    res = run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            f"{probe.video.index}",
            "-read_intervals",
            f"%+{sample_s}",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            path,
        ],
        timeout_s=timeout_s,
        check=False,
    )
    pts: list[float] = []
    for line in res.stdout.splitlines():
        value = line.strip().rstrip(",")
        if value and value != "N/A":
            try:
                pts.append(float(value) * 1000)
            except ValueError:
                continue
    return classify_frame_timing(pts, rates_differ=rates_differ)
