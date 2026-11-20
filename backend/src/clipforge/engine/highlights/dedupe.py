"""Merge duplicates from overlapping windows (spec §7.5). Pure interval logic."""

from __future__ import annotations

from collections.abc import Sequence

from clipforge.engine.highlights.schemas import DIMENSIONS, Candidate


def iou(a: Candidate, b: Candidate) -> float:
    inter = max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))
    union = max(a.end_ms, b.end_ms) - min(a.start_ms, b.start_ms)
    return inter / union if union > 0 else 0.0


def contains(a: Candidate, b: Candidate) -> bool:
    return a.start_ms <= b.start_ms and b.end_ms <= a.end_ms


def pass1_score(c: Candidate) -> float:
    return sum(c.scores.get(d, 0) for d in DIMENSIONS) / len(DIMENSIONS)


def dedupe(cands: Sequence[Candidate], max_ms: int, iou_threshold: float = 0.5) -> list[Candidate]:
    """Returns survivors; merged-away candidates get status 'merged' (and stay in the input list)."""
    alive = sorted((c for c in cands if c.status == "proposed"), key=pass1_score, reverse=True)
    survivors: list[Candidate] = []
    for c in alive:
        for s in survivors:
            same_cat = s.category == c.category
            if iou(s, c) >= iou_threshold or ((contains(s, c) or contains(c, s)) and same_cat):
                start, end = min(s.start_ms, c.start_ms), max(s.end_ms, c.end_ms)
                if end - start <= max_ms:  # union bounds if still a valid length, else keep the higher-scored
                    if c.start_ms < s.start_ms:
                        s.start_ms, s.start_segment = c.start_ms, c.start_segment
                    if c.end_ms > s.end_ms:
                        s.end_ms, s.end_segment = c.end_ms, c.end_segment
                c.status = "merged"
                s.merged_from.append(c.start_segment)
                break
        else:
            survivors.append(c)
    return survivors
