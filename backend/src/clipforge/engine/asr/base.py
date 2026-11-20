"""Transcriber protocol and result types. Times are integer milliseconds on the mezzanine timeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

AsrDevice = Literal["cuda", "cpu"]


@dataclass(frozen=True, slots=True)
class AsrConfig:
    model: str = "large-v3"
    device: AsrDevice = "cuda"
    compute_type: str = "int8_float16"
    batch_size: int = 16
    language: str = "en"
    vad_method: str = "pyannote"
    vad_onset: float = 0.5
    vad_offset: float = 0.363
    chunk_size_s: int = 30
    align: bool = True
    start_lead_ms: int = 50  # correction for CTC alignment's late word onsets (see postprocess)
    version: int = 2  # bump when postprocessing changes (invalidates cached transcripts)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Word:
    w: str
    s: int  # start ms
    e: int  # end ms
    p: float | None = None  # alignment score 0..1 (None when interpolated)

    def as_dict(self) -> dict[str, Any]:
        return {"w": self.w, "s": self.s, "e": self.e, "p": self.p}


@dataclass(frozen=True, slots=True)
class Segment:
    idx: int
    start_ms: int
    end_ms: int
    text: str
    words: tuple[Word, ...]
    confidence: float | None = None  # mean word alignment score
    flags: tuple[str, ...] = ()  # e.g. "repetitive" (likely hallucination), "interpolated_words"


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    engine: str
    model: str
    language: str
    segments: tuple[Segment, ...]
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return sum(len(s.words) for s in self.segments)

    def as_json(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model": self.model,
            "language": self.language,
            "metrics": self.metrics,
            "segments": [
                {
                    "idx": s.idx,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "text": s.text,
                    "confidence": s.confidence,
                    "flags": list(s.flags),
                    "words": [w.as_dict() for w in s.words],
                }
                for s in self.segments
            ],
        }


# Called with fraction done (0..1); return False to cancel.
AsrProgress = Callable[[float], bool]


class Transcriber(Protocol):
    def transcribe(
        self, audio_path: Path, cfg: AsrConfig, on_progress: AsrProgress | None = None
    ) -> TranscriptResult: ...
