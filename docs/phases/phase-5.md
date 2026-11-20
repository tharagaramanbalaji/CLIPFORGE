# Phase 5: Clipping

**Status:** Complete. All acceptance criteria passed on 2026-11-20 (RTX 3060, NVENC). No LLM usage in this phase.

## What was built
| Area | Files |
|------|-------|
| RenderSpec | `engine/render/spec.py`: bounds, preset, frame mode, fps, encoder, quality, measured loudness, version; hashed; stored on every clip version so any render can be reproduced |
| Presets | shorts / reels (1080×1920, ≤ 180 s), tiktok (1080×1920, ≤ 600 s), wide (1920×1080), square (1080×1080); H.264 high + AAC 48 kHz stereo, faststart, −14 LUFS / −1 dBTP |
| Filtergraph | `engine/render/filtergraph.py`: fast input seek to 3 s before the clip, then frame-exact `trim` by timestamp (half-frame margins) and `-frames:v`; **center crop** or **fit + blurred background**; fps filter only when the rate actually changes; two-pass `loudnorm` (pass 1 measured on exactly the clip span, pass 2 linear); always an audio track; bitexact flags |
| Runner | `engine/render/runner.py`: NVENC with automatic x264 fallback; thumbnail with the same framing; `verify_output` (codec/profile, size, fps, exact frame count, duration, AAC 48 kHz stereo, platform max length, moov-before-mdat) |
| Schema (Alembic `0006`) | `clips` (title, hook text, editable bounds, status, current version), `clip_versions` (version number, final render spec + hash, preset/aspect, storage keys, checks, run link) |
| Pipeline | Run kind `render`: one `render` stage on the render queue (GPU worker, NVENC) per clip version |
| API | `POST /media/{id}/clips` (from candidate IDs or a manual range), `GET /media/{id}/clips`, `PATCH /clips/{id}` (title, hook text, bounds), `POST /clips/{id}/render` (preset, frame mode; new immutable version each time), `GET /clips/{id}/versions`, `GET /clip-versions/{id}/video` (HTTP Range, seekable), `/thumbnail`. Rendering requires an active rights grant |
| UI | `frontend/src/Clips.tsx`: per ready video: usage estimate, "Find highlights" (talk/gaming), ranked candidates → "Make clip", preset + frame mode, Render / Re-render, Play (in-page player), Download |

## Acceptance criteria and results
| Criterion | Result |
|-----------|--------|
| Frame-accurate start/end (±1 frame) on fixtures | ✅ **exact** (0 frames off): frame-numbered fixture (brightness ramp, 28-frame cycle), cuts at 10.017–22.983 s and 3.333–15.000 s, crop and blur modes; every cycle reset evenly spaced, so no dropped or duplicated frames |
| A/V sync within 1 frame across a 60 s clip | ✅ **0.0 ms** offset at all 60 flash/beep pairs, 0 ms drift (clip 12.5–72.5 s, blur mode, loudness normalized) |
| Output passes ffprobe checks per platform preset | ✅ shorts 1080×1920, wide 1920×1080, square 1080×1080: codec, size, fps, exact frame count, AAC 48 kHz stereo, faststart; a wrong length is rejected |
| Renders reproducible from `render_spec` | ✅ re-render from the stored spec (dict round-trip, measured loudness included) is **bit-identical** (same SHA-256) with x264 |
| Real footage on the GPU | ✅ owner's EA FC moment 5:12.9–5:29.5: NVENC **3.6–4.0 s for a 16.6 s clip** (~4× realtime), 498/498 frames, all checks pass, output −13.7 LUFS (target −14) |
| Tests / lint / types | ✅ 135 passed (incl. 7 render tests and a clip/version/rights API test); ruff clean; mypy strict clean (90 files); frontend builds |

## Findings and decisions
- **The first frame-accuracy attempt was off by one** at both ends. Input seeking leaves the first timestamp a hair off,
  and the `fps` filter then dropped and duplicated frames. Fixed with a coarse pre-seek plus timestamp `trim`, and `fps` only
  when converting rates.
- **Center crop loses gameplay context** (scoreboard, goal; see the test thumbnail). Fit + blurred background is the
  default; proper reframing (face tracking, facecam/gameplay layouts) is Phase 7.
- NVENC isn't bit-reproducible across driver versions; reproducibility is asserted with x264, and the stored spec
  records which encoder was used.
- Clip lengths are checked against each platform's maximum before rendering.

## How to run
```bash
docker compose exec api pytest -m media tests/media/test_render.py -s   # frame accuracy, A/V sync, presets, reproducibility
```
In the UI: open a ready video → choose talk/gaming → Find highlights → Make clip → Render → Play / Download.
