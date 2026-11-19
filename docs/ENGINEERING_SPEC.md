# ClipForge AI — Engineering Specification (v0.1, draft for approval)

Status: **Proposed** — awaiting approval before Phase 0/1 implementation.
Date: 2026-11-19

---

## 0. Executive summary — the decisions that matter most

| # | Decision | Why |
|---|----------|-----|
| 1 | **Claude never emits raw timestamps. It references transcript segment/word IDs; the backend resolves them to time.** | The single biggest accuracy win. LLMs drift on numeric timestamps; IDs are verifiable and snap exactly to word boundaries. |
| 2 | **Run all workers (and ideally the whole stack) in Linux: WSL2 + Docker with NVIDIA GPU passthrough.** Windows-native is only for the editor/browser. | Celery does not support Windows (prefork pool missing; `solo` pool only), pyannote/WhisperX/ffmpeg paths are far smoother on Linux, and production will be Linux anyway. |
| 3 | **A DB-driven pipeline state machine; Celery only executes stage tasks.** No Celery canvas (chains/chords) for orchestration, no Celery result backend for data. | Idempotency, resumability, visibility, and retries per stage become trivial; Celery canvas failure semantics are hard to reason about. |
| 4 | **One GPU worker with concurrency=1 and explicit model load/unload per stage.** | RTX 3060 VRAM is the hard constraint; never hold Whisper + pyannote + face models at once. |
| 5 | **Normalize every upload to a CFR "mezzanine" timeline at ingest** (or at minimum detect VFR and record it). | OBS/phone recordings are often VFR; audio-time (ASR) vs frame-time (render) drift is the #1 cause of subtitle/A-V desync. |
| 6 | **"Audio & visual quality" (10%) is measured by the backend, not scored by Claude.** | Claude reads a transcript; it cannot hear clipping, loudness, or see blur. Signals: loudness/LUFS, speech ratio, laughter/energy peaks, face presence, blur, scene cuts. |
| 7 | **Two-pass LLM: (a) candidate discovery over overlapping windows, (b) per-candidate refinement + scoring with expanded context.** | Keeps each request small, lets pass (b) see context beyond window edges, and lets the backend dedupe between passes. |
| 8 | **Rights/provenance and basic auth move to Phase 1, not Phase 9.** | Provenance must be captured at the moment of upload; it cannot be reliably retrofitted. Rendering/export is blocked without a valid rights record. |
| 9 | **A highlight-quality evaluation set is built in Phase 4, before tuning prompts.** | Without a labeled set, "the highlights are better now" is unfalsifiable and prompt changes are guesswork. |
| 10 | **Subtitles via ASS (libass) burned in by FFmpeg; encode with NVENC.** No browser-based renderer (Remotion etc.) in the core pipeline. | ASS supports per-word karaoke/highlight tags, fonts, positions and styles natively; one FFmpeg pass per clip; NVENC is very fast on a 3060. |

---

## 1. Technical architecture

### 1.1 Logical view

