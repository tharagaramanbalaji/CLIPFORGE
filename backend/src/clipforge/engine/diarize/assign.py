"""Assign diarization speaker turns to words and segments. Pure functions (unit-tested)."""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker: str


def normalize_labels(turns: Sequence[SpeakerTurn]) -> list[SpeakerTurn]:
    """Rename backend labels (SPEAKER_03, ...) to S1, S2, ... in order of first appearance."""
    mapping: dict[str, str] = {}
    out = []
    for t in sorted(turns, key=lambda t: (t.start_ms, t.end_ms)):
        if t.end_ms <= t.start_ms:
            continue
        label = mapping.setdefault(t.speaker, f"S{len(mapping) + 1}")
        out.append(SpeakerTurn(t.start_ms, t.end_ms, label))
    return out


def speaker_for_span(
    turns: Sequence[SpeakerTurn], starts: list[int], s: int, e: int, max_gap_ms: int = 400
) -> str | None:
    """Speaker with the most overlap with [s, e); if none overlaps, the nearest turn within max_gap_ms."""
    overlap: dict[str, int] = defaultdict(int)
    i = max(0, bisect.bisect_right(starts, s) - 1)
    # turns may be long: step back while earlier turns could still overlap
    while i > 0 and turns[i - 1].end_ms > s:
        i -= 1
    j = i
    while j < len(turns) and turns[j].start_ms < e:
        o = min(e, turns[j].end_ms) - max(s, turns[j].start_ms)
        if o > 0:
            overlap[turns[j].speaker] += o
        j += 1
    if overlap:
        return max(overlap.items(), key=lambda kv: (kv[1], kv[0]))[0]
    best: tuple[int, str] | None = None
    for k in range(max(0, i - 1), min(len(turns), j + 1)):
        t = turns[k]
        gap = t.start_ms - e if t.start_ms >= e else s - t.end_ms if t.end_ms <= s else 0
        if 0 <= gap <= max_gap_ms and (best is None or gap < best[0]):
            best = (gap, t.speaker)
    return best[1] if best else None


@dataclass(frozen=True, slots=True)
class Assignment:
    segment_speakers: list[str | None]
    word_speakers: list[list[str | None]]
    totals_ms: dict[str, int]  # speech time per speaker (from turns)
    word_coverage: float  # share of words that got a speaker


def assign_speakers(segments: Sequence[dict[str, Any]], turns: Sequence[SpeakerTurn]) -> Assignment:
    """`segments`: [{"words": [{"s": ms, "e": ms, ...}, ...]}, ...] (transcript_segments rows)."""
    turns = sorted(turns, key=lambda t: t.start_ms)
    starts = [t.start_ms for t in turns]
    seg_speakers: list[str | None] = []
    word_speakers: list[list[str | None]] = []
    labelled = total = 0
    for seg in segments:
        spk_words: list[str | None] = []
        weight: Counter[str] = Counter()
        for w in seg.get("words") or []:
            spk = speaker_for_span(turns, starts, int(w["s"]), max(int(w["e"]), int(w["s"]) + 1))
            spk_words.append(spk)
            total += 1
            if spk is not None:
                labelled += 1
                weight[spk] += int(w["e"]) - int(w["s"])
        word_speakers.append(spk_words)
        seg_speakers.append(weight.most_common(1)[0][0] if weight else None)
    totals: dict[str, int] = defaultdict(int)
    for t in turns:
        totals[t.speaker] += t.end_ms - t.start_ms
    return Assignment(seg_speakers, word_speakers, dict(totals), labelled / total if total else 0.0)


def best_label_agreement(truth: Sequence[str], predicted: Sequence[str | None]) -> float:
    """Word-level agreement after mapping predicted labels to truth labels (greedy by co-occurrence).
    Used by accuracy tests: diarization labels are arbitrary, only the grouping matters."""
    pairs = Counter((p, t) for t, p in zip(truth, predicted, strict=True) if p is not None)
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for (p, t), _ in pairs.most_common():
        if p not in mapping and t not in used:
            mapping[p] = t
            used.add(t)
    hits = sum(1 for t, p in zip(truth, predicted, strict=True) if p is not None and mapping.get(p) == t)
    return hits / len(truth) if truth else 0.0
