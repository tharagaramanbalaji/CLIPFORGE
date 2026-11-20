"""Group words into short on-screen captions (spec Phase 6: by pause and line length)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class W:
    text: str
    s: int  # ms, relative to clip start
    e: int


@dataclass(frozen=True, slots=True)
class Caption:
    words: tuple[W, ...]
    start: int
    end: int

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def clip_words(words: Sequence[dict[str, object]], clip_start_ms: int, clip_end_ms: int) -> list[W]:
    """Words fully or mostly inside the clip, re-timed relative to the clip start and clamped to it."""
    out = []
    duration = clip_end_ms - clip_start_ms
    for w in words:
        s, e = int(w["s"]) - clip_start_ms, int(w["e"]) - clip_start_ms  # type: ignore[call-overload]
        mid = (s + e) / 2
        text = str(w["w"]).strip()
        if text and 0 <= mid < duration:
            out.append(W(text, max(0, s), min(duration, max(e, s + 1))))
    return out


def group_words(
    words: Sequence[W], *, max_words: int, max_chars: int, pause_ms: int = 350, linger_ms: int = 400
) -> list[Caption]:
    """New caption on: max words, line too long, a pause, or after sentence punctuation."""
    groups: list[list[W]] = []
    for w in words:
        cur = groups[-1] if groups else None
        if cur is not None:
            text_len = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            ends_sentence = cur[-1].text[-1:] in ".!?"
            if (
                len(cur) < max_words
                and text_len <= max_chars
                and w.s - cur[-1].e < pause_ms
                and not ends_sentence
            ):
                cur.append(w)
                continue
        groups.append([w])
    captions = []
    for i, g in enumerate(groups):
        nxt = groups[i + 1][0].s if i + 1 < len(groups) else None
        end = g[-1].e + linger_ms
        if nxt is not None:
            end = min(end, nxt)
        captions.append(Caption(tuple(g), g[0].s, max(end, g[-1].e)))
    return captions
