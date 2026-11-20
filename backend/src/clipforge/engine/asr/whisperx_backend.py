"""WhisperX backend: faster-whisper (CTranslate2) batched transcription with VAD, then wav2vec2 forced
alignment for word timings. Models are loaded per call inside `gpu_model_scope` and always released
(RTX 3060 VRAM is the constraint, ADR-0004). On CUDA OOM the batch size is halved and retried.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any

from clipforge.engine.asr.base import AsrConfig, AsrProgress, TranscriptResult
from clipforge.engine.asr.postprocess import build_segments
from clipforge.engine.common.gpu_memory import gpu_model_scope, is_cuda_oom, release_gpu_memory
from clipforge.errors import Canceled, ClipForgeError
from clipforge.log import get_logger

log = get_logger(__name__)


class AsrOutOfMemory(ClipForgeError):
    retryable = True


def _batch_sizes(start: int) -> list[int]:
    sizes, b = [], max(1, start)
    while b >= 1:
        sizes.append(b)
        if b == 1:
            break
        b //= 2
    return sizes


class WhisperXTranscriber:
    engine = "whisperx"

    def transcribe(
        self, audio_path: Path, cfg: AsrConfig, on_progress: AsrProgress | None = None
    ) -> TranscriptResult:
        import whisperx

        def progress(f: float) -> None:
            if on_progress is not None and not on_progress(f):
                raise Canceled("canceled")

        t0 = time.monotonic()
        audio = whisperx.load_audio(str(audio_path))
        audio_s = len(audio) / 16_000
        progress(0.02)
        timings: dict[str, float] = {}
        on_gpu = cfg.device == "cuda"

        with gpu_model_scope(enabled=on_gpu) as asr_mem:
            t = time.monotonic()
            model = whisperx.load_model(
                cfg.model,
                cfg.device,
                compute_type=cfg.compute_type,
                language=cfg.language,
                vad_method=cfg.vad_method,
                vad_options={
                    "vad_onset": cfg.vad_onset,
                    "vad_offset": cfg.vad_offset,
                    "chunk_size": cfg.chunk_size_s,
                },
                asr_options={"condition_on_previous_text": False},
            )
            timings["asr_load_s"] = time.monotonic() - t
            progress(0.08)
            supports_cb = "progress_callback" in inspect.signature(model.transcribe).parameters
            raw: dict[str, Any] | None = None
            used_batch = cfg.batch_size
            t = time.monotonic()
            for batch in _batch_sizes(cfg.batch_size):
                try:
                    kwargs: dict[str, Any] = {
                        "batch_size": batch,
                        "language": cfg.language,
                        "chunk_size": cfg.chunk_size_s,
                    }
                    if supports_cb:
                        kwargs["progress_callback"] = lambda pct: progress(0.08 + 0.72 * float(pct) / 100)
                    raw = model.transcribe(audio, **kwargs)
                    used_batch = batch
                    break
                except Canceled:
                    raise
                except Exception as exc:
                    if not on_gpu or not is_cuda_oom(exc):
                        raise
                    log.warning("asr_oom_reducing_batch", batch=batch)
                    release_gpu_memory()
            if raw is None:
                raise AsrOutOfMemory("Speech recognition ran out of GPU memory even with batch size 1.")
            timings["asr_s"] = time.monotonic() - t
            del model
        progress(0.8)

        segments_raw: list[dict[str, Any]] = list(raw.get("segments", []))
        align_mem = None
        if cfg.align and segments_raw:
            with gpu_model_scope(enabled=on_gpu) as align_mem:
                t = time.monotonic()
                align_model, metadata = whisperx.load_align_model(
                    language_code=cfg.language, device=cfg.device
                )
                aligned = whisperx.align(
                    segments_raw, align_model, metadata, audio, cfg.device, return_char_alignments=False
                )
                segments_raw = list(aligned.get("segments", []))
                timings["align_s"] = time.monotonic() - t
                del align_model
        progress(0.97)

        segments = build_segments(segments_raw, start_lead_ms=cfg.start_lead_ms if cfg.align else 0)
        total_s = time.monotonic() - t0
        metrics: dict[str, Any] = {
            "audio_s": round(audio_s, 2),
            "wall_s": round(total_s, 2),
            "realtime_factor": round(audio_s / total_s, 1) if total_s > 0 else None,
            "batch_size": used_batch,
            "segments": len(segments),
            "words": sum(len(s.words) for s in segments),
            "flagged_repetitive": sum("repetitive" in s.flags for s in segments),
            **{k: round(v, 2) for k, v in timings.items()},
            "gpu_asr": asr_mem.as_dict(),
            "gpu_align": align_mem.as_dict() if align_mem else None,
        }
        return TranscriptResult(
            engine=self.engine,
            model=cfg.model,
            language=cfg.language,
            segments=tuple(segments),
            metrics=metrics,
        )
