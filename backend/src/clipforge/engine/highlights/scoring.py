"""Final score and ranking (spec §7.8). Scores are heuristics; approvals/rejections calibrate them later."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from clipforge.engine.highlights.dedupe import iou
from clipforge.engine.highlights.schemas import DIMENSIONS, Candidate


@dataclass(frozen=True, slots=True)
class ScoringProfile:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "hook": 0.25,
            "emotion": 0.2,
            "standalone": 0.25,
            "story": 0.1,
            "shareability": 0.2,
        }
    )
    signal_bonus: float = 0.05  # added when a laughter/excitement peak falls inside the clip
    prior_context_penalty: float = 0.15
    judge_rejected_penalty: float = 0.15  # kept for its signal strength, but the LLM judge said no
    light_setup_penalty: float = 0.03
    overlap_penalty: float = 0.3  # per IoU with an already-selected clip

    def __post_init__(self) -> None:
        if set(self.weights) != set(DIMENSIONS):
            raise ValueError(f"weights must cover exactly {DIMENSIONS}")
        if abs(sum(self.weights.values()) - 1.0) > 1e-6:
            raise ValueError("weights must sum to 1.0")


def base_score(c: Candidate, profile: ScoringProfile, peak_ms: Sequence[int] = ()) -> float:
    s = sum(profile.weights[d] * c.scores.get(d, 0) / 10 for d in DIMENSIONS)
    if c.standalone_context == "needs_prior_context":
        s -= profile.prior_context_penalty
    elif c.standalone_context == "needs_light_setup":
        s -= profile.light_setup_penalty
    if c.extra.get("judge_rejected"):
        s -= profile.judge_rejected_penalty
    if any(c.start_ms <= p <= c.end_ms for p in peak_ms):
        s += profile.signal_bonus
    return round(max(0.0, min(1.0, s)), 4)


def rank(
    cands: Sequence[Candidate], profile: ScoringProfile, peak_ms: Sequence[int] = (), top_k: int | None = None
) -> list[Candidate]:
    """Greedy selection: best remaining score after penalizing overlap with already-selected clips,
    and a small diversity nudge so one category doesn't fill the whole top list."""
    pool = [c for c in cands if c.status in ("proposed", "refined")]
    for c in pool:
        c.extra["base_score"] = base_score(c, profile, peak_ms)
    selected: list[Candidate] = []
    while pool and (top_k is None or len(selected) < top_k):

        def adjusted(c: Candidate) -> float:
            overlap = max((iou(c, s) for s in selected), default=0.0)
            same_cat = sum(1 for s in selected if s.category == c.category)
            return float(c.extra["base_score"]) - profile.overlap_penalty * overlap - 0.02 * same_cat

        best = max(pool, key=adjusted)
        best.final_score = round(adjusted(best), 4)
        best.rank = len(selected) + 1
        selected.append(best)
        pool.remove(best)
    return selected
