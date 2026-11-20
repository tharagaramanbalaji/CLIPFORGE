"""RenderSpec: everything needed to reproduce a clip render exactly (stored on every clip version)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any, Literal

from clipforge.engine.common.hashing import stable_hash

Aspect = Literal["9:16", "16:9", "1:1"]
FrameMode = Literal[
    "crop", "blur_pad", "facecam_split", "two_stack", "auto", "auto_switch"
]  # see engine/render/layouts.py
Encoder = Literal["nvenc", "x264"]


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    aspect: Aspect
    width: int
    height: int
    max_fps: int = 60
    max_duration_s: float = 180.0
    loudness_lufs: float = -14.0
    true_peak_db: float = -1.0
    video_bitrate_cap: str = "12M"  # NVENC/x264 VBV cap keeps uploads reasonable
    audio_bitrate: str = "192k"


# Platform specs (2026): all accept H.264 + AAC MP4 with faststart, 1080x1920 for vertical.
PRESETS: dict[str, Preset] = {
    "shorts": Preset("shorts", "9:16", 1080, 1920, max_duration_s=180.0),
    "reels": Preset("reels", "9:16", 1080, 1920, max_duration_s=180.0),
    "tiktok": Preset("tiktok", "9:16", 1080, 1920, max_duration_s=600.0),
    "wide": Preset("wide", "16:9", 1920, 1080, max_duration_s=3600.0),
    "square": Preset("square", "1:1", 1080, 1080, max_duration_s=600.0),
}


@dataclass(frozen=True, slots=True)
class RenderSpec:
    start_ms: int
    end_ms: int
    preset: str = "shorts"
    frame_mode: FrameMode = "blur_pad"
    fps_num: int = 30
    fps_den: int = 1
    encoder: Encoder = "nvenc"
    quality: int = 20  # x264 CRF / NVENC CQ
    loudness: dict[str, float] | None = (
        None  # measured pass-1 values (input_i, input_tp, input_lra, input_thresh)
    )
    version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)  # Phase 6/7 add subtitles, crop plans

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_num, self.fps_den)

    @property
    def preset_obj(self) -> Preset:
        return PRESETS[self.preset]

    # Frame-exact bounds on the (CFR) mezzanine timeline
    @property
    def start_frame(self) -> int:
        return round(self.start_ms * self.fps / 1000)

    @property
    def frame_count(self) -> int:
        return max(1, round(self.end_ms * self.fps / 1000) - self.start_frame)

    @property
    def start_s(self) -> float:
        return float(self.start_frame / self.fps)

    @property
    def duration_s(self) -> float:
        return float(self.frame_count / self.fps)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RenderSpec:
        return cls(**d)

    def hash(self) -> str:
        return stable_hash(self.as_dict())


def output_fps(src_fps: Fraction, preset: Preset) -> Fraction:
    """Keep the source rate (30/60, 29.97...) unless it exceeds what the preset allows."""
    return min(src_fps, Fraction(preset.max_fps))
