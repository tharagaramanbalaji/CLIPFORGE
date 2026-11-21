# ClipForge

**A multimodal AI pipeline that turns long-form video (streams, podcasts, gameplay) into ranked, captioned,
auto-framed vertical short clips.**

ClipForge combines speech recognition, speaker diarization, audio-event classification, scene detection,
computer-vision layout detection and LLM reasoning to find the moments worth clipping, cut them on word boundaries,
and render platform-ready 9:16 videos with word-level animated captions, all on a single consumer GPU (RTX 3060 12 GB).

```
 video ──► ASR + forced alignment ──► diarization ──► audio events / loudness / scenes
                (WhisperX)            (pyannote)        (AST · EBU R128 · PySceneDetect)
                                                │
                                                ▼
                    multimodal signal fusion + LLM highlight detection (2-pass)
                                                │
                                                ▼
          boundary refinement ──► CV auto-framing ──► caption synthesis ──► NVENC render
          (word/silence/scene)    (facecam detection)  (ASS karaoke)        (−14 LUFS)
                                                │
                                                ▼
                     human-in-the-loop review: trim, re-render, approve, export
```

## ML components

| Stage | Model / method | What it does |
|-------|----------------|--------------|
| Speech recognition | **WhisperX** `large-v3` (CTranslate2, int8_float16) + **wav2vec2** CTC forced alignment, pyannote VAD | Word-level transcript with per-word timestamps and confidence; hallucination flagging; calibrated word-start correction |
| Speaker diarization | **pyannote** `speaker-diarization-community-1` | Who spoke when; word → speaker assignment by maximum temporal overlap |
| Audio event detection | **Audio Spectrogram Transformer** (AST, fine-tuned on AudioSet) | Laughter, cheering/shouting/excitement and music probabilities over sliding 2 s windows |
| Acoustic signals | EBU R128 momentary loudness, RMS energy, speech rate | Time-series features that mark "something happened" (spikes, bursts) |
| Scene detection | PySceneDetect (content detector) | Hard cuts and keyframes on a 540p proxy |
| Highlight detection | **LLM** (Claude via Claude Code, or local **Ollama** models) behind a pluggable `HighlightAnalyzer` | Pass 1 discovers candidate moments per transcript window, fused with signal peaks; pass 2 re-scores, tightens and titles the top-K |
| Ranking | Weighted multi-criteria scoring (hook, payoff, standalone context, emotion, signal strength) with content-type profiles | Final ranked candidate list with rationale |
| Auto-framing | **Haar-cascade face clustering** + time-averaged Sobel gradient border analysis | Finds a streamer's facecam overlay, tracks when it's on screen, and switches between split-screen and full-frame layouts per clip |

### Design choices that matter for ML quality
- **Segment IDs, not timestamps.** The LLM references transcript segment IDs, which are resolved to exact times
  deterministically, so hallucinated timecodes can't happen ([ADR-0001](docs/adr/0001-segment-ids-not-timestamps.md)).
- **Signal fusion beats transcript-only.** Gameplay highlights are often non-verbal. Audio-event and loudness peaks
  are injected as candidate moments the LLM must judge, which moved real-video recall from 0/6 to 4/6.
- **Deterministic post-processing.** Boundaries snap to word edges, silence gaps and scene cuts, and extend through
  laughter/excitement peaks, so clips never start or end mid-word.
- **Evaluation first.** A labelled eval harness (precision@K, recall, boundary error, mid-word cuts, cost-estimate
  error) is used before any prompt tuning.
- **Zero per-token cost.** The LLM step runs through a subscription CLI bridge or a local model; no paid API by default
  ([ADR-0005](docs/adr/0005-no-paid-llm-api.md)).

## Results

| Metric | Result |
|--------|--------|
| Word-start timing (held-out sample) | median error **30 ms**, **97 %** within ±100 ms |
| Diarization (2-speaker fixture) | **100 %** word-level speaker agreement, all 12 turns found, 15× realtime |
| Highlight detection, synthetic 30-min stream (8 planted moments) | precision@10 **1.0**, recall **1.0** |
| Highlight detection, real gameplay video (6 hand-marked moments) | transcript only: 0/6 → with signal fusion: **precision 0.67, recall 4/6** |
| Clip boundaries | frame-accurate (±1 frame), A/V sync within 1 frame, −14 LUFS loudness |
| Facecam detection (real stream) | overlay box located exactly; per-frame presence used to switch layouts |

Full methodology and numbers per phase: [docs/phases/](docs/phases/).

## Product features
- Resumable multi-GB uploads with content-rights tracking per creator
- Highlight candidates with scores and model rationale
- **Auto-edit top N**: picks moments, frames them, captions them and renders in one click
- Animated word-level captions (3+ styles), hook-text overlay, watermark, platform safe areas
- Layouts: center crop, blurred letterbox, facecam split, two-speaker stack, auto / auto-switch
- Review dashboard: waveform + word-level trim editor, versioned re-renders, approve/reject with reasons, audit trail
- Exports: approved-clips ZIP (MP4 + SRT + CSV), Premiere Pro / DaVinci Resolve XML project

## Architecture

| Layer | Stack |
|-------|-------|
| API | FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, server-sent events |
| Orchestration | DB-driven pipeline with idempotent stages ([ADR-0003](docs/adr/0003-db-driven-pipeline.md)), Celery + Redis, separate GPU / render / CPU / LLM queues |
| Inference | PyTorch, CUDA 12.6, CTranslate2, Hugging Face Transformers; single-GPU queue with VRAM release between models ([ADR-0004](docs/adr/0004-single-gpu-queue.md)) |
| Media | FFmpeg (NVENC with x264 fallback, libass), two-pass loudness normalization |
| Frontend | React, TypeScript, Vite, TanStack Query, Tailwind |
| Runtime | Docker Compose on WSL2 with GPU passthrough ([ADR-0002](docs/adr/0002-linux-runtime-wsl2-docker.md)) |

```
backend/   FastAPI API, Celery workers, ML + media engine (Python 3.12)
  src/clipforge/engine/   asr · diarize · signals · scenes · highlights · render · subtitles · export
frontend/  React + Vite + TypeScript + Tailwind
evals/     highlight-detection eval harness and synthetic dataset generator
docs/      engineering spec, ADRs, phase reports with measured results, runbooks
```

## Run it
Requires an NVIDIA GPU, Docker Desktop with WSL2 GPU support ([runbook](docs/runbooks/wsl2-gpu-setup.md)).
```bash
cp .env.example .env                              # add a Hugging Face token for pyannote
docker compose up --build -d
python backend/scripts/llm_bridge.py              # LLM bridge on the host (Claude Code login), or configure Ollama
cd frontend && npm install && npm run dev         # http://localhost:5173
```
On first visit create the owner account, add a creator and rights grant, create a project, and upload a video.

## Tests
```bash
docker compose exec api pytest -m "not gpu and not slow"   # unit + integration + media (~165 tests)
docker compose exec worker-gpu pytest -m gpu -s tests/gpu  # real models: ASR timing, diarization accuracy
python evals/run_highlight_eval.py --help                  # highlight precision/recall eval
```
Lint and types: `ruff check src tests`, `mypy` (strict).

## Responsible use
For creators' own content or content used with permission. Every upload requires a recorded rights grant, and
expired or revoked rights block rendering.
