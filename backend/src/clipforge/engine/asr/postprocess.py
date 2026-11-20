"""Turn raw aligned ASR output (seconds, gaps, missing timings) into clean, monotonic millisecond segments.

Pure functions: no torch/whisperx imports, fully unit-tested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from clipforge.engine.asr.base import Segment, Word

MIN_WORD_MS = 20
_SPACE = re.compile(r"\s+")


def _ms(value: Any) -> int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f * 1000) if f == f else None  # NaN check


def fill_word_times(
    raw_words: Sequence[Mapping[str, Any]], seg_start: int, seg_end: int
) -> tuple[list[Word], bool]:
    """Interpolate words the aligner could not place (numbers, symbols) between their timed neighbours."""
    texts = [str(w.get("word", "")).strip() for w in raw_words]
    raw_starts = [_ms(w.get("start")) for w in raw_words]
    raw_ends = [_ms(w.get("end")) for w in raw_words]
    timed = [s is not None and e is not None for s, e in zip(raw_starts, raw_ends, strict=True)]
    starts = [s if s is not None else 0 for s in raw_starts]
    ends = [e if e is not None else 0 for e in raw_ends]
    scores: list[Any] = [w.get("score") for w in raw_words]
    interpolated = False
    n = len(raw_words)
    i = 0
    while i < n:
        if timed[i]:
            i += 1
            continue
        j = i
        while j < n and not timed[j]:
            j += 1
        left = ends[i - 1] if i > 0 else seg_start
        right = starts[j] if j < n else seg_end
        right = max(right, left + MIN_WORD_MS * (j - i))
        step = (right - left) / (j - i)
        for k in range(i, j):
            starts[k] = round(left + step * (k - i))
            ends[k] = round(left + step * (k - i + 1))
            scores[k] = None
            timed[k] = True
        interpolated = True
        i = j
    words: list[Word] = []
    prev_end = seg_start
    for t, s, e, p in zip(texts, starts, ends, scores, strict=True):
        if not t:
            continue
        s = max(s, prev_end) if words else s
        e = max(e, s + MIN_WORD_MS)
        words.append(Word(w=t, s=s, e=e, p=round(float(p), 3) if isinstance(p, int | float) else None))
        prev_end = e
    return words, interpolated


def is_repetitive(words: list[Word], min_words: int = 8, max_unique_ratio: float = 0.25) -> bool:
    """Whisper's classic hallucination on music/silence: one short phrase repeated many times."""
    if len(words) < min_words:
        return False
    tokens = [re.sub(r"\W", "", w.w.lower()) for w in words]
    tokens = [t for t in tokens if t]
    return bool(tokens) and len(set(tokens)) / len(tokens) <= max_unique_ratio


def lead_word_starts(words: list[Word], lead_ms: int, floor_ms: int) -> list[Word]:
    """Move word starts earlier by `lead_ms` without overlapping the previous word (or `floor_ms`).

    CTC (wav2vec2) forced alignment marks word onsets late; on our ground-truth set the bias was +59 ms
    while word ends were accurate. Cutting a clip at a late onset clips the first consonant, so starts
    are corrected here. Measured by tests/gpu/test_asr_accuracy.py and scripts/align_probe.py.
    """
    if lead_ms <= 0:
        return words
    out: list[Word] = []
    prev_end = floor_ms
    for w in words:
        s = max(prev_end, w.s - lead_ms, 0)
        s = min(s, w.s)  # never move a start later
        out.append(Word(w.w, s, w.e, w.p))
        prev_end = w.e
    return out


def build_segments(raw_segments: Iterable[Mapping[str, Any]], start_lead_ms: int = 0) -> list[Segment]:
    out: list[Segment] = []
    last_end = 0
    for raw in raw_segments:
        text = _SPACE.sub(" ", str(raw.get("text", ""))).strip()
        seg_start = _ms(raw.get("start"))
        seg_end = _ms(raw.get("end"))
        if not text or seg_start is None or seg_end is None:
            continue
        raw_words = list(raw.get("words") or [])
        if raw_words:
            words, interpolated = fill_word_times(raw_words, seg_start, max(seg_end, seg_start + MIN_WORD_MS))
        else:  # alignment disabled/failed: spread words evenly so downstream code always has word times
            fake = [{"word": t} for t in text.split(" ")]
            words, interpolated = fill_word_times(fake, seg_start, max(seg_end, seg_start + MIN_WORD_MS))
        if not words:
            continue
        # Monotonic across segments (aligners occasionally overlap boundaries by a few ms)
        if words[0].s < last_end:
            shift_floor = last_end
            words = [
                Word(w.w, max(w.s, shift_floor), max(w.e, shift_floor + MIN_WORD_MS), w.p) for w in words
            ]
        words = lead_word_starts(words, start_lead_ms, last_end)
        start, end = words[0].s, words[-1].e
        scored = [w.p for w in words if w.p is not None]
        flags: list[str] = []
        if interpolated:
            flags.append("interpolated_words")
        if is_repetitive(words):
            flags.append("repetitive")
        out.append(
            Segment(
                idx=len(out),
                start_ms=start,
                end_ms=end,
                text=" ".join(w.w for w in words),
                words=tuple(words),
                confidence=round(sum(scored) / len(scored), 3) if scored else None,
                flags=tuple(flags),
            )
        )
        last_end = end
    return out
