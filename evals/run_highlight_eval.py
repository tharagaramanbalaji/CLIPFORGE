"""Highlight evaluation harness (spec §7.8). Runs the real highlight logic on a labeled transcript and scores it.

    docker compose exec api python /evals/run_highlight_eval.py evals/datasets/synthetic_stream_v1.json \
        --backend claude_code            # or: --backend ollama --model qwen3:8b

Dataset JSON: {name, duration_ms, creator_context, segments:[{idx,start_ms,end_ms,speaker,text,words:[{w,s,e}]}],
               labels:[{start_ms,end_ms,category}], peaks:[{ms,kind,value}], cuts:[ms]}
Metrics:
- precision@K: share of the top-K clips that overlap a labeled moment (>= 50 % of the clip or of the moment)
- recall@K: share of labeled moments covered by a top-K clip
- boundary error: |start|/|end| difference (ms) between matched clips and labels (median)
- mid-word cuts: clips whose start or end falls inside a word (must be 0)
- id validity: candidates whose segment IDs failed validation
- usage: requests / tokens / latency, and dry-run estimate vs actual input tokens (target within +-30 %)
Writes evals/results/<dataset>__<backend>__<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from clipforge.config import get_settings  # noqa: E402
from clipforge.engine.highlights.boundaries import cuts_mid_word  # noqa: E402
from clipforge.engine.highlights.llm import BridgeClient, LlmClient, LlmResponse, OllamaClient  # noqa: E402
from clipforge.engine.highlights.run import (  # noqa: E402
    Inputs,
    discover,
    discover_signals,
    estimate,
    finalize,
    for_content,
    refine,
    shortlist,
)
from clipforge.engine.highlights.schemas import Candidate, Seg  # noqa: E402
from clipforge.engine.highlights.scoring import ScoringProfile  # noqa: E402
from clipforge.engine.highlights.windowing import Peak  # noqa: E402


def load_inputs(data: dict[str, Any]) -> Inputs:
    segs = [Seg(idx=s["idx"], start_ms=s["start_ms"], end_ms=s["end_ms"], text=s["text"], speaker=s.get("speaker"),
                words=tuple((w["s"], w["e"]) for w in s["words"])) for s in data["segments"]]
    peaks = [Peak(p["ms"], p["kind"], p["value"]) for p in data.get("peaks", [])]
    return Inputs(segs=segs, duration_ms=data["duration_ms"], peaks=peaks, cuts=list(data.get("cuts", [])),
                  creator_context=data.get("creator_context", ""))


def overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def matches(c: Candidate, label: dict[str, Any]) -> bool:
    o = overlap(c.start_ms, c.end_ms, label["start_ms"], label["end_ms"])
    return o >= 0.5 * (c.end_ms - c.start_ms) or o >= 0.5 * (label["end_ms"] - label["start_ms"])


def score(ranked: list[Candidate], all_cands: list[Candidate], inp: Inputs, labels: list[dict[str, Any]],
          k: int) -> dict[str, Any]:
    top = ranked[:k]
    hits = [c for c in top if any(matches(c, lab) for lab in labels)]
    covered = [lab for lab in labels if any(matches(c, lab) for c in top)]
    start_err, end_err = [], []
    for c in hits:
        lab = max(labels, key=lambda lb: overlap(c.start_ms, c.end_ms, lb["start_ms"], lb["end_ms"]))
        start_err.append(abs(c.start_ms - lab["start_ms"]))
        end_err.append(abs(c.end_ms - lab["end_ms"]))
    mid_word = [c for c in ranked if cuts_mid_word(inp.words, c.start_ms) or cuts_mid_word(inp.words, c.end_ms)]
    return {
        "k": k, "precision_at_k": round(len(hits) / max(1, len(top)), 3), "recall_at_k": round(len(covered) / max(1, len(labels)), 3),
        "returned": len(top), "labels": len(labels),
        "median_start_error_ms": int(statistics.median(start_err)) if start_err else None,
        "median_end_error_ms": int(statistics.median(end_err)) if end_err else None,
        "mid_word_cuts": len(mid_word),
        "candidates_total": len(all_cands),
        "rejected_validation": sum(c.status == "rejected_validation" for c in all_cands),
        "validation_errors": sorted({e for c in all_cands for e in c.errors}),
        "durations_s": [round(c.duration_ms / 1000, 1) for c in top],
    }


def make_client(backend: str, model: str) -> LlmClient:
    s = get_settings()
    if backend == "claude_code":
        if s.llm_bridge_token is None:
            raise SystemExit("CLIPFORGE_LLM_BRIDGE_TOKEN not set")
        return BridgeClient(s.llm_bridge_url, s.llm_bridge_token.get_secret_value(), model or s.llm_discover_model)
    return OllamaClient(s.ollama_url, model or s.ollama_model)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--backend", choices=["claude_code", "ollama"], default="claude_code")
    ap.add_argument("--model", default="")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--window-min", type=int, default=15)
    ap.add_argument("--content", choices=["talk", "gaming"], default="talk")
    ap.add_argument("--no-signals", action="store_true", help="transcript-only (the old behaviour)")
    args = ap.parse_args()

    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    inp = load_inputs(data)
    cfg = for_content(args.content, window_ms=args.window_min * 60_000, signal_moments=not args.no_signals)
    est = estimate(inp, cfg)
    client = make_client(args.backend, args.model)
    calls: list[dict[str, Any]] = []

    def log(purpose: str, version: str, window_idx: int | None, r: LlmResponse, request: str) -> None:
        calls.append({"purpose": purpose, "prompt": version, "window": window_idx, "model": r.model,
                      "input_tokens": r.input_tokens + r.cache_read_tokens + r.cache_write_tokens,
                      "output_tokens": r.output_tokens, "latency_ms": r.latency_ms, "cost_usd": r.cost_usd})
        bad = r.raw.get("validation_error")
        calls[-1]["validation_error"] = bad
        print(f"  {purpose} window={window_idx} {r.latency_ms / 1000:.1f}s in={calls[-1]['input_tokens']} "
              f"out={r.output_tokens}" + (f" INVALID: {bad[:160]!r}" if bad else ""), flush=True)

    t0 = time.monotonic()
    cands = discover(inp, cfg, client, log) + discover_signals(inp, cfg, client, log)
    survivors = shortlist(cands, cfg)
    refine(inp, survivors, cfg, client, log)
    ranked = finalize(inp, cands, cfg, ScoringProfile())
    wall = time.monotonic() - t0

    actual_in = sum(c["input_tokens"] for c in calls)
    hours = inp.duration_ms / 3.6e6
    report = {
        "dataset": data.get("name"), "backend": args.backend, "model": calls[0]["model"] if calls else args.model,
        "media_minutes": round(inp.duration_ms / 60000, 1), "wall_s": round(wall, 1),
        "metrics": score(ranked, cands, inp, data["labels"], args.k),
        "usage": {"requests": len(calls), "input_tokens": actual_in,
                  "output_tokens": sum(c["output_tokens"] for c in calls),
                  "requests_per_media_hour": round(len(calls) / hours, 1),
                  "tokens_per_media_hour": round((actual_in + sum(c["output_tokens"] for c in calls)) / hours),
                  "notional_cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 4)},
        "estimate": {**est, "input_tokens_error_pct": round(100 * (est["input_tokens"] - actual_in) / max(1, actual_in), 1),
                     "requests_error": est["requests"] - len(calls)},
        "content": args.content, "top": [{"rank": c.rank, "source": c.extra.get("source", "transcript"), "score": c.final_score, "start": round(c.start_ms / 1000, 1),
                 "end": round(c.end_ms / 1000, 1), "category": c.category.value, "title": c.title or c.hook,
                 "hit": any(matches(c, lab) for lab in data["labels"])} for c in ranked[: args.k]],
        "calls": calls,
    }
    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{data.get('name')}__{args.backend}__{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    m = report["metrics"]
    print(f"\nRESULT {args.backend}/{report['model']}: precision@{args.k}={m['precision_at_k']} "
          f"recall@{args.k}={m['recall_at_k']} mid_word_cuts={m['mid_word_cuts']} "
          f"rejected={m['rejected_validation']}/{m['candidates_total']} start_err={m['median_start_error_ms']}ms "
          f"end_err={m['median_end_error_ms']}ms | requests={len(calls)} in_tokens={actual_in} "
          f"estimate_err={report['estimate']['input_tokens_error_pct']}% | {wall:.0f}s -> {out.name}")


if __name__ == "__main__":
    main()
