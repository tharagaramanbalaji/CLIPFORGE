"""Snap clip boundaries to word timings and silences (spec §7.7) so clips never start or end mid-word."""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BoundaryConfig:
    pre_roll_ms: int = 150
    post_roll_ms: int = 400
    silence_gap_ms: int = 250
    snap_search_ms: int = 1000
    scene_snap_ms: int = 500


@dataclass(frozen=True, slots=True)
class Boundaries:
    start_ms: int
    end_ms: int
    start_reason: str
    end_reason: str


def refine_boundaries(
    words: Sequence[tuple[int, int]],
    first_word: int,
    last_word: int,
    duration_ms: int,
    cfg: BoundaryConfig = BoundaryConfig(),
    cuts: Sequence[int] = (),
    extend_until_ms: int | None = None,
) -> Boundaries:
    """`words`: all (start, end) of the media, sorted; first/last_word: indexes of the clip's first/last word.
    `extend_until_ms`: a laughter/energy peak that continues after the last word (end may extend to it)."""
    ws = words[first_word][0]
    prev_end = words[first_word - 1][1] if first_word > 0 else 0
    next_start = words[last_word + 1][0] if last_word + 1 < len(words) else duration_ms
    le = words[last_word][1]

    # start: pre-roll, never into the previous word; prefer the middle of a silence gap just before
    start, s_reason = max(prev_end, ws - cfg.pre_roll_ms), "pre_roll"
    if ws - prev_end >= cfg.silence_gap_ms:
        start, s_reason = max(prev_end + (ws - prev_end) // 2, ws - cfg.snap_search_ms), "silence_gap"
    for c in cuts:  # a scene cut just before the first word makes a cleaner visual start
        if prev_end <= c <= ws and ws - c <= cfg.scene_snap_ms:
            start, s_reason = c, "scene_cut"
            break

    # end: post-roll, never into the next word; extend through a reaction peak; prefer a silence gap
    target = le + cfg.post_roll_ms
    e_reason = "post_roll"
    if extend_until_ms is not None and extend_until_ms > le:
        target, e_reason = min(extend_until_ms + cfg.post_roll_ms, le + 4000), "reaction_peak"
    end = min(target, next_start)
    if next_start - le >= cfg.silence_gap_ms and e_reason == "post_roll":
        end, e_reason = min(le + (next_start - le) // 2, le + cfg.snap_search_ms), "silence_gap"
    if end < le:
        end = le
    return Boundaries(max(0, start), min(duration_ms, end), s_reason, e_reason)


def word_index_range(word_starts: Sequence[int], start_ms: int, end_ms: int) -> tuple[int, int]:
    """Index of the first word starting at/after start_ms and the last word starting before end_ms."""
    first = bisect.bisect_left(word_starts, start_ms)
    last = bisect.bisect_left(word_starts, end_ms) - 1
    return first, max(first, last)


def cuts_mid_word(words: Sequence[tuple[int, int]], ms: int) -> bool:
    """True if `ms` falls strictly inside a word (the acceptance check for clip boundaries)."""
    starts = [w[0] for w in words]
    i = bisect.bisect_right(starts, ms) - 1
    return i >= 0 and words[i][0] < ms < words[i][1]
