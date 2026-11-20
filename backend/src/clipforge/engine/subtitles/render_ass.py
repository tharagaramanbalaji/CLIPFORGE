"""RenderSpec.extra["subtitles"] -> ASS text. Deterministic, so renders stay reproducible from the spec.

extra["subtitles"] = {"style": "bold_pop", "words": [{"w","s","e"}...] (ms relative to clip start),
                      "hook_text": str|None, "hook_ms": int, "watermark": str|None}
"""

from __future__ import annotations

from typing import Any

from clipforge.engine.render.spec import RenderSpec
from clipforge.engine.subtitles.ass import Overlays, build_ass
from clipforge.engine.subtitles.grouping import Caption, W, group_words
from clipforge.engine.subtitles.styles import SAFE_AREAS, STYLES, max_line_chars


def captions_for(spec: RenderSpec) -> list[Caption]:
    sub: dict[str, Any] = spec.extra.get("subtitles") or {}
    if not sub.get("words"):
        return []
    style = STYLES[sub.get("style", "bold_pop")]
    preset = spec.preset_obj
    words = [W(str(w["w"]), int(w["s"]), int(w["e"])) for w in sub["words"]]
    return group_words(
        words,
        max_words=style.max_words,
        max_chars=max_line_chars(style, preset.width, SAFE_AREAS[preset.name]),
    )


def ass_for(spec: RenderSpec) -> str | None:
    sub: dict[str, Any] = spec.extra.get("subtitles") or {}
    if not sub or not (sub.get("words") or sub.get("hook_text") or sub.get("watermark")):
        return None
    preset = spec.preset_obj
    style = STYLES[sub.get("style", "bold_pop")]
    overlays = Overlays(
        hook_text=sub.get("hook_text") or None,
        hook_ms=int(sub.get("hook_ms", 3000)),
        watermark=sub.get("watermark") or None,
    )
    from clipforge.engine.render.layouts import caption_margin_v

    k = preset.width / 1080
    text_h = int(style.size * k * 1.25 + 2 * style.outline_px * k)
    mv = caption_margin_v(spec, preset, text_h, SAFE_AREAS[preset.name].bottom)
    return build_ass(
        captions_for(spec), style, preset.width, preset.height, SAFE_AREAS[preset.name], overlays, margin_v=mv
    )
