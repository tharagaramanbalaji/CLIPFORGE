# Phase 4: Highlight detection

**Status:** Built. Real baseline on the owner's EA FC video: **4/6 moments found, 4/6 picks correct** after the
signal-first fix (was 0/6). Ollama fallback blocked by an outdated Ollama install.

## What was built
| Area | Files |
|------|-------|
| Windowing | `engine/highlights/windowing.py`: 15 min windows / 2 min overlap aligned to segments; compact ID-referenced lines `[s412 \| 1:02:13.4-1:02:17.9 \| Host] text`, `?` for low-confidence ASR, `~` lines for laughter/excitement/loud peaks and scene cuts; near-silent windows skipped |
| Prompts | `engine/highlights/prompts/discover_v1.md`, `refine_v1.md` (versioned; version stored on every LLM call) |
| Schemas | `engine/highlights/schemas.py`: Pydantic models for pass 1/2 output; over-long text is truncated, not rejected |
| Validation | `validate.py`: IDs exist and lie in the window, start ≤ end, 12–90 s, not mostly low-confidence (unless gaming + signal peak), times inside the media; invalid candidates are kept with their errors |
| Dedupe | `dedupe.py`: IoU ≥ 0.5 or containment with the same category → merged (union bounds if still ≤ 90 s) |
| Pass 2 | `run.refine`: top-10 only, ±90 s context; model may tighten bounds (re-validated), downgrade, discard; writes title / on-screen hook / description |
| Boundaries | `boundaries.py`: pre-roll 150 ms / post-roll 400 ms, never into a neighbouring word, prefer silence gaps, scene cut within 0.5 s, extend through a laughter/excitement peak |
| Scoring | `scoring.py`: weighted dimensions (weights validated to sum to 1), penalties for needing prior context, bonus for signal peaks, overlap- and category-aware greedy ranking |
| LLM backends | `llm.py`: `BridgeClient` (Claude Code via host bridge), `OllamaClient`, `FakeClient`; JSON extraction + Pydantic validation + one re-ask |
| Host bridge | `scripts/llm_bridge.py` (stdlib only, runs on Windows): calls `claude.exe -p` with the plan login, tools and MCP off, empty working folder, max 2 at once, shared-secret header, bound to 127.0.0.1; refuses to start if `ANTHROPIC_API_KEY` is set; plan-limit errors → HTTP 429 → retry later |
| Schema (Alembic `0005`) | `llm_calls` (purpose, backend, model, prompt version, tokens, latency, request text in storage, response), `highlight_candidates` (segment IDs, resolved ms, category, texts, scores, final score, rank, status, validation errors) |
| Pipeline | New run kind `highlights`: highlight_windows (llm) → highlight_refine (llm) → rank (cpu). The run config pins transcript, backend, models and prompt versions, so a repeat run with the same settings reuses everything (no new LLM calls) |
| API | `POST /media/{id}/highlights?dry_run=true` (estimate, no calls), `POST /media/{id}/highlights` (start), `GET /media/{id}/candidates` (`include_rejected`), `GET /media/{id}/llm-usage` (requests/tokens per media hour) |
| Eval | `evals/run_highlight_eval.py` (precision@K, recall, boundary error, mid-word cuts, ID validity, usage, estimate error) + `evals/make_synthetic_stream.py` → `evals/datasets/synthetic_stream_v1.json` (30 min, 8 planted moments) |

