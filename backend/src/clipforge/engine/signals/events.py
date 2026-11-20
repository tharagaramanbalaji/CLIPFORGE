"""Audio event detection with an AudioSet classifier (AST, ~350 MB, public on Hugging Face).

Produces per-window probabilities for groups of AudioSet classes that matter for clipping:
laughter, excitement (cheering/screaming/shouting/applause) and music (useful later as a copyright flag).
"""

from __future__ import annotations

import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from clipforge.engine.common.gpu_memory import gpu_model_scope
from clipforge.engine.signals.audio import Series

GROUPS: dict[str, tuple[str, ...]] = {
    "laughter": ("Laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle", "Baby laughter"),
    "excitement": (
        "Cheering",
        "Applause",
        "Screaming",
        "Shout",
        "Yell",
        "Battle cry",
        "Children shouting",
        "Crowd",
        "Whoop",
    ),
    "music": ("Music",),
}


@dataclass(frozen=True, slots=True)
class EventsConfig:
    model: str = "MIT/ast-finetuned-audioset-10-10-0.4593"
    device: str = "cuda"
    window_ms: int = 2000
    hop_ms: int = 1000
    batch_size: int = 32
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def class_indices(id2label: dict[int, str]) -> dict[str, list[int]]:
    by_name = {name: i for i, name in id2label.items()}
    return {group: [by_name[n] for n in names if n in by_name] for group, names in GROUPS.items()}


def detect_events(wav: Path, cfg: EventsConfig) -> tuple[list[Series], dict[str, Any]]:
    import numpy as np
    import torch
    from transformers import ASTFeatureExtractor, ASTForAudioClassification

    t0 = time.monotonic()
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    win, hop = sr * cfg.window_ms // 1000, sr * cfg.hop_ms // 1000
    n = max(0, (len(audio) - 1) // hop + 1)
    out: dict[str, list[float]] = {g: [] for g in GROUPS}
    with gpu_model_scope(enabled=cfg.device == "cuda") as mem:
        extractor = ASTFeatureExtractor.from_pretrained(cfg.model)
        model = ASTForAudioClassification.from_pretrained(cfg.model).to(cfg.device).eval()
        if cfg.device == "cuda":
            model = model.half()
        idx = class_indices({int(k): v for k, v in model.config.id2label.items()})
        for b in range(0, n, cfg.batch_size):
            chunk = []
            for i in range(b, min(n, b + cfg.batch_size)):
                seg = audio[i * hop : i * hop + win]
                if len(seg) < win:
                    seg = np.pad(seg, (0, win - len(seg)))
                chunk.append(seg)
            feats = extractor(chunk, sampling_rate=sr, return_tensors="pt")["input_values"].to(cfg.device)
            if cfg.device == "cuda":
                feats = feats.half()
            with torch.inference_mode():
                probs = torch.sigmoid(model(feats).logits.float()).cpu().numpy()
            for group, cols in idx.items():
                vals = probs[:, cols].max(axis=1) if cols else np.zeros(len(chunk))
                out[group].extend(round(float(v), 4) for v in vals)
        del model
    wall = time.monotonic() - t0
    audio_s = len(audio) / sr
    series = [Series(g, cfg.hop_ms, vals, "probability") for g, vals in out.items()]
    return series, {
        "windows": n,
        "wall_s": round(wall, 2),
        "realtime_factor": round(audio_s / wall, 1) if wall else None,
        "gpu": mem.as_dict(),
    }
