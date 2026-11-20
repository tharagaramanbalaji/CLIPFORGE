"""Synthetic video with hard cuts at known frames: visually distinct lavfi sources concatenated at 30 fps."""

from __future__ import annotations

import subprocess
from pathlib import Path

FPS = 30
# (lavfi source, frames). Cuts happen at the cumulative frame counts.
SHOTS = [
    ("testsrc2=size=960x540:rate=30", 45),
    ("smptebars=size=960x540:rate=30", 60),
    ("mandelbrot=size=960x540:rate=30", 38),
    ("color=c=0x2050c0:size=960x540:rate=30,noise=alls=20:allf=t", 75),
    ("cellauto=size=960x540:rate=30:rule=110", 52),
    ("rgbtestsrc=size=960x540:rate=30", 40),
]


def cut_frames() -> list[int]:
    frames, acc = [], 0
    for _, n in SHOTS[:-1]:
        acc += n
        frames.append(acc)
    return frames


def make_cut_video(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "cuts.mp4"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for src, n in SHOTS:
        args += ["-f", "lavfi", "-i", f"{src},format=yuv420p,trim=end_frame={n},setpts=PTS-STARTPTS"]
    concat = "".join(f"[{i}:v]" for i in range(len(SHOTS))) + f"concat=n={len(SHOTS)}:v=1:a=0[v]"
    args += [
        "-filter_complex",
        concat,
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-g",
        str(FPS),
        "-r",
        str(FPS),
        str(path),
    ]
    subprocess.run(args, check=True, timeout=120)
    return path
