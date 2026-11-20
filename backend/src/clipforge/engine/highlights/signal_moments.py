"""Signal-first candidates: moments found from audio/video signals instead of the transcript.

Gaming highlights (goals, near misses, clutch plays) often have almost no speech, so a transcript-only LLM
can't see them. Here we build candidates directly from:
- loud spikes and laughter/excitement peaks (Phase 3 signals),
- short reaction lines ("No way!", "What?", "Let's go!"),
- bursts of scene cuts (replays, celebrations),
cluster nearby events into one moment, and let the LLM only *judge* them (keep/reject, title) in one request.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from clipforge.engine.highlights.schemas import Candidate, Category, Seg
from clipforge.engine.highlights.windowing import Peak, fmt_ts

_REACTION = re.compile(
    r"\b(no way|what|let'?s go|oh my god|omg|oh no|no{1,3}|yes{1,3}|wow|unbelievable|insane|clip (it|that)|"
    r"goal|what a|are you (serious|kidding)|how|why|come on|wait)\b",
    re.IGNORECASE,
)

WEIGHTS = {"loud": 1.0, "excitement": 1.2, "laughter": 1.2, "reaction": 0.8, "cuts": 0.5}


@dataclass
class Event:
    ms: int
    kind: str
    weight: float
    detail: str


@dataclass
class SignalMoment:
    id: int
    start_ms: int
    end_ms: int
    events: list[Event] = field(default_factory=list)

    @property
    def strength(self) -> float:
        return round(sum(e.weight for e in self.events), 3)


def reaction_lines(segs: Sequence[Seg], max_words: int = 7) -> list[Event]:
    out = []
    for s in segs:
        words = s.text.split()
        if 0 < len(words) <= max_words and (_REACTION.search(s.text) or s.text.rstrip().endswith("!")):
            out.append(Event(s.start_ms, "reaction", WEIGHTS["reaction"], f'"{s.text.strip()[:60]}"'))
    return out


def cut_bursts(cuts: Sequence[int], window_ms: int = 5000, min_cuts: int = 4) -> list[Event]:
    cuts = sorted(cuts)
    out: list[Event] = []
    i = 0
    for j in range(len(cuts)):
        while cuts[j] - cuts[i] > window_ms:
            i += 1
        n = j - i + 1
        if n >= min_cuts and (not out or cuts[i] - out[-1].ms > window_ms):
            out.append(Event(cuts[i], "cuts", WEIGHTS["cuts"], f"{n} scene cuts in {window_ms // 1000}s"))
    return out


def peak_events(peaks: Sequence[Peak]) -> list[Event]:
    return [Event(p.ms, p.kind, WEIGHTS.get(p.kind, 0.5), f"{p.kind} {p.value:g}") for p in peaks]


def find_signal_moments(
    segs: Sequence[Seg],
    peaks: Sequence[Peak],
    cuts: Sequence[int],
    duration_ms: int,
    *,
    gap_ms: int = 8000,
    pre_ms: int = 9000,
    post_ms: int = 6000,
    min_ms: int = 6000,
    max_ms: int = 40_000,
    min_strength: float = 1.0,
    limit: int = 20,
) -> list[SignalMoment]:
    """Cluster events closer than `gap_ms`; a moment spans [first - pre, last + post] (the build-up before a
    spike matters more than the aftermath). Weak clusters (only one reaction line, say) are dropped."""
    events = sorted(peak_events(peaks) + reaction_lines(segs) + cut_bursts(cuts), key=lambda e: e.ms)
    clusters: list[list[Event]] = []
    for e in events:
        if clusters and e.ms - clusters[-1][-1].ms <= gap_ms:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    moments = []
    for group in clusters:
        start = max(0, group[0].ms - pre_ms)
        end = min(duration_ms, group[-1].ms + post_ms)
        if end - start > max_ms:  # very long busy stretch: center on the strongest event
            best = max(group, key=lambda e: e.weight)
            start, end = max(0, best.ms - max_ms // 2), min(duration_ms, best.ms + max_ms // 2)
        if end - start < min_ms:
            end = min(duration_ms, start + min_ms)
        m = SignalMoment(0, start, end, group)
        if m.strength >= min_strength and any(e.kind != "cuts" for e in group):
            moments.append(m)
    moments = sorted(moments, key=lambda m: m.strength, reverse=True)[:limit]
    moments.sort(key=lambda m: m.start_ms)
    for i, m in enumerate(moments):
        m.id = i
    return moments


def render_moments(moments: Sequence[SignalMoment], segs: Sequence[Seg], context_ms: int = 15_000) -> str:
    blocks = []
    for m in moments:
        sig = "; ".join(f"{e.detail} at {fmt_ts(e.ms)}" for e in m.events)
        lines = [
            f"  [s{s.idx} | {fmt_ts(s.start_ms)} | {s.speaker or '?'}] {s.text}"
            for s in segs
            if s.end_ms > m.start_ms - context_ms and s.start_ms < m.end_ms + context_ms
        ]
        speech = "\n".join(lines) if lines else "  (no speech)"
        blocks.append(
            f"[m{m.id} | {fmt_ts(m.start_ms)}-{fmt_ts(m.end_ms)} | strength {m.strength}]\n"
            f"  signals: {sig}\n  speech around it:\n{speech}"
        )
    return "\n\n".join(blocks)


def nearest_segment(segs: Sequence[Seg], ms: int) -> int:
    if not segs:
        return -1
    return min(
        segs,
        key=lambda s: 0 if s.start_ms <= ms <= s.end_ms else min(abs(s.start_ms - ms), abs(s.end_ms - ms)),
    ).idx


def to_candidates(moments: Sequence[SignalMoment], segs: Sequence[Seg]) -> list[Candidate]:
    out = []
    for m in moments:
        c = Candidate(
            window_idx=-1,
            start_segment=nearest_segment(segs, m.start_ms),
            end_segment=nearest_segment(segs, m.end_ms),
            category=Category.GAMING,
            hook="",
            description="",
            why_interesting="",
            standalone_context="self_contained",
            scores={"hook": 5, "emotion": 5, "standalone": 5, "story": 3, "shareability": 5},
            start_ms=m.start_ms,
            end_ms=m.end_ms,
        )
        c.extra.update(
            {
                "source": "signal",
                "signal_id": m.id,
                "strength": m.strength,
                "not_refined": True,
                "signals": [f"{e.detail} at {fmt_ts(e.ms)}" for e in m.events],
            }
        )
        out.append(c)
    return out


def avoid_mid_word(
    words: Sequence[tuple[int, int]], start: int, end: int, pad_ms: int = 150
) -> tuple[int, int]:
    """Signal moments aren't built from words; move edges that land inside a word to the word's edge."""
    for ws, we in words:
        if ws < start < we:
            start = max(0, ws - pad_ms)
        if ws < end < we:
            end = we + pad_ms
    return start, end
