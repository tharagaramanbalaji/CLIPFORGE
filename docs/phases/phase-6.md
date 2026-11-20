# Phase 6: Subtitles

**Status:** Complete. All acceptance criteria passed on 2026-11-20. No LLM usage in this phase.

## What was built
| Area | Files |
|------|-------|
| Grouping | `engine/subtitles/grouping.py`: words → captions, splitting on max words, line width (from font size and safe width), pauses ≥ 350 ms, and sentence ends; captions linger 0.4 s (never into the next one) |
| Styles | `engine/subtitles/styles.py`: **bold_pop** (uppercase, thick outline, active word yellow and enlarged), **clean_box** (white on translucent box, active word green), **karaoke** (words fill cyan as spoken) |
| Safe areas | Per preset (1080×1920 values): shorts top 180 / bottom 440 / right 150; reels 220 / 460 / 150; tiktok 180 / 500 / 170; plus wide and square. Captions sit above the bottom safe edge, the hook inside the top one, the watermark bottom-left above the app overlays |
| ASS | `engine/subtitles/ass.py`: one Dialogue event per active word (highlight switches on each word's own start), hook text (first 3 s, top box), watermark; text sanitised (no override braces); SRT/VTT export |
| Fonts | **Bundled** in `backend/assets/fonts/` (DejaVu Sans + Bold, license included). Renders run libass with `fontsdir` and an **empty fontconfig** (`fonts.conf`), so system fonts are invisible |
| Render | `RenderSpec.extra["subtitles"]` holds style, clip-relative words, hook text and watermark, so subtitle renders stay reproducible from the spec |
| Schema (Alembic `0007`) | `subtitle_tracks` (a clip's edited words; without a row the transcript is used); also adds the `clips.current_version_id` foreign key that `0006` missed |
| API | `GET/PUT/DELETE /clips/{id}/subtitles` (word-level edit, validation, reset to transcript), render options `subtitle_style` / `hook_text` / `watermark`, `GET /clip-versions/{id}/subtitles.srt|.vtt` (exactly what was burned in) |
| UI | Caption style, hook on/off and watermark in render options; per-clip caption editor (a changed word count spreads that caption's time evenly); SRT download |

## Acceptance criteria and results
| Criterion | Result |
|-----------|--------|
| Subtitle timing matches word timestamps | ✅ Each caption appears on **exactly** the frame its first word starts (frames 30 and 96 for words at 1.0 s / 3.2 s) and disappears at last word + 0.4 s (±1 frame). The active-word highlight switches within 1 frame of every word start (measured by tracking the yellow word in the rendered video) |
| Safe areas respected per platform | ✅ Rendered text bounding box (captions + hook + watermark, deliberately long words) inside the safe box for **shorts, reels, tiktok, square × all 3 styles** (12 renders) |
| Edited subtitles re-render correctly | ✅ Changed text and a moved word produce a different render, with the moved caption appearing at its new time (frame 81 = 2.7 s); unchanged words re-render bit-identical. API: edit → render → the spec and SRT contain the edit; reset returns to the transcript |
| Fonts bundled (no system font dependency) | ✅ With the bundled folder: text renders (libass picks DejaVuSans-Bold). With an **empty** font folder under the same isolation: **no text at all**, proving there's no system-font fallback |
| Real footage | ✅ Owner's EA FC moment (5:12.9–5:29.5) with real transcript words, bold_pop + hook text + watermark: NVENC 4.4 s, all platform checks pass |
| Tests / lint / types | ✅ 152 passed (16 subtitle render tests + API test); ruff clean; mypy strict clean (95 files); frontend builds |

## Findings and decisions
- **The first font test couldn't prove anything**: libass logs font *names*, and the container also has DejaVu
  installed. Fixed by isolating fontconfig for every subtitle render and testing with and without the bundle.
- With fit + blurred background, captions sit over the bottom of the game picture (EA FC's player-name HUD). Placing
  them in the blurred band is a Phase 7 layout option.
- ASS times have 10 ms resolution; at 30/60 fps that's below one frame.
- DejaVu is functional but plain. Nicer display fonts (e.g. Montserrat/Anton, open licenses) are a drop-in: put the
  files in `backend/assets/fonts/` and change `FONT_NAME`/styles (not downloaded without your OK).

## How to run
```bash
docker compose exec api pytest -m media tests/media/test_subtitles.py -s
```
