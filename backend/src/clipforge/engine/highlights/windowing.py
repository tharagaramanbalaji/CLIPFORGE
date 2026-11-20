"""Split a transcript into overlapping windows aligned to segment boundaries and render them as compact,
ID-referenced text for the LLM (spec §7.2, ADR-0005: ~15 min windows to keep request counts low)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.engine.highlights.schemas import Seg


@dataclass(frozen=True, slots=True)
class Peak:
    ms: int
    kind: str  # laughter | excitement | loud
    value: float


@dataclass(frozen=True, slots=True)
class Window:
    idx: int
    start_ms: int
    end_ms: int
    segs: tuple[Seg, ...]

    @property
    def first_idx(self) -> int:
        return self.segs[0].idx

    @property
    def last_idx(self) -> int:
        return self.segs[-1].idx

    @property
    def speech_ms(self) -> int:
        return sum(s.end_ms - s.start_ms for s in self.segs)


def make_windows(
    segs: Sequence[Seg], window_ms: int = 15 * 60_000, overlap_ms: int = 2 * 60_000
) -> list[Window]:
    """A segment belongs to every window whose [start, end) contains the segment's start."""
    if not segs:
        return []
    assert window_ms > overlap_ms >= 0
    windows: list[Window] = []
    end_of_media = segs[-1].end_ms
    start = 0
    while start < end_of_media:
        end = start + window_ms
        members = tuple(s for s in segs if start <= s.start_ms < end)
        if members:
            windows.append(Window(len(windows), start, min(end, end_of_media), members))
        if end >= end_of_media:
            break
        start = end - overlap_ms
    return windows


def fmt_ts(ms: int) -> str:
    s, rem = divmod(max(0, ms), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}.{rem // 100}"


def render_window(
    window: Window,
    peaks: Sequence[Peak] = (),
    cuts: Sequence[int] = (),
    speaker_names: dict[str, str] | None = None,
) -> str:
    """One line per segment: `[s0412 | 1:02:13.4-1:02:17.9 | Host] text`, with signal markers between lines.
    `?` marks low-confidence segments; the model is told to discount them."""
    names = speaker_names or {}
    events = sorted(
        [
            (p.ms, f"~ [{p.kind} {p.value:.2f} at {fmt_ts(p.ms)}]")
            for p in peaks
            if window.start_ms <= p.ms < window.end_ms
        ]
        + [(c, f"~ [scene cut {fmt_ts(c)}]") for c in cuts if window.start_ms <= c < window.end_ms]
    )
    lines: list[str] = []
    ei = 0
    for seg in window.segs:
        while ei < len(events) and events[ei][0] < seg.start_ms:
            lines.append(events[ei][1])
            ei += 1
        spk = names.get(seg.speaker or "", seg.speaker or "?")
        mark = "?" if seg.low_confidence else ""
        lines.append(f"[s{seg.idx}{mark} | {fmt_ts(seg.start_ms)}-{fmt_ts(seg.end_ms)} | {spk}] {seg.text}")
    lines.extend(e[1] for e in events[ei:])
    return "\n".join(lines)


def worth_analyzing(window: Window, peaks: Sequence[Peak], min_speech_ratio: float = 0.08) -> bool:
    """Cheap pre-filter (ADR-0005): skip near-silent windows unless a laughter/excitement peak is in them."""
    span = max(1, window.end_ms - window.start_ms)
    has_peak = any(
        window.start_ms <= p.ms < window.end_ms and p.kind in ("laughter", "excitement") for p in peaks
    )
    return window.speech_ms / span >= min_speech_ratio or has_peak


def find_peaks(
    values: Sequence[float],
    hop_ms: int,
    kind: str,
    threshold: float,
    min_gap_ms: int = 10_000,
    limit: int = 400,
) -> list[Peak]:
    """Local maxima above `threshold`, at least `min_gap_ms` apart (strongest kept), capped at `limit`."""
    cand = [
        (v, i)
        for i, v in enumerate(values)
        if v >= threshold and (i == 0 or v >= values[i - 1]) and (i == len(values) - 1 or v >= values[i + 1])
    ]
    chosen: list[tuple[float, int]] = []
    for v, i in sorted(cand, reverse=True):
        if all(abs(i - j) * hop_ms >= min_gap_ms for _, j in chosen):
            chosen.append((v, i))
        if len(chosen) >= limit:
            break
    return sorted((Peak(i * hop_ms, kind, round(v, 2)) for v, i in chosen), key=lambda p: p.ms)
