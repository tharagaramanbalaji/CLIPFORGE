"""Find a streamer's facecam overlay: where the box is, and when it's on screen. OpenCV's bundled Haar cascade
(no model download).

1. Sample frames every few seconds and detect faces.
2. A facecam is a *cluster* of detections in the same place across many samples (game faces move around).
3. The overlay box: its border is a straight line that stays put while the gameplay behind moves, so in the
   time-averaged gradient image the border is the strongest line near the face. Search
   left/right/top/bottom of
   the face cluster for those lines; fall back to the frame edge (corner facecams) or a padded face box.
4. Presence: per sample, is a face inside that box? Clips use the facecam layout only where it's present.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

Rect = list[float]  # normalized [x, y, w, h]
# Border edge strength vs the frame's median gradient. Measured on a real stream: 15-45 with the facecam on,
# 1-2.5 with it off. Near-black frames (titles) are skipped: their median is ~0 and the ratio explodes.
PRESENT_RATIO = 8.0
MIN_MEDIAN_GRADIENT = 2.0  # border edge strength vs the frame's median gradient


@dataclass
class FacecamResult:
    box: Rect | None  # None: no facecam found
    samples: list[tuple[int, bool]] = field(default_factory=list)  # (ms, face inside the box?)
    sample_every_ms: int = 5000
    cluster_share: float = 0.0  # share of samples with a face in the box
    method: str = "none"  # border | padded_face | none

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def present_share(self, start_ms: int, end_ms: int) -> float:
        inside = [ok for ms, ok in self.samples if start_ms <= ms <= end_ms]
        if not inside:  # short clip between samples: use the nearest sample
            near = min(self.samples, key=lambda s: abs(s[0] - (start_ms + end_ms) / 2), default=None)
            return 1.0 if near and near[1] else 0.0
        return sum(inside) / len(inside)


def _detect(gray: Any, cascades: list[Any], min_size: int) -> list[tuple[int, int, int, int]]:
    out: list[tuple[int, int, int, int]] = []
    for c in cascades:  # two frontal models: more hits on glasses/headsets; clustering removes the noise
        faces = c.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=4, minSize=(min_size, min_size))
        out += [tuple(int(v) for v in f) for f in faces]  # type: ignore[misc]
    return out


def _strongest_line(profile: Any, lo: int, hi: int, min_strength: float) -> int | None:
    import numpy as np

    lo, hi = max(0, lo), min(len(profile), hi)
    if hi - lo < 3:
        return None
    seg = profile[lo:hi]
    i = int(np.argmax(seg))
    return lo + i if seg[i] >= min_strength else None


def find_facecam(
    video: Path,
    *,
    every_ms: int = 5000,
    max_samples: int = 240,
    min_share: float = 0.15,
    start_ms: int = 0,
    end_ms: int | None = None,
) -> FacecamResult:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_ms = int(n / fps * 1000)
    end = min(duration_ms, end_ms) if end_ms is not None else duration_ms
    step_ms = max(every_ms, (end - start_ms) // max_samples)
    cascades = [
        cv2.CascadeClassifier(cv2.data.haarcascades + name)  # type: ignore[attr-defined]
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml")
    ]
    frames: list[tuple[int, Any, list[tuple[int, int, int, int]]]] = []
    try:
        for ms in range(start_ms, end, step_ms):
            cap.set(cv2.CAP_PROP_POS_MSEC, ms)
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frames.append((ms, gray, _detect(gray, cascades, max(24, gray.shape[0] // 20))))
    finally:
        cap.release()
    if not frames:
        return FacecamResult(None, [], step_ms)
    h, w = frames[0][1].shape

    # 1-2: cluster face centers on a coarse grid; the facecam is the most frequent cell (with neighbours)
    grid = 12
    votes: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
    for _, _, faces in frames:
        cells = set()
        for f in faces:
            cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
            cells.add((int(cx / w * grid), int(cy / h * grid)))
            votes.setdefault((int(cx / w * grid), int(cy / h * grid)), []).append(f)
    if not votes:
        return FacecamResult(None, [(ms, False) for ms, _, _ in frames], step_ms)
    best = max(
        votes,
        key=lambda c: sum(
            len(votes.get((c[0] + dx, c[1] + dy), [])) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        ),
    )
    cluster = [
        f for dx in (-1, 0, 1) for dy in (-1, 0, 1) for f in votes.get((best[0] + dx, best[1] + dy), [])
    ]
    fx = int(np.median([f[0] for f in cluster]))
    fy = int(np.median([f[1] for f in cluster]))
    fw = int(np.median([f[2] for f in cluster]))
    fh = int(np.median([f[3] for f in cluster]))

    if len(cluster) < 3:
        return FacecamResult(None, [(ms, False) for ms, _, _ in frames], step_ms)
    pad_box = (max(0, fx - fw), max(0, fy - int(0.8 * fh)), min(w, fx + 2 * fw), min(h, fy + int(2.2 * fh)))

    # 3: static border lines in the time-averaged gradient (gameplay edges move and average out)
    grads = [
        (np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)), np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)))
        for _, g, _ in frames
    ]
    gx = np.mean([x for x, _ in grads], axis=0)
    gy = np.mean([y for _, y in grads], axis=0)
    rows = slice(fy, min(h, fy + fh))
    cols = slice(fx, min(w, fx + fw))
    col_profile, row_profile = gx[rows, :].mean(axis=0), gy[:, cols].mean(axis=1)
    strong_x, strong_y = 3.0 * float(np.median(col_profile)), 3.0 * float(np.median(row_profile))
    # A side close to the frame edge is the frame edge (corner facecams); only search the open sides, or lines
    # inside the camera picture (door frames, shoulders) get mistaken for the border.
    near_l, near_r = fx < 2.5 * fw, w - (fx + fw) < 2.5 * fw
    near_t, near_b = fy < 2.0 * fh, h - (fy + fh) < 2.5 * fh
    left = None if near_l else _strongest_line(col_profile, fx - 3 * fw, fx - fw // 4, strong_x)
    right = None if near_r else _strongest_line(col_profile, fx + fw + fw // 4, fx + 4 * fw, strong_x)
    top = None if near_t else _strongest_line(row_profile, fy - 3 * fh, fy - fh // 4, strong_y)
    bottom = None if near_b else _strongest_line(row_profile, fy + fh + fh // 4, fy + 4 * fh, strong_y)
    found = [v for v in (left, right, top, bottom) if v is not None]
    method = (
        "border"
        if (left is not None or right is not None) and (top is not None or bottom is not None)
        else "padded_face"
    )
    x0 = left if left is not None else (0 if fx < 2.5 * fw else pad_box[0])
    x1 = right if right is not None else (w if w - (fx + fw) < 2.5 * fw else pad_box[2])
    y0 = top if top is not None else (0 if fy < 2 * fh else pad_box[1])
    y1 = bottom if bottom is not None else (h if h - (fy + fh) < 2.5 * fh else pad_box[3])
    box = [round(x0 / w, 4), round(y0 / h, 4), round((x1 - x0) / w, 4), round((y1 - y0) / h, 4)]

    # 4: presence per sample = the border is there (strong edge along the found lines vs the frame's median)
    def present(i: int, faces: list[tuple[int, int, int, int]]) -> bool:
        x, y = grads[i]
        if method == "border":
            if float(np.median(x)) < MIN_MEDIAN_GRADIENT:
                return False  # near-black / flat frame (title card): no facecam shown
            checks = []
            if left is not None:
                checks.append(
                    float(x[y0:y1, max(0, left - 1) : left + 2].mean()) / (float(np.median(x)) + 1e-3)
                )
            if right is not None:
                checks.append(
                    float(x[y0:y1, max(0, right - 1) : right + 2].mean()) / (float(np.median(x)) + 1e-3)
                )
            if top is not None:
                checks.append(
                    float(y[max(0, top - 1) : top + 2, x0:x1].mean()) / (float(np.median(y)) + 1e-3)
                )
            if bottom is not None:
                checks.append(
                    float(y[max(0, bottom - 1) : bottom + 2, x0:x1].mean()) / (float(np.median(y)) + 1e-3)
                )
            return bool(checks) and min(checks) > PRESENT_RATIO
        return any(x0 <= f[0] + f[2] / 2 <= x1 and y0 <= f[1] + f[3] / 2 <= y1 for f in faces)

    samples = [(ms, present(i, faces)) for i, (ms, _, faces) in enumerate(frames)]
    del found
    share = sum(ok for _, ok in samples) / len(samples)
    if share < min_share:
        return FacecamResult(None, samples, step_ms, round(share, 3), method)
    return FacecamResult(box, samples, step_ms, round(share, 3), method)


AUTO_ALWAYS = 0.95  # facecam on screen nearly the whole clip: plain facecam layout
AUTO_NEVER = 0.2  # rarely on screen: plain fit + blur
MIN_SEGMENT_MS = 2000  # layout switches last at least this long (no flicker)
MAX_BOX_AREA = 0.5  # a "facecam" covering half the frame isn't an overlay (full-screen camera, title card)


def presence_intervals(
    samples: list[tuple[int, bool]], start_ms: int, end_ms: int, step_ms: int
) -> list[list[int]]:
    """Clip-relative [start, end) ms intervals where the facecam is on screen;
    short runs and gaps are smoothed out."""
    runs: list[list[int]] = []
    for ms, ok in samples:
        if not start_ms <= ms <= end_ms:
            continue
        a, b = ms - start_ms - step_ms // 2, ms - start_ms + step_ms // 2
        if ok:
            if runs and a - runs[-1][1] < MIN_SEGMENT_MS:
                runs[-1][1] = b  # bridge short gaps
            else:
                runs.append([a, b])
    clip_len = end_ms - start_ms
    out = [[max(0, a), min(clip_len, b)] for a, b in runs if b - a >= MIN_SEGMENT_MS]
    return out


def auto_layout(proxy: Path, start_ms: int, end_ms: int, src_aspect: float) -> dict[str, Any]:
    """Per-clip decision (edited videos move/zoom the facecam, so a global box isn't enough):
    facecam on top while the facecam is on screen, the whole picture (captions below) otherwise."""
    r = find_facecam(proxy, every_ms=1000, start_ms=start_ms, end_ms=end_ms, min_share=0.2)
    box = r.box if r.box and r.box[2] * r.box[3] <= MAX_BOX_AREA else None
    share = r.present_share(start_ms, end_ms) if box else 0.0
    info: dict[str, Any] = {
        "auto": {"box": r.box, "share": round(share, 2), "method": r.method},
        "src_aspect": src_aspect,
        "captions": "below",
    }
    if box is None or share < AUTO_NEVER:
        return {"frame_mode": "blur_pad", "layout": info}
    if share >= AUTO_ALWAYS:
        return {"frame_mode": "facecam_split", "layout": {**info, "facecam": box, "top_ratio": 0.35}}
    intervals = presence_intervals(r.samples, start_ms, end_ms, r.sample_every_ms)
    if not intervals:
        return {"frame_mode": "blur_pad", "layout": info}
    return {
        "frame_mode": "auto_switch",
        "layout": {**info, "facecam": box, "top_ratio": 0.35, "facecam_intervals": intervals},
    }