```
                ┌──────────────────────── Frontend (React + Vite + TS + Tailwind) ───────────────────────┐
                │ Upload (resumable) · Projects · Pipeline status · Clip review/editor · Export          │
                └──────────────▲──────────────────────────────────────────────────────────────────────────┘
                               │ REST (JSON) + SSE for status   (no secrets, signed media URLs only)
┌──────────────────────────────┴───────────────────────────────────────────────────────────────────────┐
│ API layer (FastAPI) — auth, validation, routing. Thin: calls application services.                    │
├───────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Application services — use cases (create project, register upload, approve clip, request render…)     │
│   enqueue pipeline runs; never do heavy work in-request                                               │
├───────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Repositories (SQLAlchemy 2.x)          │ Storage (interface: LocalStorage | S3Storage)                 │
├────────────────────────────────────────┴──────────────────────────────────────────────────────────────┤
│ CLIPPING ENGINE (pure Python package, no FastAPI/Celery imports)                                       │
│  media/   probe · validate · mezzanine/proxy · audio extract · loudness                                │
│  asr/     Transcriber protocol → WhisperX / faster-whisper backends                                     │
│  diarize/ Diarizer protocol → pyannote                                                                  │
│  scenes/  PySceneDetect · frame sampling · OpenCV visual metrics                                       │
│  signals/ audio energy, laughter/excitement peaks, speech rate                                         │
│  highlights/ windowing · prompts · Claude client · schemas · validation · dedupe · boundary · scoring  │
│  reframe/ planners (center, face, speaker-aware, layouts) → CropPlan                                    │
│  subtitles/ word grouping · ASS generator · styles                                                     │
│  render/  RenderSpec → FFmpeg filtergraph builder → runner                                             │
│  pipeline/ Stage definitions + orchestrator (operates on DB via repository interfaces)                 │
├───────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Workers (Celery) — thin task wrappers: load StageRun → call engine → persist → advance pipeline        │
│   queues: gpu (c=1) · cpu (c=N) · render (c=1–2, NVENC) · llm (c=4–8, I/O bound)                       │
├───────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Infra: PostgreSQL · Redis (broker + locks + SSE pub/sub) · FFmpeg · NVIDIA runtime                     │
└───────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Modular monolith.** One backend codebase, one image, multiple process roles (`api`, `worker-gpu`, `worker-cpu`, `worker-render`, `worker-llm`, `beat`). Horizontal scaling later = run more worker containers on more machines; nothing in the design assumes a single host except local storage (which the storage interface abstracts).

### 1.2 Process/deployment view (dev)

docker-compose services: `postgres`, `redis`, `api`, `worker-gpu` (GPU reservation), `worker-cpu`, `worker-render` (GPU reservation for NVENC), `worker-llm`, `frontend` (Vite dev server). Media lives in a bind-mounted volume **inside the WSL2 filesystem** (`~/clipforge-data`), not on `/mnt/c` — cross-filesystem I/O from WSL2 to NTFS is 5–10× slower and will dominate FFmpeg time.

### 1.3 Key cross-cutting rules

- Engine modules take **paths + typed config objects** and return **typed results** (Pydantic models / dataclasses). They never touch the DB or HTTP.
- Every stage output is an **artifact** in storage, keyed deterministically: `assets/{asset_id}/{stage}/{config_hash}/…`. Re-running with the same inputs/config is a no-op (idempotency).
- External tools (ffmpeg/ffprobe) are invoked through one `run_command()` wrapper: argument lists (never shell strings), timeouts, stderr capture, structured logging, cancellation.
- Config: `pydantic-settings`, env vars, `.env.example`. Secrets (Anthropic key, HF token, DB creds) only on the backend; frontend gets short-lived signed media URLs.
- Logging: structured JSON (`structlog`), with `asset_id`, `run_id`, `stage`, `job_id` bound in context.

---

## 2. Missing / underspecified requirements (need decisions)

1. **GPU VRAM.** Desktop RTX 3060 = 12 GB; laptop RTX 3060 = 6 GB. This changes model choices (large-v3 vs medium/distil, int8 vs fp16). → *Need answer.*
2. **Languages.** English only at first? WhisperX alignment models are per-language; pyannote is language-agnostic. Multilingual subtitles/hooks? → *Need answer.*
3. **Typical source length and volume.** 20-min videos vs 8-hour streams; videos/day. Drives storage, cost, and whether Batch API is the default.
4. **Users & tenancy.** Is this single-operator (you + a few editors) or SaaS for external creators? Spec says "multiple creators" — I propose **Workspace → Members → Creators → Projects**, with simple email/password + JWT session at first, no SSO.
5. **Rights model details.** What counts as authorization: signed agreement upload, platform OAuth to the creator's own channel, written permission email, own content? Expiry? Revenue share? Also: **underlying third-party content** in the source (game publisher footage, music in streams). Gaming clips carry both the streamer's rights and the game publisher's content policy; background music is the most common cause of Content ID claims. Provenance records should capture both and a "contains copyrighted music?" flag.
6. **Gaming/no-speech highlights.** Transcript-only analysis misses silent gameplay moments (clutch plays, crashes, kills). Need non-speech signals: audio energy spikes, crowd/streamer screaming, scene-change density, optional **chat replay** (Twitch VOD chat is a strong highlight signal — only via authorized access). → Propose: audio-energy/excitement signals in Phase 3, chat ingestion later as optional.
7. **Streamer layout.** Most stream VODs are gameplay + small facecam. The reframing requirement must include **user-defined layout templates** (e.g., "facecam region at x,y,w,h → top 35%, gameplay crop → bottom 65%"). Face detection alone can't do this well. Not in the original spec; I consider it essential for the target audience.
8. **Clip length targets per platform** (Shorts ≤ 3 min since late 2024 but best performance typically < 60 s; Reels ≤ 90 s typical; TikTok up to 10 min). Need configurable min/max/target durations per output preset.
9. **Branding/overlays:** channel watermark, intro/outro cards, progress bar, title card/hook text overlay — very common in clipping channels. Propose as render-preset options (Phase 6).
10. **Retention policy:** originals of multi-GB streams fill disks fast. Need policy: delete proxies after N days, keep originals until project archived, etc.
11. **Cost controls:** per-workspace monthly LLM budget, per-run cost estimate shown before running highlight detection.
12. **Human feedback loop:** approve/reject reasons and edits should be stored as training/eval data to improve prompts.
13. **Observability:** where do logs/metrics go? Propose stdout JSON + a simple `/admin/runs` view; Prometheus/Grafana later.
14. **Upload limits:** max file size (e.g., 50 GB), max duration (e.g., 12 h), allowed codecs.

---

## 3. Technical risks & bottlenecks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Timestamp hallucination / drift** | Clips start mid-word or on wrong moment | Claude returns segment IDs; backend validates range ∈ window, snaps to word boundaries, enforces duration bounds; reject + re-ask on invalid. |
| **ASR word-timestamp quality** | Subtitle flicker, bad cuts | WhisperX forced alignment (wav2vec2) gives much better word timings than Whisper's cross-attention. Add VAD (Silero / pyannote) to avoid hallucinated text in silence/music. |
| **Whisper hallucination on music/gameplay audio** | Garbage transcript, fake "highlights" | VAD gating, `condition_on_previous_text=False`, compression-ratio/no-speech thresholds, flag low-confidence segments to the LLM. |
| **VFR sources** | Progressive A/V and subtitle desync | Detect VFR at ingest (ffprobe `r_frame_rate` vs `avg_frame_rate`, frame timestamps sample); transcode to CFR mezzanine when needed; all timing references the mezzanine. |
| **VRAM exhaustion** | OOM crashes on 3060 | GPU queue concurrency=1; models loaded per stage and freed (`del model; gc.collect(); torch.cuda.empty_cache()`); int8_float16 compute type; configurable model sizes; CPU fallback. |
| **Celery on Windows** | Workers hang/misbehave | Run workers in WSL2/Docker. Documented as a hard requirement. |
| **pyannote** | Gated HF models, slow on long audio, speaker over-segmentation | HF token in env; run on 16 kHz mono WAV; set min/max speakers when known (creator profile); cache embeddings; diarization optional per project. |
| **Long-video processing time** | 3-hour VOD blocks the GPU queue | Benchmarks per stage in Phase 2/3; proxy-resolution analysis; scene detection at downscaled res on CPU; priority queues. Rough 3060 estimate: WhisperX large-v3 int8 ≈ 15–30× realtime → 6–12 min per 3 h. |
| **LLM cost runaway** | Surprise bills | Windowing, prompt caching of the static system prompt, Batch API (50% off) for non-urgent runs, per-run estimate & budget cap, store every request/response with token usage. |
| **Context cut at window edges** | Missed or truncated highlights | Overlapping windows + pass-2 refinement with ±60–120 s expanded context. |
| **Render performance** | Slow exports | NVENC (`h264_nvenc`/`hevc_nvenc`), accurate seek with `-ss` before `-i` on the CFR mezzanine, one filtergraph per clip (crop+scale+subs+overlay), render queue c=1–2. Consumer NVENC has a session cap — keep render concurrency low. |
| **Dynamic crop in FFmpeg** | Smooth speaker tracking is awkward in pure ffmpeg | Prefer **piecewise-stable crops** (hold a framing, cut/ease between speakers) via `sendcmd`/expression-driven `crop`; this also looks more professional than constant panning. Frame-piped OpenCV rendering only as a fallback. |
| **Large uploads over HTTP** | Timeouts, lost progress | Resumable chunked uploads (custom simple protocol in dev; S3 multipart presigned URLs in prod). Never stream multi-GB through a single request. |
| **Legal / platform policy** | Strikes on channels | Provenance gate before render/export; per-clip attribution metadata; music flag. |

---

## 4. Libraries & alternatives

| Concern | Recommended | Alternatives / notes |
|---------|-------------|----------------------|
| ASR | **WhisperX** (wraps faster-whisper + wav2vec2 alignment + pyannote) | faster-whisper alone (simpler, worse word timings); behind a `Transcriber` protocol so both are swappable. Model: `large-v3` (12 GB) or `large-v3-turbo`/`distil-large-v3` (6 GB / speed). |
| VAD | Silero VAD (bundled with faster-whisper) | pyannote VAD |
| Diarization | **pyannote.audio 3.x** (`speaker-diarization-3.1`) | NeMo (heavier); WhisperX's integration uses pyannote anyway |
| Scene detection | **PySceneDetect** `AdaptiveDetector` on proxy | TransNetV2 (more accurate, GPU, extra dependency — later if needed) |
| Face detection | **OpenCV YuNet** (`cv2.FaceDetectorYN`, tiny ONNX, no extra deps) | MediaPipe (good, but heavier dependency and Windows/Linux packaging quirks); InsightFace/RetinaFace (accurate, heavier) |
| Face tracking | Simple IoU/centroid tracker + smoothing (our code, small) | ByteTrack/SORT if needed |
| Active speaker | Diarization-to-face association via **mouth-movement × speech-activity correlation** (landmarks from YuNet) | TalkNet / LoCoNet ASD models (better, but heavy and research-grade) — Phase 7b |
| Audio loudness | FFmpeg `ebur128` / `loudnorm` (two-pass) | pyloudnorm |
| Video I/O | **FFmpeg CLI** via subprocess wrapper | PyAV (for frame-accurate reads in reframe analysis), ffmpeg-python (thin, unmaintained — avoid) |
| Subtitles | **ASS** generated in-house (small) + libass via FFmpeg | `pysubs2` for reading/writing ASS/SRT (small, reliable — recommended) |
| LLM | **Anthropic Python SDK**, `client.messages.parse()` with Pydantic schemas (structured outputs) | — |
| LLM model | **`claude-opus-5-5`** default for both passes, `output_config.effort` tuned per pass; model ID configurable per pass | Cheaper models (`claude-sonnet-5-5`, `claude-haiku-5-5`) for pass 1 are your call — decide after the eval set (§7.8) shows whether quality holds. |
| Queue | **Celery + Redis** | Dramatiq (simpler, cleaner retries), ARQ (async). Celery is fine given we only use plain tasks; revisit if it causes pain. |
| DB | PostgreSQL 16, SQLAlchemy 2.x (typed ORM), Alembic, `psycopg` 3 | — |
| API | FastAPI, Pydantic v2, `pydantic-settings` | — |
| Auth | `pwdlib`/argon2 + JWT (`pyjwt`) in httpOnly cookie | fastapi-users (more magic than needed) |
| Uploads | Custom resumable chunk endpoints (dev) → S3 multipart presigned (prod) | tus protocol + `tusd` (robust, but an extra service) |
| Object storage | `boto3` against MinIO (dev-optional) / S3 / R2 | — |
| Logging | `structlog` | stdlib logging JSON formatter |
| Tests | pytest, pytest-asyncio, httpx `AsyncClient`, testcontainers (Postgres/Redis) or compose-based integration | — |
| Frontend | React 18, Vite, TS, Tailwind, **TanStack Query**, React Router, a plain `<video>` element + custom timeline | wavesurfer.js for waveform in the trim editor (Phase 8) |

**Deliberately not used:** LangChain/LlamaIndex (no value over the SDK for this), MoviePy (slow, re-implements FFmpeg poorly), microservices, Kubernetes.

---

## 5. Database schema

Conventions: UUID PKs (`uuid7` for index locality), `created_at`/`updated_at` timestamptz, soft-delete via `archived_at` where useful. Times stored as **integer milliseconds** on the mezzanine timeline (avoid float drift). Large/structured blobs (word arrays, full LLM responses) in JSONB or storage artifacts.

### 5.1 Tenancy & rights

```
workspaces(id, name, settings jsonb, monthly_llm_budget_usd numeric)
users(id, email unique, password_hash, display_name, is_active)
memberships(workspace_id, user_id, role enum[owner,admin,editor,viewer])   PK(workspace_id,user_id)

