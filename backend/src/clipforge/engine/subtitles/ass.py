"""ASS generation (rendered by libass inside FFmpeg) and SRT/VTT export.

Word highlighting: each caption is emitted as one Dialogue event per word, covering exactly the time that word
is active, so the highlight switches on each word's own start time (to the centisecond ASS allows).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.engine.subtitles.grouping import Caption
from clipforge.engine.subtitles.styles import FONT_NAME, CaptionStyle, SafeArea


@dataclass(frozen=True, slots=True)
class Overlays:
    hook_text: str | None = None
    hook_ms: int = 3000
    watermark: str | None = None


def ass_time(ms: int) -> str:
    cs = max(0, round(ms / 10))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def clean(text: str) -> str:
    """No ASS override braces or backslashes from user/ASR text."""
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _style_line(
    name: str,
    size: int,
    primary: str,
    outline: str,
    back: str,
    bold: bool,
    border: int,
    outline_px: float,
    shadow: float,
    align: int,
    ml: int,
    mr: int,
    mv: int,
) -> str:
    return (
        f"Style: {name},{FONT_NAME},{size},{primary},{primary},{outline},{back},{-1 if bold else 0},0,0,0,"
        f"100,100,0,0,{border},{outline_px:g},{shadow:g},{align},{ml},{mr},{mv},1"
    )


def caption_margin_v(style: CaptionStyle, height: int, safe: SafeArea) -> int:
    return safe.bottom + int(style.lift * height)


def build_ass(
    captions: Sequence[Caption],
    style: CaptionStyle,
    width: int,
    height: int,
    safe: SafeArea,
    overlays: Overlays | None = None,
    margin_v: int | None = None,
) -> str:
    overlays = overlays or Overlays()
    k = width / 1080
    size = round(style.size * k)
    mv = margin_v if margin_v is not None else caption_margin_v(style, height, safe)
    styles = [
        _style_line(
            "Caption",
            size,
            style.primary,
            style.outline,
            style.back,
            True,
            style.border_style,
            style.outline_px * k,
            style.shadow_px * k,
            2,
            safe.left,
            safe.right,
            mv,
        ),
        _style_line(
            "Hook",
            round(64 * k),
            "&H00FFFFFF",
            "&H00000000",
            "&HB4000000",
            True,
            3,
            16 * k,
            0,
            8,
            safe.left,
            safe.right,
            safe.top + round(30 * k),
        ),
        _style_line(
            "Watermark",
            round(34 * k),
            "&H80FFFFFF",
            "&H80000000",
            "&H00000000",
            True,
            1,
            2 * k,
            0,
            1,
            safe.left,
            safe.right,
            safe.bottom + round(20 * k),
        ),
    ]
    events: list[str] = []
    for cap in captions:
        words = [clean(w.text.upper() if style.uppercase else w.text) for w in cap.words]
        for i, w in enumerate(cap.words):
            start = w.s if i else cap.start
            end = cap.words[i + 1].s if i + 1 < len(cap.words) else cap.end
            if end <= start:
                continue
            parts = []
            for j, text in enumerate(words):
                active = j == i or (style.cumulative and j < i)
                if active:
                    pop = r"\fscx110\fscy110" if style.pop and j == i else ""
                    parts.append(f"{{\\c{style.active}{pop}}}{text}{{\\r}}")
                else:
                    parts.append(text)
            events.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{' '.join(parts)}")
    if overlays.hook_text:
        events.append(
            f"Dialogue: 2,{ass_time(0)},{ass_time(overlays.hook_ms)},Hook,,0,0,0,,{clean(overlays.hook_text)}"
        )
    if overlays.watermark:
        end = max((c.end for c in captions), default=overlays.hook_ms) + 60_000
        events.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(end)},Watermark,,0,0,0,,{clean(overlays.watermark)}"
        )
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            *styles,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


def _srt_time(ms: int, sep: str) -> str:
    h, rem = divmod(max(0, ms), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(captions: Sequence[Caption]) -> str:
    return "\n".join(
        f"{i}\n{_srt_time(c.start, ',')} --> {_srt_time(c.end, ',')}\n{c.text}\n"
        for i, c in enumerate(captions, 1)
    )


def to_vtt(captions: Sequence[Caption]) -> str:
    return "WEBVTT\n\n" + "\n".join(
        f"{_srt_time(c.start, '.')} --> {_srt_time(c.end, '.')}\n{c.text}\n" for c in captions
    )
