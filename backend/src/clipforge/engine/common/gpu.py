"""GPU discovery without importing torch (torch arrives in Phase 2)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from clipforge.config import Device
from clipforge.engine.common.process import run_command
from clipforge.errors import ExternalToolError


@dataclass(frozen=True, slots=True)
class GpuInfo:
    name: str
    memory_total_mib: int
    driver_version: str


def parse_nvidia_smi_csv(text: str) -> list[GpuInfo]:
    gpus: list[GpuInfo] = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        name, mem, driver = parts
        try:
            gpus.append(GpuInfo(name=name, memory_total_mib=int(mem), driver_version=driver))
        except ValueError:
            continue
    return gpus


def detect_gpus(nvidia_smi: str = "nvidia-smi") -> list[GpuInfo]:
    if shutil.which(nvidia_smi) is None:
        return []
    try:
        res = run_command(
            [nvidia_smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            timeout_s=15,
        )
    except ExternalToolError:
        return []
    return parse_nvidia_smi_csv(res.stdout)


def resolve_device(setting: Device, gpus: list[GpuInfo] | None = None) -> Device:
    """AUTO -> CUDA when a GPU is visible, else CPU. Explicit settings are respected."""
    if setting is not Device.AUTO:
        return setting
    found = detect_gpus() if gpus is None else gpus
    return Device.CUDA if found else Device.CPU