creators(id, workspace_id, name, platform_handles jsonb, default_speaker_count int null, notes)
rights_grants(                                   -- provenance / authorization
  id, workspace_id, creator_id,
  basis enum[own_content, written_permission, license_agreement, platform_authorized_integration],
  scope jsonb            -- platforms allowed, monetization allowed, attribution text, content types
  valid_from, valid_until null,
  evidence_storage_key null, evidence_note,
  recorded_by_user_id, revoked_at null)

output_channels(id, workspace_id, name, platform enum[youtube_shorts, instagram_reels, tiktok],
  handle, default_render_preset_id, default_subtitle_style_id)   -- your clipping channels
```

### 5.2 Projects & media

```
projects(id, workspace_id, creator_id, name, status, settings jsonb, archived_at)

media_assets(                                    -- one uploaded source video
  id, project_id, rights_grant_id NOT NULL,      -- provenance captured at upload
  original_filename, source_type enum[upload, integration], source_ref null,
  sha256, size_bytes,
  status enum[uploading, uploaded, validating, ready, failed, archived],
  duration_ms, width, height, fps_num, fps_den, is_vfr bool,
  video_codec, audio_codec, audio_channels, audio_sample_rate, container,
  probe jsonb,                                    -- raw ffprobe output
  uploaded_by_user_id, error jsonb null)

