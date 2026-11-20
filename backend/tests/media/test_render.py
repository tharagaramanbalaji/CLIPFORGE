"""Phase 5 acceptance: frame-accurate cuts, A/V sync over 60 s, platform checks, reproducibility.
x264 here (the api container has no GPU); NVENC is exercised on the render worker."""

from __future__ import annotations

import hashlib
import itertools
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from clipforge.engine.render.runner import render_clip, render_thumbnail, verify_output
from clipforge.engine.render.spec import RenderSpec

pytestmark = pytest.mark.media
FPS = 30
CYCLE = 28  # brightness pattern repeats every 28 frames; steps of 8 survive compression


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=300)


def gray_means(video: Path) -> list[float]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            "scale=16:16,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return [sum(raw[i : i + 256]) / 256 for i in range(0, len(raw), 256)]


@pytest.fixture(scope="module")
def index_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Frame N has luma 16 + (N mod 28) * 8: the frame number is readable from brightness."""
    out = tmp_path_factory.mktemp("idx") / "index.mp4"
    ff(
        "-f",
        "lavfi",
        "-i",
        f"nullsrc=s=640x360:r={FPS},geq=lum='16+mod(N\\,{CYCLE})*8':cb=128:cr=128",
        "-f",
        "lavfi",
        "-i",
        "sine=f=300:r=48000",
        "-t",
        "40",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-qp",
        "0",
        "-g",
        "60",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(out),
    )
    return out


@pytest.fixture(scope="module")
def sync_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A white flash on frame 0 of every second and a 1-frame 1 kHz beep at the same instant."""
    out = tmp_path_factory.mktemp("sync") / "sync.mp4"
    ff(
        "-f",
        "lavfi",
        "-i",
        f"nullsrc=s=640x360:r={FPS},geq=lum='if(eq(mod(N\\,{FPS})\\,0)\\,235\\,16)':cb=128:cr=128",
        "-f",
        "lavfi",
        "-i",
        "aevalsrc='if(lt(mod(t\\,1)\\,1/30)\\,0.8*sin(2*PI*1000*t)\\,0)':s=48000",
        "-t",
        "90",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "10",
        "-g",
        "60",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out),
    )
    return out


def cycle_resets(means: list[float]) -> list[int]:
    """Positions where brightness falls from the top of the ramp back to the bottom (source frame % 28 == 0).
    Robust to the small brightness shifts that encoding and scaling introduce."""
    return [i for i in range(1, len(means)) if means[i] < means[i - 1] - 50]


@pytest.mark.parametrize("mode", ["crop", "blur_pad"])
@pytest.mark.parametrize(("start_ms", "end_ms"), [(10_017, 22_983), (3_333, 15_000)])
def test_frame_accurate_cuts(
    index_video: Path, tmp_path: Path, mode: str, start_ms: int, end_ms: int
) -> None:
    spec = RenderSpec(start_ms=start_ms, end_ms=end_ms, preset="shorts", frame_mode=mode, encoder="x264")  # type: ignore[arg-type]
    out = tmp_path / "clip.mp4"
    used = render_clip("ffmpeg", index_video, out, spec, has_audio=True)
    means = gray_means(out)
    assert len(means) == spec.frame_count
    resets = cycle_resets(means)
    assert resets and all(b - a == CYCLE for a, b in itertools.pairwise(resets)), resets  # no drop/dup
    first = (-resets[0]) % CYCLE  # output frame resets[0] is a source frame whose index % 28 == 0
    last = (first + len(means) - 1) % CYCLE
    exp_first = spec.start_frame % CYCLE
    exp_last = (spec.start_frame + spec.frame_count - 1) % CYCLE
    print(
        f"\n{mode} {start_ms}-{end_ms}: first frame {first} (expected {exp_first}), last {last} "
        f"(expected {exp_last}), {len(resets)} evenly spaced cycle resets"
    )
    assert first == exp_first and last == exp_last  # exact, so within the ±1 frame acceptance
    verify_output(out, used)


def onsets_audio(video: Path, threshold: float = 0.2) -> list[float]:
    import array

    raw = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    pcm = array.array("h", raw)
    peak = max(abs(x) for x in pcm) or 1
    out, last = [], -1.0
    for i in range(0, len(pcm), 48):  # peak of each 1 ms window (a single sample could hit a zero crossing)
        if max(abs(x) for x in pcm[i : i + 48]) / peak > threshold:
            t = i / 48000
            if t - last > 0.5:
                out.append(t)
            last = t
    return out


def test_av_sync_over_60s_within_one_frame(sync_video: Path, tmp_path: Path) -> None:
    spec = RenderSpec(start_ms=12_500, end_ms=72_500, preset="shorts", frame_mode="blur_pad", encoder="x264")
    out = tmp_path / "sync_clip.mp4"
    used = render_clip("ffmpeg", sync_video, out, spec, has_audio=True)
    flashes = [i / FPS for i, m in enumerate(gray_means(out)) if m > 120]
    beeps = onsets_audio(out)
    assert len(flashes) >= 58 and len(beeps) >= 58, (len(flashes), len(beeps))
    offsets = [min(beeps, key=lambda b: abs(b - f)) - f for f in flashes]
    worst = max(abs(o) for o in offsets)
    drift = offsets[-1] - offsets[0]
    print(
        f"\nA/V offset: worst {worst * 1000:.1f} ms, start {offsets[0] * 1000:.1f} ms, end {offsets[-1] * 1000:.1f} ms,"
        f" drift {drift * 1000:.1f} ms over {len(flashes)} flashes; loudness {used.loudness}"
    )
    assert worst <= 1 / FPS + 0.002  # within one frame (33.3 ms) everywhere


def test_presets_and_reproducible_from_spec(index_video: Path, tmp_path: Path) -> None:
    for preset, size in (("shorts", "1080x1920"), ("wide", "1920x1080"), ("square", "1080x1080")):
        spec = RenderSpec(start_ms=5_000, end_ms=11_000, preset=preset, frame_mode="crop", encoder="x264")
        out = tmp_path / f"{preset}.mp4"
        used = render_clip("ffmpeg", index_video, out, spec, has_audio=True)
        checks = verify_output(out, used)
        assert checks["size"] == size and checks["frames"] == 180
    # Re-render from the stored spec (dict round-trip, measured loudness included): bit-identical output
    spec = RenderSpec(start_ms=5_000, end_ms=11_000, preset="shorts", encoder="x264")
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    used = render_clip("ffmpeg", index_video, a, spec, has_audio=True)
    render_clip("ffmpeg", index_video, b, RenderSpec.from_dict(used.as_dict()), has_audio=True)
    assert hashlib.sha256(a.read_bytes()).hexdigest() == hashlib.sha256(b.read_bytes()).hexdigest()
    thumb = tmp_path / "t.jpg"
    render_thumbnail("ffmpeg", index_video, thumb, used)
    assert thumb.read_bytes()[:2] == bytes([0xFF, 0xD8])


def test_too_long_for_platform_fails_checks(index_video: Path, tmp_path: Path) -> None:
    from clipforge.errors import ValidationFailed

    spec = RenderSpec(start_ms=0, end_ms=6_000, preset="shorts", encoder="x264")
    out = tmp_path / "x.mp4"
    used = render_clip("ffmpeg", index_video, out, spec, has_audio=True)
    with pytest.raises(ValidationFailed, match="frames"):
        verify_output(out, replace(used, end_ms=9_000))  # claims 9 s, file has 6 s
