"""Audio signals as fixed-hop series (values[i] covers [i*hop_ms, (i+1)*hop_ms)).

- loudness: EBU R128 momentary loudness (LUFS, 400 ms window) from FFmpeg's ebur128 filter, 100 ms hop
- energy:   RMS level in dBFS of the 16 kHz mono WAV, 100 ms hop
- speech_rate: words per second over a 5 s window from the transcript, 1 s hop
"""

from __future__ import annotations

import math
import re
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from clipforge.engine.common.process import run_command

_PTS = re.compile(r"pts_time:\s*(-?[\d.]+)")
_MOMENTARY = re.compile(r"lavfi\.r128\.M=(-?[\d.]+|-?inf|nan)")
SILENCE_DB = -90.0


@dataclass(frozen=True, slots=True)
class Series:
    kind: str
    hop_ms: int
    values: list[float]
    unit: str

    def stats(self) -> dict[str, float | int]:
        vals = [v for v in self.values if math.isfinite(v)]
        if not vals:
            return {"count": len(self.values)}
        srt = sorted(vals)
        return {
            "count": len(self.values),
            "min": round(srt[0], 3),
            "max": round(srt[-1], 3),
            "mean": round(sum(vals) / len(vals), 3),
            "p90": round(srt[int(0.9 * (len(srt) - 1))], 3),
        }


def parse_ebur128(metadata: str, hop_ms: int = 100) -> list[float]:
    """Parse `ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=-` output (pts_time, then M=value).

    Each frame's momentary loudness is placed at its timestamp bucket; missing buckets repeat the previous
    value, and silence (-inf / below -90) is clamped to SILENCE_DB.
    """
    values: list[float] = []
    pts: float | None = None
    for line in metadata.splitlines():
        if (m := _PTS.search(line)) is not None:
            pts = float(m.group(1))
            continue
        if pts is None or (m := _MOMENTARY.search(line)) is None:
            continue
        raw = m.group(1)
        val = SILENCE_DB if "inf" in raw or raw == "nan" else max(SILENCE_DB, float(raw))
        idx = round(pts * 1000 / hop_ms)
        pts = None
        if idx < 0:
            continue
        while len(values) < idx:
            values.append(values[-1] if values else SILENCE_DB)
        if len(values) == idx:
            values.append(val)
    return values


def loudness_series(ffmpeg_bin: str, wav: Path, duration_ms: int) -> Series:
    res = run_command(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            wav,
            "-af",
            "ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file=-",
            "-f",
            "null",
            "-",
        ],
        timeout_s=max(120, duration_ms / 1000 * 0.2),
    )
    return Series("loudness", 100, parse_ebur128(res.stdout), "LUFS")


def energy_series(wav: Path, hop_ms: int = 100) -> Series:
    import numpy as np

    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    hop = sr * hop_ms // 1000
    n = len(pcm) // hop
    if n == 0:
        return Series("energy", hop_ms, [], "dBFS")
    frames = pcm[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 10 ** (SILENCE_DB / 20)))
    return Series("energy", hop_ms, [round(float(x), 2) for x in db], "dBFS")


def speech_rate_series(
    word_starts_ms: Sequence[int], duration_ms: int, window_ms: int = 5000, hop_ms: int = 1000
) -> Series:
    import bisect

    starts = sorted(word_starts_ms)
    n = max(0, math.ceil(duration_ms / hop_ms))
    values = []
    for i in range(n):
        center = i * hop_ms + hop_ms // 2
        lo, hi = center - window_ms // 2, center + window_ms // 2
        count = bisect.bisect_left(starts, hi) - bisect.bisect_left(starts, lo)
        values.append(round(count / (window_ms / 1000), 3))
    return Series("speech_rate", hop_ms, values, "words/s")