upload_sessions(id, media_asset_id, total_bytes, chunk_size, received_bytes, parts jsonb, expires_at)

artifacts(                                       -- every derived file
  id, media_asset_id, kind enum[original, mezzanine, proxy, audio_wav, frame, transcript_json,
       scene_list, signals, thumbnail, clip_video, clip_subtitles, clip_thumbnail, export_bundle],
  storage_key, size_bytes, sha256 null, config_hash, meta jsonb,
  produced_by_stage_run_id)                       UNIQUE(media_asset_id, kind, config_hash, storage_key)
```

### 5.3 Pipeline state

```
pipeline_runs(id, media_asset_id, kind enum[analysis, highlights, render], status, requested_by,
  config jsonb, config_hash, started_at, finished_at, error jsonb)

stage_runs(                                      -- one per stage per pipeline run
  id, pipeline_run_id, stage enum[probe, mezzanine, proxy, audio_extract, transcribe, align,
        diarize, scenes, signals, highlight_windows, highlight_refine, rank, reframe, subtitles, render, export],
  status enum[pending, queued, running, succeeded, failed, skipped, canceled],
  attempt int, max_attempts int, idempotency_key text UNIQUE,   -- hash(asset, stage, config, input artifact hashes)
  celery_task_id, worker_host, progress real, started_at, finished_at,
  metrics jsonb, error jsonb)
```

### 5.4 Analysis data

```
transcripts(id, media_asset_id, stage_run_id, engine, model, language, config_hash, is_current bool)
transcript_segments(
  id bigserial, transcript_id, idx int,          -- idx = the ID shown to Claude
  start_ms, end_ms, speaker_label null, text,
  words jsonb,                                    -- [{w, s, e, p(prob), spk}]
  avg_logprob, no_speech_prob)                    INDEX(transcript_id, start_ms)
speakers(id, media_asset_id, label, display_name null, total_speech_ms, face_track_id null)
scenes(id, media_asset_id, idx, start_ms, end_ms, keyframe_artifact_id)
signal_series(id, media_asset_id, kind enum[loudness, energy, speech_rate, laughter, face_count],
  hop_ms, values_storage_key)                     -- arrays stored as .npy/.json artifacts, not rows
```

*Why words in JSONB rather than a `words` table:* ~30k words per 3-hour video; we always read words by segment range, never query individual words across videos. JSONB keeps it one indexed range read. Revisit if word-level search becomes a feature.

### 5.5 Highlights & clips

```
llm_calls(id, stage_run_id, purpose enum[discover, refine, vision, metadata], model,
  request_hash, prompt_version, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  cost_usd, latency_ms, stop_reason, request_storage_key, response_storage_key, error jsonb)

highlight_candidates(
  id, media_asset_id, pipeline_run_id, window_idx,
  start_segment_idx, end_segment_idx,            -- what Claude said
  start_ms, end_ms,                               -- backend-resolved + refined
  category enum[funny, reaction, opinion, debate, emotional, surprising, educational, story, exchange, gaming, other],
  hook, description, why_interesting, speaker_labels text[],
  standalone_context_note,
  status enum[proposed, merged, rejected_validation, refined, ranked, selected, discarded],
  merged_into_id null, validation_errors jsonb)

candidate_scores(candidate_id PK, scoring_profile_id,
  hook int, emotion int, standalone int, story int, shareability int,   -- 0–10 from Claude
  av_quality int,                                                        -- 0–10 computed by backend
  rationale jsonb, final_score numeric, rank int)

scoring_profiles(id, workspace_id, name, weights jsonb, is_default)   -- weights must sum to 1.0 (validated)

