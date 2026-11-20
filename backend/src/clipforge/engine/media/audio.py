"""Audio extraction for ASR: 16 kHz mono PCM WAV from the mezzanine (same timeline)."""

from __future__ import annotations

from pathlib import Path

from clipforge.engine.media.ffmpeg import ProgressFn, run_ffmpeg
from clipforge.engine.media.transcode import encode_timeout_s

SAMPLE_RATE = 16_000


def build_audio_args(src: Path, dst: Path) -> list[str]:
    # -map 0:a:0: first audio track only; aresample async keeps audio aligned to the video timeline
    return [
        "-y",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-af",
        "aresample=async=1:first_pts=0",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]


def extract_audio(
    ffmpeg_bin: str, src: Path, dst: Path, duration_ms: int, on_progress: ProgressFn | None = None
) -> None:
    run_ffmpeg(
        ffmpeg_bin,
        build_audio_args(src, dst),
        duration_ms=duration_ms,
        timeout_s=encode_timeout_s(duration_ms, factor=0.5),
        on_progress=on_progress,
    )
