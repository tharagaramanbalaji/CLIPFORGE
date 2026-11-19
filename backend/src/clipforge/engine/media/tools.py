"""FFmpeg capability checks (version, NVENC, libass)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from clipforge.engine.common.process import run_command

_VERSION_RE = re.compile(r"ffmpeg version (\S+)")
_REQUIRED_FILTERS = ("ass", "subtitles", "crop", "scale", "loudnorm", "ebur128")


@dataclass(frozen=True, slots=True)
class FfmpegCapabilities:
    version: str
    has_nvenc_h264: bool
    has_nvenc_hevc: bool
    has_libx264: bool
    has_libass: bool
    missing_filters: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.has_libx264 and self.has_libass and not self.missing_filters


def _names(listing: str) -> set[str]:
    """Second column of `ffmpeg -encoders` / `-filters` output."""
    names: set[str] = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(" ="):
            names.add(parts[1])
    return names


def parse_capabilities(version_out: str, encoders_out: str, filters_out: str) -> FfmpegCapabilities:
    m = _VERSION_RE.search(version_out)
    encoders = _names(encoders_out)
    filters = _names(filters_out)
    return FfmpegCapabilities(
        version=m.group(1) if m else "unknown",
        has_nvenc_h264="h264_nvenc" in encoders,
        has_nvenc_hevc="hevc_nvenc" in encoders,
        has_libx264="libx264" in encoders,
        has_libass="ass" in filters and "--enable-libass" in version_out,
        missing_filters=tuple(f for f in _REQUIRED_FILTERS if f not in filters),
    )


def ffmpeg_capabilities(ffmpeg_bin: str = "ffmpeg") -> FfmpegCapabilities:
    base = [ffmpeg_bin, "-hide_banner"]
    version = run_command([ffmpeg_bin, "-version"], timeout_s=15).stdout
    encoders = run_command([*base, "-encoders"], timeout_s=15).stdout
    filters = run_command([*base, "-filters"], timeout_s=15).stdout
    return parse_capabilities(version, encoders, filters)