clips(
  id, project_id, media_asset_id, candidate_id null, output_channel_id null,
  title, description, hashtags text[],
  start_ms, end_ms,                                -- current (user-editable) bounds
  status enum[draft, rendering, rendered, approved, rejected, exported, failed],
  reject_reason null, approved_by, approved_at, current_version_id)

clip_versions(                                    -- every render is immutable & reproducible
  id, clip_id, version int, render_spec jsonb, render_spec_hash,
  aspect enum['9:16','16:9','1:1'], render_preset_id, subtitle_style_id, reframe_plan_id,
  video_artifact_id, thumbnail_artifact_id, subtitle_artifact_id, status, created_by)

subtitle_tracks(id, clip_id, words jsonb, edited bool, edited_by)   -- user edits stored separately from ASR
subtitle_styles(id, workspace_id, name, spec jsonb)                -- font, size, colors, outline, position, highlight mode, max words/line
render_presets(id, workspace_id, name, spec jsonb)                 -- resolution, fps, codec, bitrate, loudness target, watermark
reframe_plans(id, media_asset_id, start_ms, end_ms, strategy, layout, plan jsonb)  -- keyframed crop windows

exports(id, clip_version_id, platform, metadata jsonb, artifact_id, created_at)
audit_events(id, workspace_id, actor_user_id, action, entity_type, entity_id, data jsonb, at)
```

---

## 6. Background job architecture

### 6.1 Orchestration model

```
API: POST /media/{id}/analyze
   → create pipeline_run + stage_runs (pending) from a static DAG definition
   → orchestrator.advance(run_id)

orchestrator.advance(run_id):   (idempotent, runs under a Postgres advisory lock per run)
   for each stage whose dependencies all succeeded and status=pending:
        if artifact for idempotency_key exists → mark skipped (reuse)
        else → mark queued, send Celery task to the stage's queue

celery task execute_stage(stage_run_id):
   mark running (attempt++) → load inputs → call engine function → write artifacts + rows
   → mark succeeded → orchestrator.advance(run_id)
   on retryable error → Celery autoretry with exponential backoff (max_attempts per stage)
   on fatal error → mark failed, mark run failed, emit event
```

Analysis DAG:

```
probe → mezzanine(if VFR/unsupported) → ┬→ audio_extract → transcribe(+align) → diarize ─┐
                                        ├→ proxy → scenes ───────────────────────────────┤
                                        └→ signals (audio energy, loudness) ─────────────┴→ (analysis done)
