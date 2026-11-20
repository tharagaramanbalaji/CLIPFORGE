# Phase 3: Diarization, scenes, signals

**Status:** Complete. All acceptance criteria passed on 2026-11-20 (RTX 3060 12 GB).

## Scope
pyannote speaker diarization with speakers assigned to words; PySceneDetect on the proxy with keyframes; audio
signals (loudness, energy, speech rate, laughter/excitement/music) stored as series, plus a debug plot.

## What was built
| Area | Files |
|------|-------|
| Diarization | `engine/diarize/pyannote_backend.py`: `pyannote/speaker-diarization-community-1` (pyannote 4.1) on an in-memory waveform; uses its overlap-free output; speaker count from the creator's `default_speaker_count` when set. `engine/diarize/assign.py` (pure): word → speaker by maximum overlap, nearest turn within 400 ms otherwise; segment speaker = longest-speaking; labels normalized to S1, S2, ... |
| Scenes | `engine/scenes/detect.py`: PySceneDetect `AdaptiveDetector` on the proxy (same timeline as the mezzanine), middle-frame JPEG keyframe per scene via OpenCV seek |
| Signals | `engine/signals/audio.py`: loudness (EBU R128 momentary LUFS, 100 ms), energy (RMS dBFS, 100 ms), speech rate (words/s, 5 s window, 1 s hop). `engine/signals/events.py`: AudioSet classifier (`MIT/ast-finetuned-audioset-10-10-0.4593`, public, fp16 on GPU) → laughter, excitement (cheering/screaming/shouting/applause/crowd), music; 2 s windows, 1 s hop |
| Schema (Alembic `0004`) | `speakers` (label, user display name kept across re-runs, total speech), `scenes` (frames + ms + keyframe key), `signal_series` (kind, hop, unit, float32 `.npy` in storage, stats). Words gain `spk`; segments get `speaker_label`. |
| Pipeline | DAG: … transcribe → {diarize (gpu), signals (gpu)}; proxy → scenes (cpu). GPU stages run one at a time, so models never share VRAM. With no HF token, diarize **succeeds with `skipped_reason: no_hf_token`** instead of failing the whole analysis; adding a token later re-runs it (token presence is part of its key). |
| API | `GET /media/{id}/speakers`, `PATCH /media/{id}/speakers/{label}` (rename), `GET /media/{id}/scenes`, `GET /media/{id}/scenes/{idx}/keyframe`, `GET /media/{id}/signals?max_points=` (max-pooled downsampling), **`GET /media/{id}/signals/plot`** (SVG debug view: one lane per signal, speaker lane, scene-cut lines, time axis) |
| Image | Heavy deps (torch/WhisperX/pyannote) now sit in their own layer keyed on `requirements/heavy.txt`, so pyproject/code changes no longer re-download ~5 GB. Added `libpython3.12` (fixes the torchcodec warning), `scenedetect`, `opencv-python-headless`. |

## Acceptance criteria and results
| Criterion | Result |
|-----------|--------|
| Speaker labels on a 2-person podcast fixture ≥ 85 % word-level agreement with hand labels | ✅ **100 %** word agreement (110/110 words), 2 speakers and all 12 turns found, both with and without a known speaker count; 15× realtime, 2 GB VRAM peak, released afterwards. Fixture: `tests/gpu/test_diarization.py` (2-voice dialogue, every word's true speaker and time known). See findings for the first, failed attempt. |
| Scene cuts detected exactly on a synthetic cut fixture | ✅ 6 visually distinct shots cut at frames 45, 105, 143, 218, 270: all 5 cuts found **at the exact frame**, no extra cuts; a continuous 6 s shot gives 1 scene; one keyframe per scene. |
| Signals stored and plotted in a debug endpoint | ✅ 6 series stored as `.npy` + stats; `/signals` returns them downsampled; `/signals/plot` returns an SVG. Real-stack run on a 2-min speech clip: loudness max −17.6 LUFS, speech rate 1.4–4.0 words/s, laughter/excitement/music ≤ 0.09 on plain speech (expected). |
| Audio-event model on GPU | ✅ 10 min of audio in 10.5 s (**57× realtime**, ~3 min for a 3 h stream), peak 1.5 GB VRAM, released afterwards. No false laughter on speech. |
| Real pipeline (all 8 stages on the workers) | ✅ `scripts/e2e_ingest.py`: 2-min VFR clip ready in 44 s; resumable upload with checksum match; re-analysis skips all stages. |
| Tests / lint / types | ✅ 108 passed (unit + media + integration, fake models); 6 GPU tests pass (ASR calibration + held-out, 2× diarization, audio events); ruff clean; mypy strict clean (69 files) |

## Findings and decisions
- **First diarization run failed, because of the test audio.** With two espeak voices that differed only in pitch
  and accent, pyannote heard one speaker (57 % agreement) and, when forced to two, found only 5 of 12 turns (46 %).
  Speaker-embedding models identify people by vocal-tract (formant) characteristics, and both voices came from
  the same synthetic vocal tract. After scaling voice B's formants up and voice A's down (like a shorter and a longer
  vocal tract), the same pipeline scored 100 %. The pipeline code didn't change.
  **Caveat:** synthetic turns are clean (no overlap, no "mm-hm" backchannels), so real podcasts will score lower.
  **Recommended:** a check on a real 2-person recording with hand-labeled speakers (yours, or a public sample).
- **FFmpeg 6.1 doesn't print `ebur128` frame logs**; loudness is read from the filter's frame metadata
  (`ametadata=print`), which is also more robust to format changes. Caught by the integration test.
- **Bug caught:** the transcript API's word schema dropped the new `spk` field; fixed and tested.
- **Circular import** between the stage modules: the stage-name → handler map now lives in `pipeline/handlers.py`.
- **Laughter accuracy is not measured** (no laughter ground truth; synthetic laughter isn't realistic).
  The detector is checked for running, speed, memory, and no false positives on speech. Its real value will show
  in Phase 4's eval set, where labeled highlights reveal whether these signals help ranking.
- **Music detection** is kept because it doubles as the Content-ID risk flag needed in later phases.
- Scene detection runs on the 540p proxy (≤ 30 fps); cut times are precise to one proxy frame (≤ 33 ms) on the
  mezzanine timeline.

## Disk
Images 19.5 GB + models 5.0 GB (+0.4 GB this phase). The one-time layer restructure re-downloaded torch, so the
Docker disk file on D: grew to 56.6 GB; an admin compaction would return about 30 GB (see Phase 2 doc). Build
cache is pruned after rebuilds.

## How to run the checks
```bash
docker compose exec api pytest -m "not gpu and not slow"            # unit + media + integration
docker compose exec worker-gpu pytest -m gpu -s tests/gpu           # ASR accuracy, audio events, diarization*
```
*Diarization tests run once `CLIPFORGE_HF_TOKEN` is set in `.env` and the services are recreated
(`docker compose up -d --force-recreate`).
