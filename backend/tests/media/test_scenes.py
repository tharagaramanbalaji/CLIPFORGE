"""Scene detection must find the synthetic hard cuts at exactly the right frames."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.engine.scenes.detect import SceneConfig, detect_scenes, write_keyframes
from tests.fixtures_scenes import FPS, SHOTS, cut_frames, make_cut_video

pytestmark = pytest.mark.media


def test_cuts_detected_exactly(tmp_path: Path) -> None:
    video = make_cut_video(tmp_path)
    scenes, metrics = detect_scenes(video, SceneConfig())
    detected = [s.start_frame for s in scenes[1:]]
    print(f"\nexpected cuts {cut_frames()} detected {detected} metrics {metrics}")
    assert detected == cut_frames()
    assert scenes[0].start_frame == 0 and scenes[-1].end_frame == sum(n for _, n in SHOTS)
    assert [s.start_ms for s in scenes[1:]] == [round(f * 1000 / FPS) for f in cut_frames()]
    frames = write_keyframes(video, scenes, tmp_path / "kf")
    assert sorted(frames) == list(range(len(SHOTS)))
    assert all(p.stat().st_size > 1000 for p in frames.values())


def test_no_cuts_in_continuous_shot(tmp_path: Path) -> None:
    video = tmp_path / "one.mp4"
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
            "testsrc2=size=960x540:rate=30",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    scenes, _ = detect_scenes(video, SceneConfig())
    assert len(scenes) == 1