highlights run:   highlight_windows → highlight_refine → rank
render run (per clip version): reframe → subtitles → render → thumbnail
```

### 6.2 Queues

| Queue | Concurrency | Work |
|-------|-------------|------|
| `gpu` | **1** | transcribe/align, diarize, face detection for reframing |
| `cpu` | cores−2 | probe, proxy, audio extract, scene detection, signals |
| `render` | 1–2 | FFmpeg NVENC renders, thumbnails |
| `llm` | 4–8 | Claude calls (I/O bound), with a Redis token-bucket rate limiter |

Celery settings: `task_acks_late=True`, `worker_prefetch_multiplier=1`, `task_reject_on_worker_lost=True`, explicit time limits per stage (scaled by media duration), JSON serializer, no result backend dependence (state is in Postgres).

### 6.3 Reliability

- **Idempotency:** `idempotency_key = sha256(stage, asset_id, input artifact hashes, stage config, engine version)`. Outputs written to a temp dir then atomically moved; DB rows written in one transaction after files exist.
- **Retries:** network/LLM 429/5xx/ffmpeg transient → retry with backoff+jitter; validation/codec errors → fail fast with a user-readable message.
- **Temp files:** each task gets `tmp/{stage_run_id}/` with a context manager that always cleans up; a periodic beat task sweeps orphans older than 24 h.
- **Cancellation:** `pipeline_runs.status=canceled` checked between chunks; ffmpeg subprocesses terminated.
- **Progress:** stages report progress (ffmpeg `-progress pipe:1`, ASR segment count) → DB + Redis pub/sub → SSE to frontend.
- **GPU memory:** a `gpu_model_scope()` context manager loads a model, yields it, and guarantees release; models are never cached across different stages in the same worker process (configurable cache for the same stage when processing a batch).

---

## 7. AI highlight detection workflow

### 7.1 Inputs prepared by the backend

For each media asset: transcript segments (with idx, speaker label, start/end), word timings, scene boundaries, signal peaks (loudness/energy/laughter), and creator context (who the speakers are, channel genre, language).

### 7.2 Windowing

- Window by **time, aligned to segment boundaries**: default 8 min windows, 2 min overlap (configurable).
- Each window is rendered to compact text the model can reference:
  ```
  [s0412 | 01:02:13.4–01:02:17.9 | SPK_1] I swear if he jumps off that building again—
  [s0413 | 01:02:18.0–01:02:19.1 | SPK_2] (laughs) He's gonna do it.
  ~ [energy peak 01:02:18.5, +14 dB]   [scene cut 01:02:20.1]
  ```
- Low-confidence segments are marked (`?`) so Claude discounts them.
- A 3-hour video ≈ 30k words ≈ 40k tokens of transcript → ~28 windows of ~4–5k tokens each.

### 7.3 Pass 1 — discovery (per window)

- System prompt (static, cached with `cache_control`): task definition, category taxonomy, what makes a moment work *standalone*, explicit anti-patterns (keyword-only triggers, inside jokes needing earlier context, mid-sentence starts), and the output schema.
- User message: creator context + window transcript.
- Output via **structured outputs** (`client.messages.parse()` with a Pydantic model):
  ```python
  class Candidate(BaseModel):
      start_segment: int          # must be within this window
      end_segment: int
      category: Category
      hook: str                   # the first line/visual that grabs attention
      description: str
      why_interesting: str
      speakers: list[str]
      standalone_context: Literal["self_contained", "needs_light_setup", "needs_prior_context"]
      setup_segment: int | None   # earlier segment needed for context, if any
      scores: DimensionScores     # hook, emotion, standalone, story, shareability: 0–10 + one-line rationale each
  class WindowResult(BaseModel):
      candidates: list[Candidate]  # 0..N; empty is valid
  ```
- Effort `medium`, thinking adaptive. Requests are independent → run in parallel on the `llm` queue, or via the **Message Batches API** (50% cheaper, results within hours) when the run isn't urgent.

### 7.4 Validation (backend, deterministic)

Reject or repair each candidate:
- `start_segment ≤ end_segment`, both exist and fall inside the window (± overlap).
- Resolved duration within preset bounds (e.g., 12–90 s); too long → mark for refinement, not silently trim.
- Not dominated by low-confidence/no-speech segments (unless category=gaming and backed by an energy peak).
- Speakers referenced exist in those segments.
- Times within `[0, media.duration_ms]`.
Invalid candidates are stored with `validation_errors` (for prompt debugging), never dropped silently.

### 7.5 Dedupe / merge

Window overlap yields duplicates. Merge when temporal IoU ≥ 0.5 **or** one contains the other and categories match; keep the union bounds if ≤ max duration else the higher-scored one. Pure interval logic — no LLM needed.

### 7.6 Pass 2 — refinement & scoring (per surviving candidate)

- Send the candidate plus **±90 s of surrounding transcript** (crossing window edges) and the pass-1 assessment.
- Ask Claude to: choose the tightest start/end **segment and word indices** that preserve setup and payoff; confirm or downgrade standalone-ness; produce final dimension scores with rationale; write a title, on-screen hook text, and a 1-line description.
- Optional (config, top-N only): attach 3–6 downscaled keyframes for **vision** assessment (is the visual interesting / is the facecam visible). Costs are bounded by N.

### 7.7 Boundary refinement (deterministic)

Using word timings:
- Start: snap to the first word's start − pre-roll (default 150 ms), but never cut into a previous word; prefer a nearby silence gap ≥ 250 ms if within 1 s.
- End: last word end + post-roll (default 400 ms, longer if a laughter/energy peak continues), snap to silence gap if available.
- Optionally nudge to a scene cut within ±0.5 s for cleaner visual starts.
- Final: clamp to media duration; re-check duration bounds.

### 7.8 Scoring & ranking

```
final = Σ weight_d × score_d / 10      (weights from scoring_profiles; validated to sum to 1.0)
av_quality  = f(LUFS in range, clipping %, speech-to-noise, face present, blur, black frames)   # backend-computed
```
Plus deterministic penalties: overlap with an already-selected clip, `needs_prior_context`, extreme length. Rank; select top-K per asset with diversity across categories/time. **Scores are heuristics, displayed as such in the UI**; the dashboard records approvals/rejections to calibrate later.

**Evaluation harness (Phase 4 deliverable):** 5–10 representative videos hand-labeled with "good clip" ranges. Metrics: precision@K (does a selected clip overlap a labeled moment), boundary error (ms) on starts/ends, and pass rates for "starts mid-sentence" / "ends before payoff". Every prompt or model change runs against it before merge. This is also where model/effort choices for each pass are decided with numbers rather than guesses.

### 7.9 Claude integration specifics

- Official `anthropic` Python SDK; model ID and `effort` configurable **per pass**; default `claude-opus-5-5`.
- Structured outputs via `messages.parse()` + Pydantic; still validate semantically (§7.4).
- Handle `stop_reason` (`refusal`, `max_tokens`) explicitly; server-side refusal fallback (`fallbacks: "default"`) enabled; typed exception handling (rate-limit → retry, 4xx → fail).
- Static system prompt first and cached; window content after the cache breakpoint.
- `prompt_version` constant stored on every `llm_calls` row; prompts live in versioned files (`highlights/prompts/discover_v1.md`).
- Every request/response persisted (storage) for debugging and evals.

### 7.10 Cost estimate (3-hour video, Opus 5.5 list price $4 / $20 per MTok)

| Pass | Requests | Input | Output (incl. thinking) | ≈ Cost |
|------|----------|-------|--------|--------|
| Discovery | ~28 | ~140k (system prompt cached after the first) | ~60k | ~$1.70 |
| Refinement | ~30 candidates | ~180k | ~45k | ~$1.60 |
| Vision (optional, top 10) | 10 | ~40k | ~10k | ~$0.35 |
| **Total** | | | | **≈ $3–4 per 3 h** (≈ $1.5–2 with Batch API) |

Rough planning numbers only; real figures come from `llm_calls` after Phase 4. Transcription/diarization are local (free besides electricity/time).

---

## 8. API endpoints (v1, all under `/api/v1`)

Auth: httpOnly cookie JWT; every resource scoped to the caller's workspace.

```
Auth / workspace
POST   /auth/register · /auth/login · /auth/logout        GET /auth/me
GET    /workspaces/current          PATCH /workspaces/current
GET|POST /workspaces/current/members   DELETE /workspaces/current/members/{user_id}

Creators & rights
GET|POST /creators                  GET|PATCH|DELETE /creators/{id}
GET|POST /creators/{id}/rights-grants   POST /rights-grants/{id}/revoke
POST   /rights-grants/{id}/evidence   (file upload)