## Acceptance criteria and results
| Criterion | Result |
|-----------|--------|
| All candidate times resolve from segment IDs and pass validation | ✅ Claude run: 10/10 candidates valid. Invalid IDs (tests) are kept as `rejected_validation` with the reason, e.g. "end_segment s99 does not exist" |
| No clip starts/ends mid-word on the eval set | ✅ 0 mid-word cuts (also asserted in unit tests for every ranked clip) |
| precision@10 baseline recorded | ✅ **placeholder only**: synthetic set, Claude Sonnet (`claude-sonnet-5-5`): precision@10 **1.0**, recall **1.0** (8/8), median boundary error start 0.48 s / end 0.27 s (pre/post-roll and silence snapping). ⏳ Real baseline needs owner-labeled videos |
| Cost per hour of video recorded | ✅ 30 min synthetic: 11 requests, 61k input + 5.6k output tokens, 98 s → **~22 requests and ~130k tokens per media hour**. Plan usage, not billed per token (Claude Code reports a notional figure only) |
| Dry-run estimate within ±30 % of actual | ✅ −23.7 % on input tokens (first version was −76 %; recalibrated for Claude Code's ~930-token per-call overhead and dense ID/timestamp lines) |
| Tests / lint / types | ✅ 127 passed (after the gaming fix) (incl. 16 highlight unit tests and an end-to-end API test with a fake model); ruff clean; mypy strict clean (82 files) |

## Real baseline: EA FC 25 video, 7:41, 6 moments marked by the owner (2026-11-20)
| Metric | Result |
|--------|--------|
| precision@10 / recall | **0.0 / 0.0**: 3 candidates, 2 discarded in refinement, 1 clip (0:32–1:07, a spoken anecdote), not a marked moment |
| Usage | 4 requests, 17k input tokens, 38 s; estimate within −28 % |
| Mid-word cuts / invalid IDs | 0 / 0 |

Why: the marked moments are **gameplay** (goals, near misses) with almost no speech: 323 words in 7:41;
3:49–3:56 has none, others only "No way!" / "No strike!" or in-game commentary. The transcript-only LLM can't see
them. The audio *did*: the loudest moment of the video (1:39) is inside moment 1, and loud spikes at 3:34 and 4:08
are inside moments 2 and 4. A no-AI "loudest spikes" picker would score 3/7 precision, 3/6 recall. Marked
moments are 7–18 s, shorter than the 12 s minimum.

**Fix built: signal-first moments** (`engine/highlights/signal_moments.py`, prompt `judge_signals_v1.md`):
loud/laughter/excitement peaks, short reaction lines ("No way!", "What?") and scene-cut bursts are clustered into
moments (build-up 9 s before, 6 s after); one Claude request judges all of them (keep, category, title, scores).
`content_type=gaming` (API `?content_type=gaming`, `CLIPFORGE_HIGHLIGHT_CONTENT_TYPE`) allows 6–60 s clips and
keeps strong-signal moments (strength ≥ 1.5) the judge rejected, ranked lower, because the judge can't see video.

| Run (same video, Claude Sonnet) | precision@10 | recall | requests | estimate error |
|---|---|---|---|---|
| transcript only | 0.00 | 0/6 | 4 | −28 % |
| + signal moments, judge decides | 1.00 (2/2) | 2/6 | 5 | −30 % |
| + gaming mode (strong signals kept) | **0.67 (4/6)** | **4/6** | 3 | +10 % |

Clip edges vs marks: median 2.6 s early / 5.6 s late (includes build-up; marks were ±10 s).
The 2 misses (3:49–3:56, 4:48–5:02) have no audio/cut signal at all; only a vision pass could find them.
Not re-checked after this change: the synthetic talk eval (talk mode still discards judge-rejected moments).

Dataset: `evals/datasets/eafc25_seasons1.json` (git-ignored: derived from a third-party video).

## Findings and decisions
- **Re-ask doubled usage.** In the first Claude run every refine answer failed validation once (text over the length
  limits), so each was asked twice (19 requests). Text is now truncated instead: 11 requests, same results.
- **Windows `.cmd` shim.** `claude` from npm is a `.cmd` file; arguments to it are re-parsed by cmd.exe, which
  would mangle prompts containing `|`, `&`, `%` or newlines. The bridge calls the real `claude.exe` directly.
- **Ollama on this PC crashes** (`CUDA error: a PTX JIT compilation failed`): the installed Ollama is older than
  the NVIDIA 610 driver. Updating Ollama should fix it; the free-fallback comparison runs after that.
- **Scores live on the candidate row** (JSONB) instead of a separate `candidate_scores` table; one scoring profile
  per run for now. Vision pass (keyframes) not built; off by default per ADR-0005.
- **Synthetic eval is easy:** planted moments stand out from repetitive filler. It proves IDs, validation,
  boundaries, ranking and usage accounting, not taste. Use the real baseline to tune prompts/models.

## How to run
```bash
python backend/scripts/llm_bridge.py          # on Windows, keep running (uses your Claude plan login)
docker compose exec api pytest -m "not gpu and not slow"
MSYS_NO_PATHCONV=1 docker compose exec api python /evals/run_highlight_eval.py /evals/datasets/synthetic_stream_v1.json --backend claude_code
```
Real eval set: put `{name, duration_ms, segments, labels:[{start_ms,end_ms,category}]}` files in
`evals/datasets/` (export from a processed video + your marked moments) and run the same command.
