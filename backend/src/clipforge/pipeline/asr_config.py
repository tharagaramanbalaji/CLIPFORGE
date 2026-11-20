"""ASR settings -> AsrConfig.

`asr_settings_fingerprint()` is what goes into the transcribe stage's idempotency key. It is computed from
settings only (the API process that computes keys has no GPU). `asr_config_for_this_host()` resolves the
device where the stage actually runs (the GPU worker) and falls back to the CPU model when no GPU is usable.
"""

from __future__ import annotations

from typing import Any

from clipforge.config import Device, get_settings
from clipforge.engine.asr.base import AsrConfig


def asr_settings_fingerprint() -> dict[str, Any]:
    s = get_settings()
    return {
        "device": s.device.value,
        "model": s.asr_model,
        "compute_type": s.asr_compute_type,
        "cpu_model": s.asr_cpu_model,
        "cpu_compute_type": s.asr_cpu_compute_type,
        "vad": s.asr_vad_method,
        "version": AsrConfig().version,
    }


def cuda_usable() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def asr_config_for_this_host(force_device: str | None = None) -> AsrConfig:
    s = get_settings()
    wanted = force_device or s.device.value
    use_cuda = wanted == "cuda" or (wanted == Device.AUTO.value and cuda_usable())
    if use_cuda:
        return AsrConfig(
            model=s.asr_model,
            device="cuda",
            compute_type=s.asr_compute_type,
            batch_size=s.asr_batch_size,
            vad_method=s.asr_vad_method,
        )
    return AsrConfig(
        model=s.asr_cpu_model,
        device="cpu",
        compute_type=s.asr_cpu_compute_type,
        batch_size=s.asr_cpu_batch_size,
        vad_method=s.asr_vad_method,
    )
