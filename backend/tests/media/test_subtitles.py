"""Phase 6 acceptance on real renders: caption timing follows word timestamps, text stays inside each platform's
safe area, only bundled fonts are used, and edited words re-render."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.engine.render.runner import render_clip
from clipforge.engine.render.spec import PRESETS, RenderSpec
from clipforge.engine.subtitles.styles import SAFE_AREAS, STYLES

pytestmark = pytest.mark.media
FPS = 30


@pytest.fixture(scope="module")
def black_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Pure black frames + silence: any non-black pixel in the output is subtitle text."""
    out = tmp_path_factory.mktemp("black") / "black.mp4"
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
            f"color=c=black:s=1280x720:r={FPS}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "12",
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


def frames_gray(video: Path, w: int, h: int) -> list[bytes]:
    raw = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            f"scale={w}:{h},format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    n = w * h
    return [raw[i : i + n] for i in range(0, len(raw), n)]


def bbox(frame: bytes, w: int, h: int, thr: int = 60) -> tuple[int, int, int, int] | None:
    xs, ys = [], []
    for y in range(h):
        row = frame[y * w : (y + 1) * w]
        for x, v in enumerate(row):
            if v > thr:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


WORDS = [  # ms relative to the clip start; two captions: words 0-2 and 3-5 (pause before word 3)
    {"w": "this", "s": 1000, "e": 1300},
    {"w": "is", "s": 1350, "e": 1500},
    {"w": "crazy", "s": 1550, "e": 2100},
    {"w": "no", "s": 3200, "e": 3450},
    {"w": "way", "s": 3500, "e": 3800},
    {"w": "bro", "s": 3850, "e": 4300},
]


def spec_with(
    words: list[dict[str, object]], style: str = "bold_pop", preset: str = "shorts", **extra: object
) -> RenderSpec:
    return RenderSpec(
        start_ms=2000,
        end_ms=8000,
        preset=preset,
        frame_mode="crop",
        encoder="x264",
        extra={"subtitles": {"style": style, "words": words, **extra}},
    )


