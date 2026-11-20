"""Stage name -> handler. Kept separate so stage modules can import each other's helpers freely."""

from __future__ import annotations

from clipforge.pipeline import stages, stages_analysis, stages_highlights, stages_render
from clipforge.pipeline.orchestrator import StageHandler

HANDLERS: dict[str, StageHandler] = {
    "probe": stages.probe_stage,
    "mezzanine": stages.mezzanine_stage,
    "proxy": stages.proxy_stage,
    "audio_extract": stages.audio_extract_stage,
    "transcribe": stages.transcribe_stage,
    "diarize": stages_analysis.diarize_stage,
    "scenes": stages_analysis.scenes_stage,
    "signals": stages_analysis.signals_stage,
    "highlight_windows": stages_highlights.highlight_windows_stage,
    "highlight_refine": stages_highlights.highlight_refine_stage,
    "rank": stages_highlights.rank_stage,
    "render": stages_render.render_stage,
}
