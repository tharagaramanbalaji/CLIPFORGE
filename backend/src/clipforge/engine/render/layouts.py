"""Static vertical layouts (Phase 7, slim): no face detection, regions are chosen by the user.

spec.extra["layout"] (all rects normalized to the source frame: [x, y, w, h] in 0..1):
- facecam_split: {"facecam": rect, "gameplay": rect (default whole frame), "top_ratio": 0.35}
    facecam region fills the top band, gameplay fills the rest (center of the region kept).
- two_stack:     {"top": rect (default left half), "bottom": rect (default right half)}
    two people (or two views) stacked, each band half the height.
- blur_pad with {"captions": "below"}: whole picture centered over blur, captions in the blurred
  band below it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from clipforge.engine.render.spec import Preset, RenderSpec

Rect = list[float]
FULL: Rect = [0.0, 0.0, 1.0, 1.0]
LEFT_HALF: Rect = [0.0, 0.0, 0.5, 1.0]
RIGHT_HALF: Rect = [0.5, 0.0, 0.5, 1.0]

FACECAM_CORNERS: dict[str, Rect] = {  # common facecam spots, ~1/4 width of a 16:9 frame
    "top_left": [0.0, 0.0, 0.27, 0.32],
    "top_right": [0.73, 0.0, 0.27, 0.32],
    "bottom_left": [0.0, 0.68, 0.27, 0.32],
    "bottom_right": [0.73, 0.68, 0.27, 0.32],
}


def layout_of(spec: RenderSpec) -> dict[str, Any]:
    return dict(spec.extra.get("layout") or {})


def check_rect(rect: Rect) -> Rect:
    x, y, w, h = (float(v) for v in rect)
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.0001 and y + h <= 1.0001):
        raise ValueError(f"region {rect} must lie inside the frame (normalized 0..1)")
    return [x, y, w, h]


def _crop(rect: Rect) -> str:
    x, y, w, h = check_rect(rect)
    return f"crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f}"


def _fill(width: int, height: int) -> str:
    """Scale to cover the band, keep the center (no black bars)."""
    return f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height}"


def band_heights(spec: RenderSpec, preset: Preset) -> tuple[int, int]:
    lay = layout_of(spec)
    ratio = float(lay.get("top_ratio", 0.35)) if spec.frame_mode == "facecam_split" else 0.5
    top = int(preset.height * ratio) // 2 * 2
    return top, preset.height - top


def _blur_pad_branch(src: str, w: int, h: int, out: str) -> str:
    return (
        f"{src}split=2[bs{out}][fs{out}];"
        f"[bs{out}]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
        f"boxblur=10:2,scale={w}:{h}[bg{out}];"
        f"[fs{out}]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fg{out}];"
        f"[bg{out}][fg{out}]overlay=(W-w)/2:(H-h)/2[{out}]"
    )


def switch_filter(spec: RenderSpec, preset: Preset, head: str, tail: str) -> str:
    """auto_switch: the facecam layout is shown during `facecam_intervals`, the whole picture otherwise.
    Both layouts are built from the same frames and overlaid with a time-based enable (cuts, no blending)."""
    lay, w, h = layout_of(spec), preset.width, preset.height
    top_h, bot_h = band_heights(replace(spec, frame_mode="facecam_split"), preset)
    a, b = lay["facecam"], lay.get("gameplay") or FULL
    enable = "+".join(
        f"between(t,{s / 1000:.3f},{e / 1000 - 0.001:.3f})" for s, e in lay["facecam_intervals"]
    )
    return (
        f"{head}split=3[sa][sb][sc];[sa]{_crop(a)},{_fill(w, top_h)}[ta];"
        f"[sb]{_crop(b)},{_fill(w, bot_h)}[tb];"
        f"[ta][tb]vstack=inputs=2[fc];" + _blur_pad_branch("[sc]", w, h, "bp") + ";"
        f"[bp][fc]overlay=0:0:enable='{enable}',{tail}"
    )


def layout_filter(spec: RenderSpec, preset: Preset, head: str, tail: str) -> str:
    """`head` = '[0:v]trim...,' and `tail` = 'setsar=1,...format=yuv420p[v]'."""
    if spec.frame_mode == "auto_switch":
        return switch_filter(spec, preset, head, tail)
    lay, w = layout_of(spec), preset.width
    top_h, bot_h = band_heights(spec, preset)
    if spec.frame_mode == "facecam_split":
        a, b = lay.get("facecam"), lay.get("gameplay") or FULL
        if not a:
            raise ValueError("facecam_split needs a facecam region")
    else:  # two_stack
        a, b = lay.get("top") or LEFT_HALF, lay.get("bottom") or RIGHT_HALF
    return (
        f"{head}split=2[ra][rb];[ra]{_crop(a)},{_fill(w, top_h)}[ta];[rb]{_crop(b)},{_fill(w, bot_h)}[tb];"
        f"[ta][tb]vstack=inputs=2,{tail}"
    )


def picture_bottom(spec: RenderSpec, preset: Preset) -> int | None:
    """For blur_pad: y where the centered picture ends (needs the source aspect)."""
    aspect = layout_of(spec).get("src_aspect")
    if not aspect:
        return None
    pic_h = min(preset.height, preset.width / float(aspect))
    return int((preset.height + pic_h) / 2)


def caption_margin_v(spec: RenderSpec, preset: Preset, text_h: int, safe_bottom: int) -> int | None:
    """Bottom margin for captions that keeps them off the picture/seam; None = style default."""
    lay, h = layout_of(spec), preset.height
    gap = int(30 * preset.width / 1080)
    if spec.frame_mode == "facecam_split":
        seam, _ = band_heights(spec, preset)
        mv = h - seam - gap - text_h  # just below the seam, over the top of the gameplay
    elif spec.frame_mode == "two_stack":
        mv = h // 2 - text_h // 2  # centered on the seam between the two people
    elif spec.frame_mode in ("blur_pad", "auto_switch") and lay.get("captions") == "below":
        bottom = picture_bottom(spec, preset)
        if bottom is None:
            return None
        mv = h - bottom - gap - text_h  # in the blurred band under the picture
    else:
        return None
    return max(safe_bottom, mv)
