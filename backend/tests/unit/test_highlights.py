from __future__ import annotations

import json

import pytest

from clipforge.engine.highlights.boundaries import BoundaryConfig, cuts_mid_word, refine_boundaries
from clipforge.engine.highlights.dedupe import dedupe
from clipforge.engine.highlights.llm import FakeClient, LlmOutputInvalid, call_structured, extract_json_object
from clipforge.engine.highlights.run import (
    HighlightConfig,
    Inputs,
    discover,
    estimate,
    finalize,
    refine,
    shortlist,
)
from clipforge.engine.highlights.schemas import Candidate, Category, Refinement, Seg, WindowResult
from clipforge.engine.highlights.scoring import ScoringProfile, base_score, rank
from clipforge.engine.highlights.validate import DurationBounds, validate_candidate
from clipforge.engine.highlights.windowing import Peak, Window, find_peaks, make_windows, render_window


def seg(i: int, start: int, dur: int = 3000, words: int = 6, speaker: str = "S1", low: bool = False) -> Seg:
    step = dur // words
    return Seg(
        i,
        start,
        start + dur,
        f"line {i}",
        speaker,
        tuple((start + k * step, start + k * step + int(step * 0.8)) for k in range(words)),
        low,
    )


def transcript(n: int = 400, gap: int = 500) -> list[Seg]:
    return [seg(i, i * (3000 + gap)) for i in range(n)]


def cand(
    s: int,
    e: int,
    start_ms: int = 0,
    end_ms: int = 0,
    cat: Category = Category.FUNNY,
    score: int = 7,
    ctx: str = "self_contained",
) -> Candidate:
    return Candidate(
        0,
        s,
        e,
        cat,
        "hook",
        "desc",
        "why",
        ctx,
        {d: score for d in ("hook", "emotion", "standalone", "story", "shareability")},
        start_ms=start_ms,
        end_ms=end_ms,
    )


# --- windowing ----------------------------------------------------------------------------------------


def test_windows_overlap_and_cover_every_segment() -> None:
    segs = transcript()  # 400 * 3.5 s = 23.3 min
    ws = make_windows(segs, window_ms=10 * 60_000, overlap_ms=2 * 60_000)
    assert [w.start_ms for w in ws] == [0, 480_000, 960_000]
    covered = {s.idx for w in ws for s in w.segs}
    assert covered == set(range(400))
    assert ws[0].last_idx >= ws[1].first_idx  # overlap


def test_render_uses_ids_marks_low_confidence_and_signals() -> None:
    w = Window(0, 0, 10_000, (seg(7, 0), seg(8, 4000, low=True)))
    text = render_window(w, [Peak(3500, "laughter", 0.91)], cuts=[9000], speaker_names={"S1": "Host"})
    lines = text.splitlines()
    assert lines[0].startswith("[s7 | 0:00:00.0-0:00:03.0 | Host] line 7")
    assert lines[1] == "~ [laughter 0.91 at 0:00:03.5]"
    assert lines[2].startswith("[s8? |")
    assert lines[3] == "~ [scene cut 0:00:09.0]"


def test_find_peaks_keeps_strongest_with_min_gap() -> None:
    values = [0.0, 0.9, 0.2, 0.95, 0.1, 0.0, 0.7, 0.0]
    peaks = find_peaks(values, 1000, "laughter", threshold=0.5, min_gap_ms=2500)
    assert [(p.ms, p.value) for p in peaks] == [(3000, 0.95), (6000, 0.7)]


# --- validation & dedupe ------------------------------------------------------------------------------


def test_validation_resolves_times_and_rejects_bad_ids() -> None:
    segs = transcript(40)
    by = {s.idx: s for s in segs}
    w = Window(0, 0, 60_000, tuple(segs[:17]))
    ok = validate_candidate(cand(2, 8), w, by, 999_999, DurationBounds())
    assert ok.status == "proposed" and (ok.start_ms, ok.end_ms) == (7000, 31000)
    outside = validate_candidate(cand(10, 30), w, by, 999_999, DurationBounds())
    assert outside.status == "rejected_validation" and "outside window" in outside.errors[0]
    missing = validate_candidate(cand(2, 900), w, by, 999_999, DurationBounds())
    assert any("does not exist" in e for e in missing.errors)
    short = validate_candidate(cand(3, 4), w, by, 999_999, DurationBounds())
    assert any("too short" in e for e in short.errors)
    backwards = validate_candidate(cand(8, 2), w, by, 999_999, DurationBounds())
    assert "start_segment after end_segment" in backwards.errors
    long = validate_candidate(cand(0, 16), w, by, 999_999, DurationBounds(max_ms=30_000))
    assert long.status == "proposed" and long.extra["needs_trim"]


