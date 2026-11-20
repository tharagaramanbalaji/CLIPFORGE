# Phase 8: Review dashboard

**Status:** Built, tested and accepted by the owner on 2026-11-20.
No LLM usage, no model downloads, no new dependencies.

## What was built
| Area | Files |
|------|-------|
| Review state | `db/models/clips.py`: clip `review` (pending / approved / rejected), `reject_reason`, `reviewed_by`, `reviewed_at`. Migration `0008` (existing clips default to pending) |
| Audit trail | `AuditEvent` table (workspace, actor, action, entity, data, time). Recorded in the same transaction as the change: `clip.create`, `clip.update` (changed fields), `clip.render` (version, preset, framing, caption style), `clip.subtitles_edit` / `clip.subtitles_reset`, `clip.approve` (which version), `clip.reject` (reason), `clip.reopen`, `media.export_approved` |
| API | `api/routers/review.py`: `POST /clips/{id}/approve` (409 until a version is rendered), `POST /clips/{id}/reject {reason}` (1–500 chars), `POST /clips/{id}/reopen`, `GET /audit?entity_id=`, `GET /media/{id}/waveform?from_ms&to_ms&points` (peaks from the 16 kHz analysis audio), `GET /media/{id}/preview` (seekable 540p proxy), `GET /media/{id}/approved.zip` (approved clips' current MP4 + SRT + `clips.csv`) |
| UI | `frontend/src/Review.tsx`, opened with **Review** on a ready video. Left: **Clips** (filter by review state) and **Candidates** (rank, score, reasoning, per-signal scores, **Make clip**). Right: title, player (rendered version or source preview that loops the in/out range), hook text, render options, **Save edits**, **Re-render (new version)**, **Approve** / **Reject with reason** / **Reopen**. Tabs: **trim** (waveform with draggable in/out handles, ±0.5 s nudges, word-level transcript: click a word to start there, shift-click to end there), **captions** (the Phase 6 editor), **versions** (every version with Play / MP4 / SRT), **history** (audit trail). Header: **Download N approved** |

## Acceptance criteria and results
| Criterion | Result |
|-----------|--------|
| Usability walkthrough | ✅ In the browser on a 50 s cut of the EA FC video (throwaway account, wiped afterwards): opened Review → clip → waveform drawn (62 % of the canvas lit) and 18 transcript words shown → clicked a word to move the start 6.0 → 2.1 s, nudged the end +0.5 s, renamed it → Render (v1, ~10 s) → nudged again → Re-render (v2) → Approve ("Download 1 approved", ZIP 200) → Reopen → Reject "Too long intro" (shown on the clip) → history tab |
| All actions persist | ✅ After a page reload: title, trimmed range, rejected + reason, both versions all came back from the server |
| All actions are audited | ✅ History showed, newest first: reject (reason), reopen, approve (version id), render v2, update (new end), render v1, update (title, start, end), create |
| Regenerate keeps old versions | ✅ v1 and v2 both `ready` and both still stream (HTTP 206) after v2 became current; integration test checks the same |
| Bulk export | ✅ Integration test: ZIP holds `01 <title>.mp4` + SRT + `clips.csv` for approved clips only; approving before any render → 409; empty reject reason → 422 |
| Tests / lint / types | ✅ 163 passed (not gpu/slow); ruff clean (src, tests); mypy strict clean (100 files); frontend builds |

## Notes
- Approve records which version was approved. Re-rendering after approval keeps the clip approved, and the ZIP takes
  the current version; reopen first if a re-render should be reviewed again.
- The waveform covers the clip ±10 s. The trim editor keeps clips ≥ 1 s; the server checks bounds again.
- Candidates show the reasoning stored in Phase 4 (refine rationale → judge reason → why interesting), so reviewing
  costs no LLM calls.