Projects
GET|POST /projects                  GET|PATCH|DELETE /projects/{id}

Uploads & media
POST   /projects/{id}/uploads                 → {upload_id, chunk_size}  (requires rights_grant_id)
PUT    /uploads/{upload_id}/chunks/{n}        (raw bytes, Content-Range, idempotent per chunk)
GET    /uploads/{upload_id}                   → received chunks (resume)
POST   /uploads/{upload_id}/complete          → media_asset (status=validating)
GET    /projects/{id}/media                   GET /media/{id}    DELETE /media/{id}
GET    /media/{id}/stream?variant=proxy       (signed URL / range requests)

Pipeline
POST   /media/{id}/analyze                    (transcribe+diarize+scenes+signals; options)
POST   /media/{id}/highlights                 (run discovery; returns cost estimate first if ?dry_run=true)
GET    /pipeline-runs/{id}                    (stages, progress, errors)
POST   /pipeline-runs/{id}/cancel · /retry
GET    /events/stream                         (SSE: run/stage/clip status for the workspace)

Analysis results
GET    /media/{id}/transcript?from_ms&to_ms
PATCH  /media/{id}/speakers/{label}           (rename speaker)
GET    /media/{id}/scenes
GET    /media/{id}/candidates?status&sort=score

Clips
POST   /media/{id}/clips                      (from candidate_ids or manual range)
GET    /projects/{id}/clips?status            GET|PATCH /clips/{id}   (title, bounds, hashtags)
GET|PUT /clips/{id}/subtitles                 (word-level edits)
POST   /clips/{id}/render                     (preset, aspect, subtitle style, reframe options) → new clip_version
GET    /clips/{id}/versions
POST   /clips/{id}/approve · /reject {reason}
POST   /clips/{id}/export {platforms[]}        GET /exports/{id}/download

Presets
GET|POST|PATCH /subtitle-styles · /render-presets · /scoring-profiles · /output-channels

Ops
GET /health · /ready          GET /admin/usage (LLM cost, processing time per workspace)
```

---

## 9. Folder structure

```
clipforge/
├─ README.md
├─ docker-compose.yml  · docker-compose.gpu.yml
├─ .env.example
├─ Makefile (or justfile)           # dev, test, lint, migrate, fmt
├─ docs/
│  ├─ ENGINEERING_SPEC.md           # this file
│  ├─ adr/                          # architecture decision records (0001-ids-not-timestamps.md, …)
│  ├─ phases/                       # per-phase plan + acceptance results
│  └─ runbooks/                     # WSL2/GPU setup, troubleshooting
├─ backend/
│  ├─ pyproject.toml  (uv)          ├─ Dockerfile
│  ├─ alembic.ini · alembic/versions/
│  ├─ src/clipforge/
│  │  ├─ config.py · logging.py · errors.py
│  │  ├─ engine/                    # ← independent of FastAPI/Celery/DB
│  │  │  ├─ common/ (process.py, timecode.py, gpu.py, tempfiles.py, hashing.py)
│  │  │  ├─ media/ (probe.py, validate.py, mezzanine.py, proxy.py, audio.py, loudness.py)
│  │  │  ├─ asr/ (base.py, whisperx_backend.py, faster_whisper_backend.py)
│  │  │  ├─ diarize/ (base.py, pyannote_backend.py, assign.py)
│  │  │  ├─ scenes/ (detect.py, frames.py, visual_metrics.py)
│  │  │  ├─ signals/ (energy.py, laughter.py)
│  │  │  ├─ highlights/ (windowing.py, prompts/, client.py, schemas.py, validate.py,
│  │  │  │               dedupe.py, refine.py, boundaries.py, scoring.py)
│  │  │  ├─ reframe/ (base.py, center.py, face.py, speaker.py, layouts.py, smoothing.py)
│  │  │  ├─ subtitles/ (grouping.py, ass.py, styles.py)
│  │  │  └─ render/ (spec.py, filtergraph.py, runner.py, thumbnail.py)
│  │  ├─ storage/ (base.py, local.py, s3.py)
│  │  ├─ db/ (base.py, session.py, models/…)
│  │  ├─ repositories/
│  │  ├─ services/                  # use cases; used by api & workers
│  │  ├─ pipeline/ (dag.py, orchestrator.py, stages/…)
│  │  ├─ workers/ (celery_app.py, tasks.py)
│  │  └─ api/ (main.py, deps.py, routers/…, schemas/…)
│  └─ tests/
│     ├─ unit/  ├─ integration/  ├─ e2e/
│     └─ fixtures/media/            # tiny generated clips (ffmpeg lavfi), small speech sample
├─ frontend/
│  ├─ package.json · vite.config.ts · tailwind.config.ts · Dockerfile
│  └─ src/ (api/, components/, features/{projects,upload,media,clips,review,settings}/, routes/, lib/)
└─ evals/
   ├─ datasets/ (labels only; media referenced by path, not committed)
   └─ run_highlight_eval.py
