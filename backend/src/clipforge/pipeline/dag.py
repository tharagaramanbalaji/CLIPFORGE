"""Static stage graphs per pipeline kind. Later phases append stages (diarize, scenes, signals, ...)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clipforge.db.models import PipelineKind, StageName


@dataclass(frozen=True, slots=True)
class StageDef:
    name: StageName
    queue: str
    deps: tuple[StageName, ...] = ()
    max_attempts: int = 3
    version: int = 1  # bump when the stage's logic changes so old results are not reused
    # Settings-derived config that changes the stage's output (e.g. ASR model). Part of the idempotency key,
    # so changing CLIPFORGE_ASR_MODEL re-runs transcription instead of reusing the old transcript.
    default_config: Callable[[], dict[str, Any]] = field(default=dict)


def _asr_config() -> dict[str, Any]:
    from clipforge.pipeline.asr_config import asr_settings_fingerprint

    return asr_settings_fingerprint()


def _diarize_config() -> dict[str, Any]:
    from clipforge.config import get_settings

    s = get_settings()
    # Token presence is part of the key: adding a token later re-runs diarization
    # instead of reusing the "no token" result.
    has_token = s.hf_token is not None and bool(s.hf_token.get_secret_value())
    return {"model": s.diarize_model, "has_token": has_token, "v": 1}


def _scenes_config() -> dict[str, Any]:
    from clipforge.config import get_settings

    return {"threshold": get_settings().scene_threshold, "v": 1}


def _signals_config() -> dict[str, Any]:
    from clipforge.config import get_settings

    return {"events_model": get_settings().audio_events_model, "v": 1}


# Mezzanine/proxy go to the render queue: that worker has the GPU, so NVENC does the encodes.
# audio_extract and proxy both depend only on the mezzanine, so they run in parallel.
# GPU stages (transcribe, diarize, signals) run one at a time on the gpu queue, so models never share VRAM.
ANALYSIS_DAG: tuple[StageDef, ...] = (
    StageDef(StageName.PROBE, "cpu"),
    StageDef(StageName.MEZZANINE, "render", deps=(StageName.PROBE,)),
    StageDef(StageName.PROXY, "render", deps=(StageName.MEZZANINE,)),
    StageDef(StageName.AUDIO_EXTRACT, "cpu", deps=(StageName.MEZZANINE,)),
    StageDef(
        StageName.TRANSCRIBE,
        "gpu",
        deps=(StageName.AUDIO_EXTRACT,),
        max_attempts=2,
        default_config=_asr_config,
    ),
    StageDef(
        StageName.DIARIZE, "gpu", deps=(StageName.TRANSCRIBE,), max_attempts=2, default_config=_diarize_config
    ),
    StageDef(StageName.SCENES, "cpu", deps=(StageName.PROXY,), default_config=_scenes_config),
    StageDef(
        StageName.SIGNALS, "gpu", deps=(StageName.TRANSCRIBE,), max_attempts=2, default_config=_signals_config
    ),
)

# Highlights run (per media, on demand). LLM stages go to the `llm` queue (I/O bound: they wait on Claude Code
# or Ollama). The run's config pins transcript, backend, models and prompt versions, so they're in every key.
HIGHLIGHTS_DAG: tuple[StageDef, ...] = (
    StageDef(StageName.HIGHLIGHT_WINDOWS, "llm", max_attempts=4),
    StageDef(StageName.HIGHLIGHT_REFINE, "llm", deps=(StageName.HIGHLIGHT_WINDOWS,), max_attempts=4),
    StageDef(StageName.RANK, "cpu", deps=(StageName.HIGHLIGHT_REFINE,)),
)

# One render run per clip version (render queue: the GPU worker, NVENC). Its config holds the version id.
RENDER_DAG: tuple[StageDef, ...] = (StageDef(StageName.RENDER, "render", max_attempts=2),)

DAGS: dict[PipelineKind, tuple[StageDef, ...]] = {
    PipelineKind.RENDER: RENDER_DAG,
    PipelineKind.ANALYSIS: ANALYSIS_DAG,
    PipelineKind.HIGHLIGHTS: HIGHLIGHTS_DAG,
}


def dag_for(kind: PipelineKind) -> tuple[StageDef, ...]:
    return DAGS[kind]


def stage_def(kind: PipelineKind, stage: StageName) -> StageDef:
    for sd in DAGS[kind]:
        if sd.name == stage:
            return sd
    raise KeyError(f"{stage} not in {kind} DAG")


def validate_dag(dag: tuple[StageDef, ...]) -> None:
    """Every dependency must appear earlier (the orchestrator relies on topological order)."""
    seen: set[StageName] = set()
    for sd in dag:
        missing = [d for d in sd.deps if d not in seen]
        if missing:
            raise ValueError(f"stage {sd.name} depends on {missing} which are not defined before it")
        seen.add(sd.name)


for _dag in DAGS.values():
    validate_dag(_dag)
