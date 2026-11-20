"""Caption styles and per-platform safe areas.

Safe areas keep text clear of each app's overlays (like/comment/share column on the right,
caption/channel/music
block at the bottom, top bar). Values are for a 1080x1920 frame and are deliberately conservative.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parents[4] / "assets" / "fonts"  # backend/assets/fonts (bundled)
FONT_NAME = "DejaVu Sans"


def isolated_font_env() -> dict[str, str]:
    """Process env for FFmpeg renders: fontconfig sees no system fonts,
    only `fontsdir` (the bundle) is used."""
    import os

    return {**os.environ, "FONTCONFIG_FILE": str(FONTS_DIR / "fonts.conf")}


@dataclass(frozen=True, slots=True)
class SafeArea:
    top: int
    bottom: int
    left: int
    right: int


SAFE_AREAS: dict[str, SafeArea] = {
    "shorts": SafeArea(top=180, bottom=440, left=60, right=150),
    "reels": SafeArea(top=220, bottom=460, left=60, right=150),
    "tiktok": SafeArea(top=180, bottom=500, left=60, right=170),
    "wide": SafeArea(top=60, bottom=90, left=100, right=100),
    "square": SafeArea(top=60, bottom=90, left=60, right=60),
}


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    name: str
    size: int  # px at 1080 wide (scaled for other widths)
    primary: str  # ASS colour &HAABBGGRR
    active: str
    outline: str
    back: str
    outline_px: int
    shadow_px: int
    border_style: int  # 1 = outline + shadow, 3 = opaque box
    uppercase: bool
    max_words: int
    pop: bool  # active word slightly enlarged
    cumulative: bool  # karaoke: words stay highlighted once spoken
    lift: float  # caption baseline height above the bottom safe edge, as a fraction of frame height


STYLES: dict[str, CaptionStyle] = {
    "bold_pop": CaptionStyle(
        "bold_pop",
        78,
        "&H00FFFFFF",
        "&H0000E5FF",
        "&H00000000",
        "&H80000000",
        7,
        2,
        1,
        True,
        3,
        True,
        False,
        0.12,
    ),
    "clean_box": CaptionStyle(
        "clean_box",
        60,
        "&H00FFFFFF",
        "&H0050E070",
        "&H00000000",
        "&H96000000",
        14,
        0,
        3,
        False,
        5,
        False,
        False,
        0.10,
    ),
    "karaoke": CaptionStyle(
        "karaoke",
        70,
        "&H00FFFFFF",
        "&H00FFD400",
        "&H00101010",
        "&H80000000",
        6,
        1,
        1,
        False,
        4,
        False,
        True,
        0.12,
    ),
}

AVG_CHAR_WIDTH = (
    0.66  # DejaVu Sans Bold, in em; used to fit lines inside the safe width (with uppercase margin)
)


def max_line_chars(style: CaptionStyle, width: int, safe: SafeArea) -> int:
    scale = width / 1080
    usable = width - safe.left - safe.right - 2 * style.outline_px * scale
    per_char = AVG_CHAR_WIDTH * style.size * scale * (1.08 if style.uppercase else 1.0)
    return max(8, int(usable / per_char))