```

Test media is generated by FFmpeg (`testsrc`, `sine`, VFR variants) plus one short licensed/own speech sample, so CI never needs large files.

---

## 10. Implementation roadmap

Proposed changes to your ordering: **auth + rights capture pulled into Phase 1**, **signals added to Phase 3**, **eval harness in Phase 4**, **a minimal review UI in Phase 5** (you can't judge clips from JSON), and Phase 9 becomes "integrations" since basic permissions already exist.

| Phase | Scope | Key acceptance criteria |
|-------|-------|------------------------|
| **0 — Scaffolding** | Monorepo, compose (Postgres, Redis, api, workers, frontend), WSL2+GPU runbook, config/logging, Alembic baseline, CI-able `make test`, lint (ruff, mypy, eslint), ADRs 0001–0005. | `docker compose up` works; `/health` OK; GPU visible inside worker (`nvidia-smi`, `torch.cuda.is_available()`); ffmpeg with NVENC & libass present; tests + lint pass. |
| **1 — Ingestion** | Users/workspaces/creators/rights grants (minimal), resumable upload, sha256, ffprobe probe, validation, VFR detection, mezzanine/proxy generation, pipeline_runs/stage_runs + orchestrator skeleton, SSE status. Minimal upload page. | Upload 5 GB file with a simulated interruption and resume; metadata correct for MP4/MOV/MKV/VFR fixtures; corrupt/unsupported files fail with clear errors; re-running probe is a no-op; upload without a rights grant is rejected. |
| **2 — Audio & transcription** | Audio extract (16 kHz mono), WhisperX transcription + alignment, VAD, segments+words persistence, model/device config, VRAM release, benchmarks. | Word timestamps within ±100 ms on a hand-checked sample; 1 h audio processed without OOM on your GPU; CPU fallback works (slow); transcript API endpoint returns ranges. |
| **3 — Diarization, scenes, signals** | pyannote diarization + speaker assignment to words, PySceneDetect on proxy, keyframes, audio energy/loudness/laughter signals. | Speaker labels on a 2-person podcast fixture ≥ 85% word-level agreement with hand labels; scene cuts detected on synthetic cut fixture exactly; signals stored and plotted in a debug endpoint. |
| **4 — Highlight detection** | Windowing, prompts v1, Claude client (structured outputs, caching, batch option, retries, cost logging), validation, dedupe, pass 2 refinement, boundary snapping, scoring profiles, ranking, **eval harness + labeled set**. | All candidate times resolve from segment IDs and pass validation; no clip starts/ends mid-word on eval set; precision@10 baseline recorded; cost per hour of video recorded; dry-run cost estimate within ±30% of actual. |
| **5 — Clipping** | RenderSpec, FFmpeg filtergraph builder, NVENC with libx264 fallback, 9:16/16:9/1:1 with center crop + blurred-background letterbox option, loudness normalization, thumbnails, minimal clip list + player. | Frame-accurate start/end (±1 frame) on fixtures; A/V sync within 1 frame across a 60 s clip; output passes ffprobe checks per platform preset; renders are reproducible from `render_spec`. |
| **6 — Subtitles** | Word grouping (by pause/line length), ASS generation, word-highlight karaoke, ≥ 3 styles, configurable font/position/safe-area, SRT/VTT export, user subtitle edits, hook-text overlay, watermark. | Subtitle timing matches word timestamps; safe areas respected for each platform's UI overlays; edited subtitles re-render correctly; fonts bundled (no system font dependency). |
| **7 — Reframing** | 7a: face detection + tracking + smoothed piecewise crops, layout templates (facecam+gameplay split, two-speaker stack). 7b: speaker-aware (diarization ↔ face association), active-speaker switching. Fallback to center/letterbox when confidence is low. | On fixture set: face kept in frame ≥ 95% of frames; no crop jitter above threshold; layout templates render correctly; low-confidence cases fall back rather than produce bad crops. |
| **8 — Review dashboard** | Full review UX: candidate list with scores/rationale, player, trim editor with waveform and word-level transcript, subtitle editor, title edits, regenerate, approve/reject with reasons, bulk export. | Usability walkthrough; all actions persist and are audited; regenerate creates a new version without losing old ones. |
| **9 — Integrations & permissions** | Role permissions refinement, rights expiry enforcement, authorized platform integrations (OAuth to creators' own channels for source import), audit views. | Expired/revoked rights block render/export; integration tokens stored encrypted server-side, never sent to frontend. |
| **10 — Publishing prep & multi-channel** | Per-platform export bundles (video, cover, title, description, hashtags, attribution), channel management, scheduling metadata, basic analytics ingestion (later), auto-publish only after official API approval. | Export bundle validated against each platform's current specs; attribution present on every export. |

Each phase gets `docs/phases/phase-N.md` with: scope, plan, files, dependencies, acceptance criteria, tests, and **actual results** recorded before moving on.

---

## 11. Decisions (approved 2026-11-19)

1. GPU: **RTX 3060 12 GB** → WhisperX `large-v3` (int8_float16) is the default ASR model.
2. Runtime: **WSL2 + Docker Desktop with GPU** for backend and workers.
3. Language: **English only** at launch (one alignment model).
4. Location: separate `clipforge/` repository.
5. Users: **single operator** (one owner account). Workspace/membership tables stay in the schema for later but there is no multi-user UI. Source content from other streamers/creators is used → rights grants remain mandatory at upload.
6. Roadmap changes accepted.
7. **No paid API spend.** Highlight analysis must not use the pay-per-token Anthropic API by default. See ADR-0005: the LLM step sits behind a `HighlightAnalyzer` interface with a Claude Code (subscription) backend as the primary and a local model (Ollama) as a free fallback; the Anthropic API backend exists only as an optional, disabled-by-default adapter. §7.9–7.10 describe the API backend's cost if it is ever enabled. Pass 1/pass 2 request counts must be kept low to conserve plan limits (larger windows, top-N refinement only, vision off by default).
