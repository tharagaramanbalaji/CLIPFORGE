# Phase 1: Ingestion

**Status:** Complete. All acceptance criteria verified on 2026-11-19 (results below).

## Scope
Users/workspaces/creators/rights grants (minimal, single operator), resumable upload, sha256, ffprobe probe,
validation, VFR detection, mezzanine/proxy generation, pipeline_runs/stage_runs + orchestrator, SSE status,
minimal upload page.

## What was built
| Area | Files |
|------|-------|
| Schema (Alembic `0002`) | `db/models/{tenancy,media,pipeline}.py`: workspaces, users, memberships, creators, rights_grants, projects, media_assets, upload_sessions, artifacts, pipeline_runs, stage_runs. UUIDv7 keys (`db/ids.py`). |
| Auth | `services/auth.py`, `api/routers/auth.py`: argon2 passwords, JWT in an httpOnly cookie. The first registration creates the owner + workspace; registration then closes. |
| Creators, rights, projects | `api/routers/catalog.py`. Grants have basis, scope (platforms, monetization, attribution, music flag), validity window, evidence note, revoke. |
| Resumable upload | `services/uploads.py`, `api/routers/uploads.py`. 16 MiB chunks written at their offset in a preallocated file; any order, idempotent, optional per-chunk SHA-256; `GET /uploads/{id}` lists received chunks; `complete` refuses while chunks are missing. |
| Rights gate | Creating an upload, completing it, re-analyzing and retrying all require a grant that belongs to the project's creator and is active now (not revoked/expired/future). |
| Media engine | `engine/media/probe.py` (ffprobe parse, timestamp-based VFR check), `validate.py` (clear rejection messages), `transcode.py` + `ffmpeg.py` (mezzanine/proxy with progress, timeout, cancel). |
| Pipeline | `pipeline/dag.py` (probe → mezzanine → proxy), `orchestrator.py` (advisory-locked `advance`, idempotency keys, skip/reuse, retries with backoff, cancel, retry), `stages.py` (handlers). |
| Workers | `workers/tasks.py`: one generic `execute_stage` task; Celery only executes, state is in Postgres. |
| Live status | `services/events.py` (Redis pub/sub), `GET /api/v1/events/stream` (SSE, per workspace). |
| Frontend | Sign-in, creators & rights, projects, resumable uploader (pause/resume, survives reload via `localStorage` + server chunk list), media list with live stage progress, retry/cancel/archive. |
| Tests | `tests/unit/test_ingest_logic.py`, `tests/media/test_media_engine.py`, `tests/integration/test_ingest_flow.py`, `scripts/e2e_ingest.py` |

## Acceptance criteria and results
Verified 2026-11-19 inside the Docker stack (RTX 3060, FFmpeg 6.1.1).

| Criterion | Result |
|-----------|--------|
| Upload a 5 GB file with a simulated interruption and resume | ✅ `scripts/e2e_ingest.py`: 5.0 GB = 4.69 GiB (300 × 16 MiB chunks). The connection dropped halfway through chunk 120 and the client was discarded; the server kept exactly chunks 0–119 (the half chunk was not recorded); `complete` refused with 409; the new client resumed from the server's list. The reassembled file's sha256 matches the source. Upload ran at 244 MB/s inside the container. |
| Metadata correct for MP4 / MOV / MKV / VFR fixtures | ✅ integration tests: 1280×720, h264/aac, 30 fps, 4.0 s ± 0.1 s, container family, `is_vfr` false/false/false/**true** |
| VFR normalized to a CFR mezzanine | ✅ test: the mezzanine re-probes as CFR with the same duration (±150 ms). Real workers: 2-min 1080p60 VFR clip ready in 34 s using **NVENC** for mezzanine + proxy |
| Corrupt / unsupported files fail with clear errors | ✅ random bytes and a truncated MP4 → "The file could not be read as a video (...). It may be corrupt or incomplete."; audio-only → "No video track found..."; `.txt` rejected before upload; no server paths or command lines in the messages |
| Re-running probe is a no-op | ✅ `POST /media/{id}/analyze` → every stage `skipped` with `reused_stage_run_id`; nothing dispatched; metadata unchanged (tests + real stack) |
| Upload without a rights grant is rejected | ✅ missing grant 422; another creator's grant, expired grant and revoked grant 403; a grant revoked mid-upload blocks `complete` (403) |
| Tests | ✅ 85 passed (`pytest -m "not gpu"`: unit + media + integration); ruff clean; mypy strict clean (50 files) |
| Frontend | ✅ `npm run build`; checked in the browser: owner sign-up, creator, rights grant, project, a 40 MB chunked upload with progress, live stage status over SSE, clear failure message |

Bug found during the browser check and fixed: the SSE endpoint kept its database session (and an open
transaction) for as long as a tab stayed open. It now releases it right after authentication. A regression test
(`test_event_stream_does_not_hold_a_db_transaction`) fails without the fix.

## Decisions and deviations from the spec
- **`stage_runs.idempotency_key` is indexed, not UNIQUE.** UNIQUE would forbid running a stage again in a new
  pipeline run. Instead `advance()` looks for an earlier *succeeded* stage run with the same key and marks the new
  one `skipped` (`metrics.reused_stage_run_id`). Keys chain through dependencies, so changing an upstream stage's
  config or version invalidates everything downstream.
- **Mezzanine and proxy run on the `render` queue**, not `cpu`: that worker has the GPU, so both use NVENC
  (x264 fallback if NVENC fails or no GPU is visible; `CLIPFORGE_VIDEO_ENCODER=auto|nvenc|x264`).
- **Passthrough mezzanine:** CFR H.264/HEVC yuv420p in MP4/MOV/MKV is used as-is (no re-encode). Anything else
  (VFR, other codecs/containers, 10-bit) is transcoded to CFR H.264 with a keyframe every ~2 s; frame rate is
  snapped to the nearest standard rate and capped at 60.
- **VFR detection uses packet timestamps** from the first 60 s (>2% irregular frame intervals), not just
  `r_frame_rate` vs `avg_frame_rate`, which misfires on MKV/FLV millisecond timebases. Both are recorded in
  `media_assets.probe.vfr`.
- **Audio is required at ingest** (transcription needs it); silent gameplay-only sources are rejected with a clear message.
- **Single operator:** membership roles exist in the schema, but there is no invite/member UI.
- **Archive instead of delete:** `DELETE /media/{id}` cancels any running pipeline and marks the asset archived.
  Files stay until a retention job exists (later phase).
- **Errors shown to users never include server paths or command lines**; full details go to the logs.
- Deferred from §8: rights-grant evidence file upload, `GET /media/{id}/stream` (Phase 5 player), audit events.

## How to run the checks
```bash
docker compose exec api pytest                                   # unit
docker compose exec api pytest -m "integration or media"         # real Postgres + FFmpeg, throwaway *_test DB
docker compose exec api python scripts/e2e_ingest.py --big-gb 5  # fresh install only (registers the owner)
```
