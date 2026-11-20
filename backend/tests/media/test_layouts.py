"""Phase 7 (slim) acceptance: layout templates render the right regions into the right bands, and captions
move off the picture."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.engine.render.runner import render_clip
from clipforge.engine.render.spec import RenderSpec

pytestmark = pytest.mark.media


@pytest.fixture(scope="module")
def regions_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """1280x720: left half yellow, right half magenta, a green box in the center, a red 'facecam' at the
    top-right corner (x 0.73..1, y 0..0.32)."""
    out = tmp_path_factory.mktemp("lay") / "regions.mp4"
    vf = (
        "drawbox=x=0:y=0:w=640:h=720:color=yellow:t=fill,drawbox=x=640:y=0:w=640:h=720:color=magenta:t=fill,"
        "drawbox=x=440:y=260:w=400:h=200:color=green:t=fill,drawbox=x=935:y=0:w=345:h=230:color=red:t=fill"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "6",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(out),
        ],
        check=True,
    )
    return out


def mean_rgb(video: Path, y0: float, y1: float, x0: float = 0.0, x1: float = 1.0) -> tuple[int, int, int]:
    """Average colour of a frame region (fractions of the output frame), from the middle of the clip."""
    w, h = 90, 160
    raw = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "1",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={w}:{h},format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    px = [
        raw[(y * w + x) * 3 : (y * w + x) * 3 + 3]
        for y in range(int(y0 * h), int(y1 * h))
        for x in range(int(x0 * w), int(x1 * w))
    ]
    return tuple(sum(p[i] for p in px) // len(px) for i in range(3))  # type: ignore[return-value]


def is_color(rgb: tuple[int, int, int], name: str) -> bool:
    r, g, b = rgb
    return {
        "red": r > 180 and g < 80 and b < 80,
        "green": g > 90 and r < 60 and b < 60,
        "yellow": r > 180 and g > 180 and b < 80,
        "magenta": r > 180 and b > 180 and g < 80,
    }[name]


def render(src: Path, out: Path, mode: str, layout: dict[str, object], **sub: object) -> None:
    extra: dict[str, object] = {"layout": layout}
    if sub:
        extra["subtitles"] = sub
    render_clip(
        "ffmpeg",
        src,
        out,
        RenderSpec(
            start_ms=0,
            end_ms=3000,
            preset="shorts",
            frame_mode=mode,  # type: ignore[arg-type]
            encoder="x264",
            extra=extra,
        ),
        has_audio=True,
    )


def test_facecam_split(regions_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "fc.mp4"
    render(regions_video, out, "facecam_split", {"facecam": [0.73, 0.0, 0.27, 0.32], "top_ratio": 0.35})
    top, bottom_center = mean_rgb(out, 0.02, 0.33), mean_rgb(out, 0.6, 0.75, 0.3, 0.7)
    print(f"\nfacecam_split: top band {top}, gameplay center {bottom_center}")
    assert is_color(top, "red")  # the whole top band is the facecam
    assert is_color(bottom_center, "green")  # gameplay band keeps the center of the frame


def test_two_stack(regions_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "two.mp4"
    render(regions_video, out, "two_stack", {})
    top, bottom = mean_rgb(out, 0.05, 0.12, 0.05, 0.25), mean_rgb(out, 0.88, 0.95, 0.75, 0.95)
    print(f"\ntwo_stack: top {top}, bottom {bottom}")
    assert is_color(top, "yellow") and is_color(bottom, "magenta")  # left person on top, right below


def test_captions_move_below_picture(tmp_path: Path) -> None:
    from tests.media.test_subtitles import bbox, frames_gray

    black = tmp_path / "black.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(black),
        ],
        check=True,
    )
    words = [{"w": "goal", "s": 200, "e": 900}, {"w": "incoming", "s": 950, "e": 1800}]
    out = tmp_path / "below.mp4"
    render(black, out, "blur_pad", {"src_aspect": 16 / 9, "captions": "below"}, style="bold_pop", words=words)
    tops = [b[1] * 4 for f in frames_gray(out, 270, 480) if (b := bbox(f, 270, 480))]
    picture_bottom = (1920 + 1080 * 9 / 16) / 2  # 1263.75
    print(f"\ncaption top y = {min(tops)} (picture ends at {picture_bottom:.0f}, safe bottom edge 1480)")
    bottoms = [b[3] * 4 for f in frames_gray(out, 270, 480) if (b := bbox(f, 270, 480))]
    assert min(tops) >= picture_bottom and max(bottoms) <= 1480 + 8


def test_auto_switch_changes_layout_on_schedule(regions_video: Path, tmp_path: Path) -> None:
    """Facecam layout during [0.5, 2.5) s, whole picture otherwise: the top band is red (facecam) only then."""
    out = tmp_path / "sw.mp4"
    render(
        regions_video,
        out,
        "auto_switch",
        {
            "facecam": [0.73, 0.0, 0.27, 0.32],
            "top_ratio": 0.35,
            "facecam_intervals": [[500, 2500]],
            "src_aspect": 16 / 9,
        },
    )
    import subprocess as sp

    def top_at(t: float) -> tuple[int, int, int]:
        raw = sp.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(t),
                "-i",
                str(out),
                "-frames:v",
                "1",
                "-vf",
                "scale=90:160,format=rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        px = [raw[(y * 90 + x) * 3 : (y * 90 + x) * 3 + 3] for y in range(5, 50) for x in range(10, 80)]
        return tuple(sum(p[i] for p in px) // len(px) for i in range(3))  # type: ignore[return-value]

    before, during, after = top_at(0.2), top_at(1.5), top_at(2.8)
    print(f"\nauto_switch top band: before {before}, during {during}, after {after}")
    assert is_color(during, "red") and not is_color(before, "red") and not is_color(after, "red")
