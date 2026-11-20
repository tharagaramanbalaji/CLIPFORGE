"""Deterministic candidate checks (spec §7.4).
Invalid candidates are kept with their errors, never dropped."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.engine.highlights.schemas import Candidate, Category, LlmCandidate, Seg
from clipforge.engine.highlights.windowing import Window


@dataclass(frozen=True, slots=True)
class DurationBounds:
    min_ms: int = 12_000
    max_ms: int = 90_000
    target_ms: int = 45_000


def to_candidate(c: LlmCandidate, window_idx: int) -> Candidate:
    return Candidate(
        window_idx=window_idx,
        start_segment=c.start_segment,
        end_segment=c.end_segment,
        category=c.category,
        hook=c.hook,
        description=c.description,
        why_interesting=c.why_interesting,
        standalone_context=c.standalone_context,
        scores=c.scores.model_dump(),
        setup_segment=c.setup_segment,
    )


def validate_candidate(
    c: Candidate,
    window: Window,
    by_idx: dict[int, Seg],
    duration_ms: int,
    bounds: DurationBounds,
    peak_ms: Sequence[int] = (),
) -> Candidate:
    errors: list[str] = []
    if c.start_segment > c.end_segment:
        errors.append("start_segment after end_segment")
    lo, hi = window.first_idx, window.last_idx
    for name, idx in (("start_segment", c.start_segment), ("end_segment", c.end_segment)):
        if idx not in by_idx:
            errors.append(f"{name} s{idx} does not exist")
        elif not lo <= idx <= hi:
            errors.append(f"{name} s{idx} outside window s{lo}-s{hi}")
    if c.setup_segment is not None and c.setup_segment not in by_idx:
        c.setup_segment = None  # harmless hint; drop rather than reject
    if not errors:
        start, end = by_idx[c.start_segment], by_idx[c.end_segment]
        c.start_ms, c.end_ms = start.start_ms, end.end_ms
        if not 0 <= c.start_ms < c.end_ms <= duration_ms:
            errors.append("times outside media")
        if c.duration_ms < bounds.min_ms:
            errors.append(f"too short ({c.duration_ms / 1000:.1f}s < {bounds.min_ms / 1000:.0f}s)")
        elif c.duration_ms > bounds.max_ms:
            c.extra["needs_trim"] = True  # spec: mark for refinement, don't silently trim
        members = [by_idx[i] for i in range(c.start_segment, c.end_segment + 1) if i in by_idx]
        low = sum(s.end_ms - s.start_ms for s in members if s.low_confidence)
        total = sum(s.end_ms - s.start_ms for s in members) or 1
        backed = any(c.start_ms <= p <= c.end_ms for p in peak_ms)
        if low / total > 0.5 and not (c.category is Category.GAMING and backed):
            errors.append("mostly low-confidence speech")
    c.errors = errors
    c.status = "rejected_validation" if errors else "proposed"
    return c
