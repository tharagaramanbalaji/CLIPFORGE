"""Highlight pipeline logic (pure; DB persistence lives in pipeline/stages_highlights.py).

discover (pass 1, per window) -> validate -> dedupe -> refine top-N (pass 2) -> boundaries -> rank
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from clipforge.engine.highlights import prompts
from clipforge.engine.highlights.boundaries import BoundaryConfig, refine_boundaries
from clipforge.engine.highlights.dedupe import dedupe, pass1_score
from clipforge.engine.highlights.llm import (
    PER_CALL_OVERHEAD_TOKENS,
    LlmClient,
    LlmResponse,
    call_structured,
    estimate_tokens,
)
from clipforge.engine.highlights.schemas import Candidate, JudgeResult, Refinement, Seg, WindowResult
from clipforge.engine.highlights.scoring import ScoringProfile, rank
from clipforge.engine.highlights.signal_moments import (
    avoid_mid_word,
    find_signal_moments,
    render_moments,
    to_candidates,
)
from clipforge.engine.highlights.validate import DurationBounds, to_candidate, validate_candidate
from clipforge.engine.highlights.windowing import (
    Peak,
    Window,
    fmt_ts,
    make_windows,
    render_window,
    worth_analyzing,
)

# (purpose, prompt_version, window_idx or None, response) -> None; used to persist llm_calls
CallLogger = Callable[[str, str, int | None, LlmResponse, str], None]


@dataclass(frozen=True, slots=True)
class HighlightConfig:
    window_ms: int = 15 * 60_000
    overlap_ms: int = 2 * 60_000
    bounds: DurationBounds = field(default_factory=DurationBounds)
    refine_top_n: int = 10
    top_k: int = 10
    context_ms: int = 90_000
    max_output_tokens: int = 4000
    content_type: str = "talk"  # talk | gaming (gaming: shorter clips, signal moments matter most)
    signal_moments: bool = True
    max_signal_moments: int = 20
    version: int = 2

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Inputs:
    segs: list[Seg]
    duration_ms: int
    peaks: list[Peak] = field(default_factory=list)
    cuts: list[int] = field(default_factory=list)
    speaker_names: dict[str, str] = field(default_factory=dict)
    creator_context: str = ""

    def __post_init__(self) -> None:
        self.by_idx = {s.idx: s for s in self.segs}
        self.words: list[tuple[int, int]] = [w for s in self.segs for w in s.words]
        self.word_starts = [w[0] for w in self.words]
        self.reaction_peaks = [p.ms for p in self.peaks if p.kind in ("laughter", "excitement")]


def _header(inp: Inputs) -> str:
    ctx = f"Creator context: {inp.creator_context}\n" if inp.creator_context else ""
    return f"{ctx}Video length: {fmt_ts(inp.duration_ms)}\n"


def plan_windows(inp: Inputs, cfg: HighlightConfig) -> tuple[list[Window], list[Window]]:
    windows = make_windows(inp.segs, cfg.window_ms, cfg.overlap_ms)
    kept = [w for w in windows if worth_analyzing(w, inp.peaks)]
    return windows, kept


def estimate(inp: Inputs, cfg: HighlightConfig) -> dict[str, Any]:
    """Dry-run estimate of requests and tokens (no LLM calls). Validated against actual usage in evals."""
    windows, kept = plan_windows(inp, cfg)
    system = prompts.load(prompts.DISCOVER_VERSION)
    texts = [render_window(w, inp.peaks, inp.cuts, inp.speaker_names) for w in kept]
    discover_in = sum(estimate_tokens([system, _header(inp), t]) + PER_CALL_OVERHEAD_TOKENS for t in texts)
    # pass 2: expected candidates ~3 per analyzed window, capped by top-N; each sends ~(2*context + clip) of
    # rendered transcript, measured from this media's own rendered density
    refine_n = min(cfg.refine_top_n, 3 * len(kept))
    full = Window(0, 0, inp.duration_ms, tuple(inp.segs)) if inp.segs else None
    density = (
        estimate_tokens([render_window(full, inp.peaks, inp.cuts, inp.speaker_names)])
        / max(1, inp.duration_ms)
        if full
        else 0.0
    )
    ctx_tokens = int(density * (2 * cfg.context_ms + 40_000))
    refine_in = refine_n * (
        estimate_tokens([prompts.load(prompts.REFINE_VERSION), _header(inp)])
        + ctx_tokens
        + 250
        + PER_CALL_OVERHEAD_TOKENS
    )
    moments = (
        find_signal_moments(
            inp.segs,
            inp.peaks,
            inp.cuts,
            inp.duration_ms,
            min_ms=cfg.bounds.min_ms,
            limit=cfg.max_signal_moments,
        )
        if cfg.signal_moments
        else []
    )
    judge_n = 1 if moments else 0
    judge_text = [prompts.load(prompts.JUDGE_VERSION), _header(inp), render_moments(moments, inp.segs)]
    judge_in = estimate_tokens(judge_text) + PER_CALL_OVERHEAD_TOKENS if moments else 0
    return {
        "windows_total": len(windows),
        "windows_analyzed": len(kept),
        "requests": len(kept) + refine_n + judge_n,
        "discover_requests": len(kept),
        "refine_requests": refine_n,
        "judge_requests": judge_n,
        "signal_moments": len(moments),
        "input_tokens": discover_in + refine_in + judge_in,
        "output_tokens": len(kept) * 900 + refine_n * 380 + len(moments) * 90,
    }


STRONG_SIGNAL = 1.5  # e.g. a loud spike plus a reaction line or a burst of scene cuts


def for_content(content_type: str, **kw: Any) -> HighlightConfig:
    """Gaming moments are short (a goal + reaction is often 6-20 s)."""
    if content_type == "gaming":
        return HighlightConfig(content_type="gaming", bounds=DurationBounds(min_ms=6000, max_ms=60_000), **kw)
    return HighlightConfig(content_type=content_type, **kw)


def discover_signals(
    inp: Inputs, cfg: HighlightConfig, client: LlmClient, log_call: CallLogger
) -> list[Candidate]:
    """Signal-first candidates + one LLM request to judge them all (keep/reject, title, scores)."""
    if not cfg.signal_moments:
        return []
    moments = find_signal_moments(
        inp.segs,
        inp.peaks,
        inp.cuts,
        inp.duration_ms,
        min_ms=cfg.bounds.min_ms,
        max_ms=min(40_000, cfg.bounds.max_ms),
        limit=cfg.max_signal_moments,
    )
    if not moments:
        return []
    cands = to_candidates(moments, inp.segs)
    system = prompts.load(prompts.JUDGE_VERSION)
    user = _header(inp) + f"Content type: {cfg.content_type}\n\n" + render_moments(moments, inp.segs)
    result, responses = call_structured(client, system, user, JudgeResult, max_tokens=cfg.max_output_tokens)
    for r in responses:
        log_call("judge", prompts.JUDGE_VERSION, None, r, user)
    verdicts = {j.id: j for j in result.moments}
    for c in cands:
        j = verdicts.get(int(c.extra["signal_id"]))
        if j is None:
            c.status, c.errors = "rejected_validation", ["not judged"]
            continue
        c.category, c.title, c.hook_text, c.hook = j.category, j.title or None, j.hook_text or None, j.title
        c.scores, c.description, c.extra["judge_reason"] = j.scores.model_dump(), j.reason, j.reason
        if j.keep:
            c.status = "proposed"
        elif cfg.content_type == "gaming" and float(c.extra["strength"]) >= STRONG_SIGNAL:
            # The judge can't see the video; a strong audio/cut signal outweighs a blind "reject".
            # Keep it, ranked lower, instead of deleting it (measured on the owner's EA FC eval).
            c.status, c.extra["judge_rejected"] = "proposed", True
        else:
            c.status = "discarded"
    return cands


def discover(inp: Inputs, cfg: HighlightConfig, client: LlmClient, log_call: CallLogger) -> list[Candidate]:
    system = prompts.load(prompts.DISCOVER_VERSION)
    _, kept = plan_windows(inp, cfg)
    all_cands: list[Candidate] = []
    for w in kept:
        user = (
            _header(inp)
            + f"Window {w.idx}: segments s{w.first_idx}-s{w.last_idx}\n\n"
            + render_window(w, inp.peaks, inp.cuts, inp.speaker_names)
        )
        result, responses = call_structured(
            client, system, user, WindowResult, max_tokens=cfg.max_output_tokens
        )
        for r in responses:
            log_call("discover", prompts.DISCOVER_VERSION, w.idx, r, user)
        for c in result.candidates:
            all_cands.append(
                validate_candidate(
                    to_candidate(c, w.idx), w, inp.by_idx, inp.duration_ms, cfg.bounds, inp.reaction_peaks
                )
            )
    return all_cands


def shortlist(cands: Sequence[Candidate], cfg: HighlightConfig) -> list[Candidate]:
    survivors = dedupe(cands, cfg.bounds.max_ms)
    survivors.sort(key=pass1_score, reverse=True)
    for c in survivors[cfg.refine_top_n :]:
        c.extra["not_refined"] = True
    return survivors


def _context_window(inp: Inputs, c: Candidate, cfg: HighlightConfig) -> Window:
    lo, hi = c.start_ms - cfg.context_ms, c.end_ms + cfg.context_ms
    segs = tuple(s for s in inp.segs if s.end_ms > lo and s.start_ms < hi)
    return Window(c.window_idx, max(0, lo), min(inp.duration_ms, hi), segs)


def refine(
    inp: Inputs, cands: Sequence[Candidate], cfg: HighlightConfig, client: LlmClient, log_call: CallLogger
) -> None:
    system = prompts.load(prompts.REFINE_VERSION)
    for c in cands:
        if c.extra.get("not_refined"):
            continue
        ctx = _context_window(inp, c, cfg)
        user = (
            _header(inp)
            + "Candidate as proposed:\n"
            + f"- segments s{c.start_segment}-s{c.end_segment} ({fmt_ts(c.start_ms)}-{fmt_ts(c.end_ms)}), "
            f"category {c.category.value}\n- hook: {c.hook}\n- description: {c.description}\n"
            f"- why: {c.why_interesting}\n- pass-1 scores: {c.scores}\n\nTranscript around it:\n"
            + render_window(ctx, inp.peaks, inp.cuts, inp.speaker_names)
        )
        r, responses = call_structured(client, system, user, Refinement, max_tokens=1500)
        for resp in responses:
            log_call("refine", prompts.REFINE_VERSION, c.window_idx, resp, user)
        if not r.keep:
            c.status, c.extra["refine_rationale"] = "discarded", r.rationale
            continue
        ok_ids = {s.idx for s in ctx.segs}
        if r.start_segment in ok_ids and r.end_segment in ok_ids and r.start_segment <= r.end_segment:
            new_start, new_end = inp.by_idx[r.start_segment].start_ms, inp.by_idx[r.end_segment].end_ms
            if cfg.bounds.min_ms <= new_end - new_start <= cfg.bounds.max_ms:
                c.start_segment, c.end_segment, c.start_ms, c.end_ms = (
                    r.start_segment,
                    r.end_segment,
                    new_start,
                    new_end,
                )
            else:
                c.extra["refine_bounds_rejected"] = f"{(new_end - new_start) / 1000:.1f}s"
        else:
            c.extra["refine_bounds_rejected"] = "segment ids outside context"
        c.scores, c.standalone_context = r.scores.model_dump(), r.standalone_context
        c.title, c.hook_text, c.description = r.title, r.hook_text, r.description
        c.extra["refine_rationale"] = r.rationale
        c.status = "refined"


def finalize(
    inp: Inputs,
    cands: Sequence[Candidate],
    cfg: HighlightConfig,
    profile: ScoringProfile,
    bcfg: BoundaryConfig | None = None,
) -> list[Candidate]:
    """Snap to words/silences/scene cuts, drop anything still over/under length, rank."""
    bcfg = bcfg or BoundaryConfig()
    for c in cands:
        if c.status == "proposed" and c.extra.get("needs_trim"):
            c.status, c.errors = "rejected_validation", [*c.errors, "too long and not refined"]
            continue
        if c.status not in ("proposed", "refined"):
            continue
        if c.extra.get("source") == "signal":
            c.start_ms, c.end_ms = avoid_mid_word(inp.words, c.start_ms, c.end_ms)
            c.extra.update({"start_reason": "signal", "end_reason": "signal"})
            if not cfg.bounds.min_ms <= c.duration_ms <= cfg.bounds.max_ms + 5000:
                c.status, c.errors = (
                    "rejected_validation",
                    [*c.errors, f"final length {c.duration_ms / 1000:.1f}s"],
                )
            continue
        first = bisect.bisect_left(inp.word_starts, c.start_ms)
        last = bisect.bisect_left(inp.word_starts, c.end_ms) - 1
        if first >= len(inp.words) or last < first:
            c.status, c.errors = "rejected_validation", [*c.errors, "no words in range"]
            continue
        peak_after = next(
            (p for p in inp.reaction_peaks if inp.words[last][1] <= p <= inp.words[last][1] + 4000), None
        )
        b = refine_boundaries(inp.words, first, last, inp.duration_ms, bcfg, inp.cuts, peak_after)
        c.start_ms, c.end_ms = b.start_ms, b.end_ms
        c.extra.update({"start_reason": b.start_reason, "end_reason": b.end_reason})
        if not cfg.bounds.min_ms <= c.duration_ms <= cfg.bounds.max_ms + 5000:
            c.status, c.errors = (
                "rejected_validation",
                [*c.errors, f"final length {c.duration_ms / 1000:.1f}s"],
            )
    return rank(cands, profile, inp.reaction_peaks, top_k=None)