def test_mostly_low_confidence_rejected_unless_gaming_with_peak() -> None:
    segs = [seg(i, i * 3500, low=True) for i in range(10)]
    by = {s.idx: s for s in segs}
    w = Window(0, 0, 40_000, tuple(segs))
    assert validate_candidate(cand(1, 6), w, by, 99_999, DurationBounds()).status == "rejected_validation"
    g = validate_candidate(cand(1, 6, cat=Category.GAMING), w, by, 99_999, DurationBounds(), peak_ms=[10_000])
    assert g.status == "proposed"


def test_dedupe_merges_overlapping_window_duplicates() -> None:
    a = cand(10, 20, 35_000, 70_000, score=8)
    b = cand(12, 21, 42_000, 73_500, score=6)  # IoU > 0.5 -> merged into a (union bounds)
    c = cand(40, 45, 140_000, 158_000)
    out = dedupe([a, b, c], max_ms=90_000)
    assert out == [a, c] and b.status == "merged"
    assert (a.start_ms, a.end_ms, a.end_segment) == (35_000, 73_500, 21)


# --- boundaries ---------------------------------------------------------------------------------------

WORDS = [(0, 400), (450, 900), (2000, 2400), (2450, 2900), (2950, 3300), (5000, 5400)]


def test_boundaries_never_cut_into_words() -> None:
    b = refine_boundaries(WORDS, 2, 4, 10_000)
    assert b.start_reason == "silence_gap" and 900 < b.start_ms < 2000  # inside the 1.1 s gap
    assert b.end_reason == "silence_gap" and 3300 < b.end_ms < 5000
    for ms in (b.start_ms, b.end_ms):
        assert not cuts_mid_word(WORDS, ms)


def test_tight_gaps_use_pre_and_post_roll_without_overlap() -> None:
    b = refine_boundaries(WORDS, 3, 3, 10_000, BoundaryConfig(pre_roll_ms=150, post_roll_ms=400))
    assert b.start_ms == 2400 and b.end_ms == 2950  # clamped to neighbouring words, not into them


def test_scene_cut_and_reaction_peak() -> None:
    b = refine_boundaries(WORDS, 2, 4, 10_000, cuts=[1700], extend_until_ms=4200)
    assert (b.start_ms, b.start_reason) == (1700, "scene_cut")
    assert b.end_reason == "reaction_peak" and b.end_ms == 4600


def test_cuts_mid_word() -> None:
    assert cuts_mid_word(WORDS, 200) and not cuts_mid_word(WORDS, 420) and not cuts_mid_word(WORDS, 0)


# --- scoring ------------------------------------------------------------------------------------------


def test_profile_weights_validated() -> None:
    with pytest.raises(ValueError):
        ScoringProfile(weights={"hook": 1.0})
    with pytest.raises(ValueError):
        ScoringProfile(
            weights={"hook": 0.5, "emotion": 0.5, "standalone": 0.5, "story": 0, "shareability": 0}
        )


def test_penalties_bonus_and_overlap_aware_ranking() -> None:
    p = ScoringProfile()
    base = cand(0, 1, 0, 30_000, score=8)
    assert base_score(base, p) == 0.8
    assert base_score(cand(0, 1, 0, 30_000, score=8, ctx="needs_prior_context"), p) == 0.65
    assert base_score(base, p, peak_ms=[10_000]) == 0.85
    a = cand(0, 5, 0, 30_000, score=9)
    dup = cand(0, 5, 2_000, 31_000, score=9, cat=Category.STORY)  # nearly the same clip
    other = cand(9, 12, 100_000, 130_000, score=7)
    ranked = rank([a, dup, other], p)
    assert [c.rank for c in (a, other, dup)] == [1, 2, 3]  # overlap penalty pushes the duplicate down
    assert ranked[0] is a


# --- LLM plumbing -------------------------------------------------------------------------------------


def test_extract_json_tolerates_fences_and_prose() -> None:
    assert extract_json_object('```json\n{"candidates": []}\n```') == {"candidates": []}
    assert extract_json_object('Sure! Here it is: {"a": 1} hope that helps') == {"a": 1}


def test_call_structured_reasks_once_then_fails() -> None:
    answers = iter(["not json", '{"candidates": []}'])
    client = FakeClient(lambda s, u: next(answers))
    result, responses = call_structured(client, "sys", "user", WindowResult)
    assert result.candidates == [] and len(responses) == 2
    assert "not valid" in client.calls[1][1] and responses[0].raw["validation_error"]
    with pytest.raises(LlmOutputInvalid):
        call_structured(FakeClient(lambda s, u: "no json here"), "sys", "user", WindowResult)


def test_long_text_is_truncated_not_rejected() -> None:
    r = Refinement.model_validate(
        {
            "keep": True,
            "start_segment": 1,
            "end_segment": 2,
            "standalone_context": "self_contained",
            "scores": {"hook": 1, "emotion": 1, "standalone": 1, "story": 1, "shareability": 1},
            "title": "t" * 300,
            "hook_text": "h",
            "description": "d",
            "rationale": "r" * 5000,
        }
    )
    assert len(r.title) == 100 and r.title.endswith("…") and len(r.rationale) == 600


