"""PySceneDetect AdaptiveDetector on the proxy, plus one JPEG keyframe per scene (OpenCV seek + grab)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SceneConfig:
    adaptive_threshold: float = 3.0
    min_scene_len_s: float = 0.5
    min_content_val: float = 15.0
    keyframe_quality: int = 85
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Scene:
    idx: int
    start_frame: int
    end_frame: int  # exclusive
    start_ms: int
    end_ms: int


def frames_to_ms(frame: int, fps: float) -> int:
    return round(frame * 1000 / fps)


def detect_scenes(video: Path, cfg: SceneConfig) -> tuple[list[Scene], dict[str, Any]]:
    from scenedetect import AdaptiveDetector, SceneManager, open_video

    t0 = time.monotonic()
    v = open_video(str(video))
    fps = float(v.frame_rate)
    manager = SceneManager()
    manager.add_detector(
        AdaptiveDetector(
            adaptive_threshold=cfg.adaptive_threshold,
            min_scene_len=max(1, round(cfg.min_scene_len_s * fps)),
            min_content_val=cfg.min_content_val,
        )
    )
    manager.detect_scenes(v, show_progress=False)
    raw = manager.get_scene_list(start_in_scene=True)
    total_frames = v.frame_number
    scenes = [
        Scene(
            i,
            s.get_frames(),
            e.get_frames(),
            frames_to_ms(s.get_frames(), fps),
            frames_to_ms(e.get_frames(), fps),
        )
        for i, (s, e) in enumerate(raw)
    ]
    if not scenes and total_frames:
        scenes = [Scene(0, 0, total_frames, 0, frames_to_ms(total_frames, fps))]
    wall = time.monotonic() - t0
    return scenes, {
        "scenes": len(scenes),
        "fps": fps,
        "frames": total_frames,
        "wall_s": round(wall, 2),
        "realtime_factor": round(total_frames / fps / wall, 1) if wall and fps else None,
    }


def write_keyframes(
    video: Path,
    scenes: list[Scene],
    out_dir: Path,
    quality: int = 85,
    on_progress: Callable[[float], bool] | None = None,
) -> dict[int, Path]:
    """Middle frame of each scene as JPEG. Seeks per scene (proxy has a keyframe every ~1 s)."""
    import cv2

    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    written: dict[int, Path] = {}
    try:
        for n, sc in enumerate(scenes):
            mid = (sc.start_frame + max(sc.start_frame, sc.end_frame - 1)) // 2
            cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
            ok, frame = cap.read()
            if ok:
                path = out_dir / f"scene_{sc.idx:05d}.jpg"
                cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                written[sc.idx] = path
            if on_progress is not None and n % 25 == 0 and not on_progress(n / max(1, len(scenes))):
                break
    finally:
        cap.release()
    return written
