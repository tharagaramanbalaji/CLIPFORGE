"""pyannote.audio speaker diarization. The model is gated on Hugging Face: accept its terms and set
CLIPFORGE_HF_TOKEN. Audio is passed as an in-memory waveform (no torchcodec file decoding)."""

from __future__ import annotations

import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from clipforge.engine.common.gpu_memory import gpu_model_scope
from clipforge.engine.diarize.assign import SpeakerTurn, normalize_labels
from clipforge.errors import ClipForgeError


class DiarizationUnavailable(ClipForgeError):
    """Model can't be loaded (no token, terms not accepted, offline)."""


@dataclass(frozen=True, slots=True)
class DiarizeConfig:
    model: str = "pyannote/speaker-diarization-community-1"
    device: str = "cuda"
    min_speakers: int | None = None
    max_speakers: int | None = None
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_wav_tensor(path: Path) -> tuple[Any, int]:
    import numpy as np
    import torch

    with wave.open(str(path), "rb") as w:
        sr, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise ClipForgeError(f"expected 16-bit PCM WAV, got {width * 8}-bit")
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.reshape(-1, channels).mean(axis=1).astype(np.float32) / 32768.0
    return torch.from_numpy(audio).unsqueeze(0), sr


def diarize(
    wav_path: Path, cfg: DiarizeConfig, token: str | None
) -> tuple[list[SpeakerTurn], dict[str, Any]]:
    import torch
    from pyannote.audio import Pipeline

    t0 = time.monotonic()
    waveform, sr = load_wav_tensor(wav_path)
    with gpu_model_scope(enabled=cfg.device == "cuda") as mem:
        try:
            pipeline = Pipeline.from_pretrained(cfg.model, token=token)
        except Exception as exc:
            raise DiarizationUnavailable(
                f"Could not load {cfg.model}: {type(exc).__name__}. Check CLIPFORGE_HF_TOKEN and that "
                "the model's terms are accepted on huggingface.co.",
                details={"error": str(exc)[:300]},
            ) from exc
        if pipeline is None:
            raise DiarizationUnavailable(f"Could not load {cfg.model} (gated model: token or terms missing).")
        pipeline.to(torch.device(cfg.device))
        kwargs: dict[str, Any] = {}
        if cfg.min_speakers:
            kwargs["min_speakers"] = cfg.min_speakers
        if cfg.max_speakers:
            kwargs["max_speakers"] = cfg.max_speakers
        output = pipeline({"waveform": waveform, "sample_rate": sr}, **kwargs)
        # pyannote 4 returns an object with an overlap-free variant, which suits word assignment best
        annotation: Any = getattr(output, "exclusive_speaker_diarization", None) or getattr(
            output, "speaker_diarization", output
        )
        turns = [
            SpeakerTurn(round(seg.start * 1000), round(seg.end * 1000), str(label))
            for seg, _, label in annotation.itertracks(yield_label=True)
        ]
        del pipeline
    turns = normalize_labels(turns)
    wall = time.monotonic() - t0
    audio_s = waveform.shape[1] / sr
    return turns, {
        "speakers": len({t.speaker for t in turns}),
        "turns": len(turns),
        "audio_s": round(audio_s, 2),
        "wall_s": round(wall, 2),
        "realtime_factor": round(audio_s / wall, 1) if wall else None,
        "gpu": mem.as_dict(),
    }