def _fake_llm(system: str, user: str) -> str:
    if "Candidate as proposed" in user:  # pass 2: keep, tighten by one segment at the start
        first = int(user.split("segments s")[1].split("-")[0])
        last = int(user.split("segments s")[1].split("-s")[1].split(" ")[0])
        return json.dumps(
            {
                "keep": True,
                "start_segment": first + 1,
                "end_segment": last,
                "standalone_context": "self_contained",
                "scores": {"hook": 8, "emotion": 7, "standalone": 9, "story": 7, "shareability": 8},
                "title": "Great moment",
                "hook_text": "Wait for it",
                "description": "d",
                "rationale": "tightened",
            }
        )
    ids = [int(line[2:].split(" ")[0].rstrip("?")) for line in user.splitlines() if line.startswith("[s")]
    lo = ids[len(ids) // 2]
    return json.dumps(
        {
            "candidates": [
                {
                    "start_segment": lo,
                    "end_segment": lo + 8,
                    "category": "story",
                    "hook": "h",
                    "description": "d",
                    "why_interesting": "w",
                    "standalone_context": "self_contained",
                    "setup_segment": None,
                    "scores": {"hook": 7, "emotion": 6, "standalone": 8, "story": 7, "shareability": 6},
                }
            ]
        }
    )


def test_full_flow_with_fake_llm_and_estimate() -> None:
    segs = transcript(600)  # 35 min
    inp = Inputs(segs=segs, duration_ms=segs[-1].end_ms + 1000)
    cfg = HighlightConfig(window_ms=15 * 60_000)
    client = FakeClient(_fake_llm)
    calls: list[str] = []
    cands = discover(inp, cfg, client, lambda p, v, w, r, req: calls.append(p))
    assert len(cands) == 3 and all(c.status == "proposed" for c in cands)
    survivors = shortlist(cands, cfg)
    refine(inp, survivors, cfg, client, lambda p, v, w, r, req: calls.append(p))
    ranked = finalize(inp, cands, cfg, ScoringProfile())
    assert calls == ["discover"] * 3 + ["refine"] * 3
    assert [c.rank for c in ranked] == [1, 2, 3] and all(c.title == "Great moment" for c in ranked)
    for c in ranked:
        assert not cuts_mid_word(inp.words, c.start_ms) and not cuts_mid_word(inp.words, c.end_ms)
        assert 12_000 <= c.duration_ms <= 95_000
    est = estimate(inp, cfg)
    assert est["discover_requests"] == 3 and est["refine_requests"] == 9 and est["input_tokens"] > 0


# --- signal-first moments (gaming) ----------------------------------------------------------------------


def test_signal_moments_cluster_events_and_skip_weak_ones() -> None:
    from clipforge.engine.highlights.signal_moments import find_signal_moments

    segs = [
        Seg(0, 30_000, 30_800, "No way!", "S1", ((30_000, 30_400), (30_450, 30_800))),
        Seg(1, 90_000, 92_000, "okay heading north now", "S1", ((90_000, 92_000),)),
        Seg(2, 150_000, 150_500, "What?", "S1", ((150_000, 150_500),)),
    ]
    peaks = [Peak(31_000, "loud", -4.0)]
    cuts = [200_000, 201_000, 202_000, 203_000]  # a burst on its own is not enough
    moments = find_signal_moments(segs, peaks, cuts, 300_000)
    assert [(m.start_ms, m.end_ms) for m in moments] == [(21_000, 37_000)]  # reaction + loud merged
    assert moments[0].strength == 1.8  # lone "What?" (0.8) is below the minimum strength


def test_gaming_keeps_strong_moment_the_judge_rejected() -> None:
    from clipforge.engine.highlights.run import discover_signals, for_content

    segs = [Seg(0, 30_000, 30_800, "No way!", "S1", ((30_000, 30_400),))]
    inp = Inputs(segs=segs, duration_ms=120_000, peaks=[Peak(31_000, "loud", -4.0)])
    verdict = json.dumps(
        {
            "moments": [
                {
                    "id": 0,
                    "keep": False,
                    "category": "gaming",
                    "title": "t",
                    "scores": {d: 4 for d in ("hook", "emotion", "standalone", "story", "shareability")},
                }
            ]
        }
    )
    gaming = discover_signals(inp, for_content("gaming"), FakeClient(lambda s, u: verdict), lambda *a: None)
    talk = discover_signals(inp, for_content("talk"), FakeClient(lambda s, u: verdict), lambda *a: None)
    assert gaming[0].status == "proposed" and gaming[0].extra["judge_rejected"]
    assert talk[0].status == "discarded"
    ranked = finalize(inp, gaming, for_content("gaming"), ScoringProfile())
    assert (
        ranked
        and not cuts_mid_word(inp.words, ranked[0].start_ms)
        and ranked[0].extra["start_reason"] == "signal"
    )
