# Phase 2: Audio & transcription

**Status:** Complete. Acceptance results below (2026-11-19/20, RTX 3060 12 GB, inside the Docker stack).

## Scope
Audio extraction (16 kHz mono), WhisperX transcription + forced alignment, VAD, segment/word persistence,
model/device configuration with CPU fallback, VRAM release, benchmarks, transcript API.

## What was built
| Area | Files |
|------|-------|
| Audio extraction | `engine/media/audio.py`: 16 kHz mono PCM WAV from the mezzanine (same timeline as everything else) |
| ASR interface | `engine/asr/base.py`: `Transcriber` protocol, `AsrConfig`, `Word`/`Segment`/`TranscriptResult` (integer ms) |
| WhisperX backend | `engine/asr/whisperx_backend.py`: faster-whisper large-v3 (int8_float16, batch 16) with pyannote VAD, then wav2vec2 alignment. Each model is loaded inside `gpu_model_scope` and always freed. On CUDA OOM the batch size halves and it retries. |
| Transcript cleanup | `engine/asr/postprocess.py`: seconds → ms, interpolates words the aligner can't place (numbers, symbols), keeps times monotonic, flags repetitive "hallucination" segments, corrects word starts (below) |
| GPU memory | `engine/common/gpu_memory.py`: device memory before / peak / after, release on exit (also on errors) |
| Config | `CLIPFORGE_ASR_MODEL`, `_ASR_COMPUTE_TYPE`, `_ASR_BATCH_SIZE`, `_ASR_VAD_METHOD`, `_DEVICE` (auto/cuda/cpu), CPU fallback `_ASR_CPU_MODEL` (medium.en), `_ASR_CPU_COMPUTE_TYPE`, `_ASR_CPU_BATCH_SIZE`. `pipeline/asr_config.py` |
| Schema (Alembic `0003`) | `transcripts` (engine, model, language, config hash, `is_current`, metrics), `transcript_segments` (`idx` = the ID the LLM will reference, start/end ms, text, words JSONB `[{w,s,e,p}]`, confidence, flags) |
| Pipeline | DAG is now probe → mezzanine → {proxy, audio_extract → transcribe}. Proxy and audio extraction run in parallel. `transcribe` runs on the `gpu` queue. |
| API | `GET /api/v1/media/{id}/transcript?from_ms&to_ms&include_words`: segments overlapping the range |
| Image | Dependencies installed before the source is copied, so code edits don't reinstall torch. Models are cached in the `clipforge-models` volume. The GPU worker recycles its process every 10 tasks as a VRAM backstop. |
| Tools | `scripts/bench_asr.py` (long-audio benchmark), `scripts/align_probe.py` (signed timing errors per alignment model) |

## Acceptance criteria and results
| Criterion | Result |
|-----------|--------|
| Word timestamps within ±100 ms on a checked sample | ✅ **Held-out sample** (never used for tuning): 61/61 words recognized; start error median **30 ms**, p90 83 ms; **97 %** of starts within ±100 ms; end error median 17 ms. Calibration sample: 73/76 words, median 23 ms, 93 % within ±100 ms. (`tests/gpu/test_asr_accuracy.py`, threshold ≥ 90 %) |
| 1 h audio without OOM on the RTX 3060 | ✅ 60 min of speech: 10,739 words, 1,007 segments in ~2 min (**28× realtime**). Peak device memory 9.5 of 12 GB (includes ~1 GB used by Windows/desktop). Memory after release was back to the starting level (+54 MiB of persistent CUDA context); alignment model fully released (0 MiB). |
| CPU fallback works (slow) | ✅ medium.en int8 on CPU: 1.9× realtime on a 39 s clip, same timing accuracy (93 % within ±100 ms). Used automatically when no GPU is visible; recorded as `cpu_fallback` in stage metrics. |
| Transcript API returns ranges | ✅ integration tests: overlap semantics, `include_words=false`, 422 on inverted range, 404 before transcription. Real stack: the 0:30–0:45 range returned the matching sentences. |
| Real pipeline on the GPU worker | ✅ 2-min 1080p60 VFR clip with speech: all 5 stages ready in 42 s; transcribe on CUDA (large-v3) at 6.8× realtime including model load; VRAM back to baseline. Re-run on 2026-11-20: ready in 48 s, 360 words, VRAM peak 4.8 GB → back to baseline. An interrupted 1.17 GB upload resumed and went through all 5 stages; its sha256 matched; re-analysis skipped every stage. |
| Re-analysis | ✅ unchanged settings → transcript reused (stage skipped). Changing `CLIPFORGE_ASR_MODEL` re-runs only `transcribe`; the old transcript stays but is no longer current. |
| Tests / lint / types | ✅ 97 passed (unit + media + integration, fake transcriber), 2 GPU tests + 1 slow CPU test passed separately; ruff clean; mypy strict clean (58 files) |

