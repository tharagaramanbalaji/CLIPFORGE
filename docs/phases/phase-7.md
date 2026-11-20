# Phase 7 (slim): Layouts + Premiere export

**Status:** Built and tested on 2026-11-20. **Pending:** owner confirms the export imports correctly in Premiere Pro.
No LLM usage, no model downloads.

## Scope change (owner's decision, 2026-11-20)
The original Phase 7 was automatic reframing: face detection/tracking, smoothed crops, and speaker-aware switching.
After seeing that the built-in face detector gives false positives on gameplay, and since the owner edits in Premiere
Pro, it was slimmed to:
1. **Static layout templates** (regions chosen by the user, no detection, no jitter by construction).
2. **Export to Premiere** (also imports into DaVinci Resolve / Final Cut), so detailed framing happens in the editor.
Face tracking / speaker switching are deferred. They can come back later with a better detector (OpenCV YuNet,
~230 KB download, only with the owner's OK).

## What was built
| Area | Files |
|------|-------|
| Layouts | `engine/render/layouts.py`: **facecam_split** (facecam region → top band, default 35 %; gameplay region → rest, center kept), **two_stack** (left half on top, right half below, or custom regions), **blur_pad + captions below** (captions in the blurred band under the picture, off the game HUD). Facecam corner presets (top/bottom × left/right) or a custom normalized region; regions are validated |
| Caption placement | Layout-aware caption margin: under the picture (blur_pad), just below the seam (facecam_split), on the seam (two_stack); never below the platform's bottom safe edge |
| Premiere export | `engine/export/premiere.py`: Final Cut Pro 7 XML (xmeml v4) project: **"All highlights"** sequence (source resolution, ranked moments cut back-to-back, a marker per moment with title + score) and **one 9:16 sequence per clip** (1080×1920, Basic Motion scale to fill). Media linked by the owner's local path (URL-encoded, Windows drive paths), or by filename so Premiere asks to locate it once |
| API | Render options `frame_mode` = `facecam_split` / `two_stack` / `blur_pad` (+ `captions_below`), `facecam` (corner or `[x,y,w,h]`), `gameplay`, `top_ratio`; `GET /media/{id}/export/premiere?source_path=…` → ZIP (`clipforge_project.xml`, `captions/NN title.srt`, `README.txt`) |
| UI | Layout picker (with facecam corner), "captions under picture", **Export to Premiere** with optional original-file path |

## Acceptance criteria and results
| Criterion (slimmed) | Result |
|-----------|--------|
| Layout templates render correctly | ✅ Synthetic source with known colour regions: facecam_split top band = the red top-right "facecam" (RGB 252,0,0), gameplay band keeps the green center; two_stack top = yellow left half, bottom = magenta right half |
| Captions moved off the picture | ✅ With "captions below", caption top at y = 1332 px, below the picture's bottom edge (1264) and above the safe edge (1480) |
| No crop jitter | ✅ By construction: layout crops are static for the whole clip |
| Low-confidence fallback | n/a in the slim version (no detection); the default stays fit + blurred background |
| Editor export is valid | ✅ XML parses; sequences, frame-exact in/out (e.g. 1:32.0–1:50.0 → frames 2760–3300 at 30 fps), back-to-back timeline, markers, single media definition, 29.97 → `ntsc TRUE`, Windows path encoding (spaces, curly apostrophe), 1080×1920 + 266.67 % fill scale; API returns the ZIP with XML + SRT + README |
| Imports in Premiere | ⏳ Owner to check with `evals/results/eafc25_premiere_test.zip` (built for the EA FC test video in Downloads) |
| Tests / lint / types | ✅ 158 passed; ruff clean; mypy strict clean (98 files); frontend builds |

## Auto-edit (added 2026-11-20, owner: "do the editing yourself")
DaVinci Resolve isn't installed (and as far as we know only Resolve Studio allows outside control), and Premiere has no
official remote-control API, so ClipForge does the editing itself and Premiere stays optional.

| Area | Files |
|------|-------|
| Facecam finder | `engine/render/facecam.py`: two bundled Haar cascades find face *clusters* (a facecam stays put, game faces move); the overlay box comes from its **static border lines** in the time-averaged gradient (sides near a frame edge snap to the edge); presence per frame = that border is visible (ratio vs frame median > 8, near-black frames ignored). No download |
| Per-clip auto layout | `auto_layout`: facecam on screen ≥ 95 % of the clip → facecam on top; < 20 % or no plausible box (> 50 % of frame) → whole picture + captions below; otherwise **auto_switch**: facecam layout while it's on screen, whole picture otherwise (≥ 2 s segments, short gaps bridged), with captions in one spot that works for both |
| Render | `frame_mode="auto"` is resolved in the render stage from the clip's own proxy frames; the stored spec records the decision, box, share and intervals |
| API / UI | `POST /media/{id}/auto-edit` {count, preset, subtitle_style, hook_text, watermark}: clips from the top-N highlights (re-renders existing ones), each rendered with auto layout. UI: **Auto-edit top N** button; "auto" is now the default framing |

Results on the owner's EA FC video (an edited video: the facecam is bottom-right for ~4.5 min, sometimes zoomed or
moved, then gone):
- Facecam box found: [0.745, 0.556] → bottom-right corner, matching the frame exactly. Border strength 15–45 with the
  facecam on, 1–2.5 with it off.
- 1:27 clip: switches facecam layout → whole picture (while the edit moves the facecam) → facecam layout.
  3:59 clip: facecam layout, then whole picture when the camera zooms full-screen. 0:00 / 5:12 / 6:10: no facecam →
  whole picture, captions below. All 6 renders pass platform checks (NVENC, ~6 s each including detection).
- The earlier "no facecam" call was wrong: the detector had hit the real facecam, but the hits were dismissed as noise.

## Findings
- Bug caught by the API test: thumbnails rebuilt a bare spec and lost the layout (facecam_split thumbnail failed). Fixed.
- FCP7 XML is the most widely supported interchange format for Premiere; FCPXML (newer) is Final Cut–specific and
  Premiere doesn't import it natively.

## How to use in Premiere
1. In ClipForge: Find highlights → (optional) Make clips → **Export to Premiere** (paste the original file's path to
   skip relinking).
2. Unzip, then in Premiere **File → Import → clipforge_project.xml**.
3. "All highlights" has every moment cut with markers; each "(9x16)" sequence is one vertical clip.
4. Captions: **File → Import** the matching `.srt` from `captions/` and drag it onto that 9×16 sequence.