def test_caption_timing_follows_words(black_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "t.mp4"
    used = render_clip("ffmpeg", black_video, out, spec_with(WORDS), has_audio=True)
    w, h = 108, 192
    lit = [bbox(f, w, h) is not None for f in frames_gray(out, w, h)]
    on = [i for i, v in enumerate(lit) if v and (i == 0 or not lit[i - 1])]
    off = [i for i, v in enumerate(lit) if not v and i > 0 and lit[i - 1]]
    print(f"\ncaption on at frames {on}, off at {off}; expected on {[30, 96]} (word starts 1.0 s, 3.2 s)")
    assert on == [30, 96]  # exactly the frame where the first word of each caption starts
    assert off and abs(off[-1] - round((4300 + 400) * FPS / 1000)) <= 1  # last word end + 0.4 s linger
    assert used.extra["subtitles"]["style"] == "bold_pop"


def _active_highlight_changes(video: Path) -> list[int]:
    """Frames where the yellow (active-word) region moves: the highlight switching words."""
    w, h = 216, 384
    raw = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            f"scale={w}:{h},format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    n = w * h * 3
    centers: list[float | None] = []
    for i in range(0, len(raw), n):
        f = raw[i : i + n]
        xs = [p // 3 % w for p in range(0, n, 3) if f[p] > 180 and f[p + 1] > 150 and f[p + 2] < 90]
        centers.append(sum(xs) / len(xs) if len(xs) > 15 else None)
    return [
        i
        for i in range(1, len(centers))
        if centers[i] is not None and centers[i - 1] is not None and abs(centers[i] - centers[i - 1]) > 4
    ]


def test_active_word_switches_on_word_start(black_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "k.mp4"
    render_clip("ffmpeg", black_video, out, spec_with(WORDS), has_audio=True)
    switches = _active_highlight_changes(out)
    expected = [round(ms * FPS / 1000) for ms in (1350, 1550, 3500, 3850)]  # within-caption word starts
    print(f"\nhighlight switches at frames {switches}, expected {expected}")
    assert all(any(abs(s - e) <= 1 for s in switches) for e in expected)


@pytest.mark.parametrize("preset", ["shorts", "reels", "tiktok", "square"])
@pytest.mark.parametrize("style", sorted(STYLES))
def test_text_inside_safe_area(black_video: Path, tmp_path: Path, preset: str, style: str) -> None:
    long_words = [
        {"w": t, "s": 500 + i * 300, "e": 780 + i * 300}
        for i, t in enumerate(["unbelievable", "absolutely", "incredible", "championship"])
    ]
    out = tmp_path / "s.mp4"
    render_clip(
        "ffmpeg",
        black_video,
        out,
        spec_with(long_words, style=style, preset=preset, hook_text="WAIT FOR IT", watermark="@mychannel"),
        has_audio=True,
    )
    p, safe = PRESETS[preset], SAFE_AREAS[preset]
    union: list[int] | None = None
    for f in frames_gray(out, p.width // 4, p.height // 4):
        b = bbox(f, p.width // 4, p.height // 4)
        if b:
            union = (
                list(b)
                if union is None
                else [min(union[0], b[0]), min(union[1], b[1]), max(union[2], b[2]), max(union[3], b[3])]
            )
    assert union is not None
    x0, y0, x1, y1 = (v * 4 for v in union)
    print(
        f"\n{preset}/{style}: text bbox {x0},{y0}-{x1},{y1}; safe box {safe.left},{safe.top}-"
        f"{p.width - safe.right},{p.height - safe.bottom}"
    )
    tol = 8  # 4x downscale rounding
    assert x0 >= safe.left - tol and y0 >= safe.top - tol
    assert x1 <= p.width - safe.right + tol and y1 <= p.height - safe.bottom + tol


def test_only_bundled_fonts_are_used(black_video: Path, tmp_path: Path) -> None:
    """Renders run with an empty fontconfig, so system fonts are invisible: text appears with the bundled font
    folder, and nothing appears when that folder is empty (proving there's no hidden system-font fallback)."""
    from clipforge.engine.render.filtergraph import build_render_args
    from clipforge.engine.subtitles import styles
    from clipforge.engine.subtitles.render_ass import ass_for

    spec = spec_with(WORDS)
    ass = tmp_path / "x.ass"
    ass.write_text(ass_for(spec) or "", encoding="utf-8")

    def render(fonts_dir: Path, name: str) -> tuple[bool, str]:
        out = tmp_path / name
        args = build_render_args(black_video, out, spec, spec.preset_obj, True, None, ass)
        args = [a.replace(str(styles.FONTS_DIR), str(fonts_dir)) for a in args]
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "verbose", *args],
            capture_output=True,
            text=True,
            env=styles.isolated_font_env(),
        )
        assert res.returncode == 0, res.stderr[-500:]
        lit = any(bbox(f, 108, 192) for f in frames_gray(out, 108, 192))
        return lit, " | ".join(ln for ln in res.stderr.splitlines() if "fontselect" in ln)

    with_bundle, log = render(styles.FONTS_DIR, "with.mp4")
    empty = tmp_path / "nofonts"
    empty.mkdir()
    without_bundle, _ = render(empty, "without.mp4")
    print(f"\nwith bundle: text={with_bundle} [{log[:90]}]; empty font folder: text={without_bundle}")
    assert with_bundle and "DejaVuSans-Bold" in log
    assert not without_bundle  # no system fallback exists


def test_edited_words_rerender(black_video: Path, tmp_path: Path) -> None:
    """Changing the words (text and timing) changes the output; the original words re-render identically."""
    import hashlib

    edited = [dict(w) for w in WORDS]
    edited[2]["w"] = "insane"
    edited[3] = {"w": "no", "s": 2700, "e": 3450}  # moved earlier by 0.5 s
    a, b, c = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"
    render_clip("ffmpeg", black_video, a, spec_with(WORDS), has_audio=True)
    render_clip("ffmpeg", black_video, b, spec_with(edited), has_audio=True)
    render_clip("ffmpeg", black_video, c, spec_with(WORDS), has_audio=True)
    h = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b, c)]
    assert h[0] == h[2] and h[0] != h[1]
    w, hh = 108, 192
    lit = [bbox(f, w, hh) is not None for f in frames_gray(b, w, hh)]
    on = [i for i, v in enumerate(lit) if v and (i == 0 or not lit[i - 1])]
    assert on == [30, 81]  # second caption now starts at 2.7 s
