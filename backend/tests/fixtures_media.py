"""Generates small test videos with FFmpeg's lavfi sources (no media files committed)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_V = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30"]
_A = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
_X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


def make_fixtures(out: Path, seconds: int = 4) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    t = ["-t", str(seconds)]
    files = {
        "mp4": out / "cfr.mp4",
        "mov": out / "cfr.mov",
        "mkv": out / "cfr.mkv",
        "vfr": out / "vfr.mp4",
        "audio_only": out / "audio_only.mp4",
        "corrupt": out / "corrupt.mp4",
        "truncated": out / "truncated.mp4",
    }
    _ffmpeg(*_V, *_A, *t, *_X264, "-c:a", "aac", "-movflags", "+faststart", str(files["mp4"]))
    _ffmpeg(*_V, *_A, *t, *_X264, "-c:a", "aac", str(files["mov"]))
    _ffmpeg(*_V, *_A, *t, *_X264, "-c:a", "aac", str(files["mkv"]))
    # Every third frame interval doubles -> genuinely variable frame rate
    _ffmpeg(
        *_V,
        *_A,
        *t,
        "-vf",
        "setpts='(N+floor(N/3))/(30*TB)'",
        "-fps_mode",
        "passthrough",
        *_X264,
        "-c:a",
        "aac",
        str(files["vfr"]),
    )
    _ffmpeg(*_A, *t, "-c:a", "aac", str(files["audio_only"]))
    files["corrupt"].write_bytes(os.urandom(256 * 1024))
    # moov atom at the end (no faststart) + cut in half => unreadable container
    full = out / "full_for_truncation.mp4"
    _ffmpeg(*_V, *_A, *t, *_X264, "-c:a", "aac", str(full))
    data = full.read_bytes()
    files["truncated"].write_bytes(data[: len(data) // 2])
    return files