## Findings and decisions
- **Word-start correction (+50 ms lead).** wav2vec2 (CTC) alignment marked word *starts* about 59 ms late,
  consistently, while word *ends* were accurate (−8 ms). A larger alignment model
  (`WAV2VEC2_ASR_LARGE_LV60K_960H`) was no better (85 % vs 88 % within ±100 ms), so the default model stays.
  A late start would cut the first consonant of every clip, so `postprocess.lead_word_starts` moves starts
  50 ms earlier, never overlapping the previous word (`AsrConfig.start_lead_ms`; `version` bumped to 2).
  The lead was tuned on the calibration sample and verified on the held-out one.
- **Ground truth is synthetic.** Each word was synthesized separately with espeak-ng, trimmed, and placed at a
  known time with 0–120 ms gaps (`tests/fixtures_speech.py`). It measures alignment precisely but is robotic
  speech. **Recommended manual check:** transcribe one of your own recordings and spot-check ~20 word times
  in an editor; if real speech shows a different bias, `start_lead_ms` is the knob.
- **Idempotency key uses settings, not GPU detection.** Keys are computed in the API process (no GPU), so the
  transcribe key is a fingerprint of the ASR *settings*. The worker resolves the device at run time.
- **Bug fixed: random "Not signed in."** Docker Desktop's VM clock is occasionally stepped back slightly when it
  resyncs, so a freshly issued session token looked "issued in the future" (PyJWT `ImmatureSignatureError`).
  60 s leeway added, with a regression test. Separately, two test failures were seen once while the GPU
  benchmark was running; their details were cut off by an output filter and they did not recur in 14
  subsequent full runs (5 of them under GPU load). Recorded here in case they reappear.
- **No `avg_logprob`/`no_speech_prob`.** WhisperX's batched mode doesn't expose them; the columns stay (null).
  Mean alignment score (`confidence`) and the `repetitive` flag stand in for hallucination detection.
- **Known warning:** torchcodec (used by pyannote for file decoding) can't load because the image lacks
  `libpython3.12.so`. Harmless now (audio is passed as arrays); fixed in Phase 3's image change, where
  diarization needs it.

## Disk usage (incident on 2026-11-19 and what changed)
Docker's virtual disk on D: grew to 44.7 GB and filled the drive, which stopped the Docker engine. Causes:
image rebuilds (each torch layer is several GB), build cache, and multi-GB test files. Deleted files inside
Docker never shrink the disk file on D:. Cleanup: build cache pruned, test files and the unused 1.2 GB
alignment model deleted.
Steady state is about 19 GB of images + 4.7 GB of models. Media needs about 2.5× each source video
(original + CFR mezzanine + proxy + audio). **Planned:** a retention job (spec §2 item 10) to delete
proxies/audio/mezzanines of finished projects. That's worth doing before heavy use, not at the end.

## How to run the checks
```bash
docker compose exec api pytest                                          # unit
docker compose exec api pytest -m "integration or media"                # Postgres + FFmpeg (fake transcriber)
docker compose exec worker-gpu pytest -m gpu -s tests/gpu               # real models, word-timing accuracy
docker compose exec api pytest -m "media and slow" -s -k cpu            # CPU fallback
docker compose exec worker-gpu python scripts/bench_asr.py --minutes 60 # long-audio benchmark / OOM check
```
