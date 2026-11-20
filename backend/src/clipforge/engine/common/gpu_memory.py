"""GPU memory scope: guarantees models are released after a stage and measures device memory.

torch is imported lazily so importing the engine stays cheap and works without CUDA.
`torch.cuda.mem_get_info` reports *device* usage, which also covers CTranslate2 (faster-whisper)
allocations that torch's own allocator statistics would miss.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GpuMemoryReport:
    device_total_mib: int | None = None
    used_before_mib: int | None = None
    used_peak_mib: int | None = None
    used_after_mib: int | None = None
    samples: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def leaked_mib(self) -> int | None:
        if self.used_before_mib is None or self.used_after_mib is None:
            return None
        return self.used_after_mib - self.used_before_mib

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_total_mib": self.device_total_mib,
            "used_before_mib": self.used_before_mib,
            "used_peak_mib": self.used_peak_mib,
            "used_after_mib": self.used_after_mib,
            "leaked_mib": self.leaked_mib,
        }


def _used_mib() -> tuple[int, int] | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        return (total - free) // 2**20, total // 2**20
    except Exception:
        return None


def release_gpu_memory() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()  # type: ignore[no-untyped-call]
    except Exception:
        pass


@contextmanager
def gpu_model_scope(enabled: bool = True, sample_interval_s: float = 0.5) -> Iterator[GpuMemoryReport]:
    """Use as `with gpu_model_scope() as mem: model = load(); ...; del model`.

    On exit (also on errors) collects garbage and empties the CUDA cache; the report records device memory
    before, at peak (sampled in a background thread) and after release.
    """
    report = GpuMemoryReport()
    if not enabled:
        yield report
        return
    first = _used_mib()
    if first is not None:
        report.used_before_mib, report.device_total_mib = first
        report.used_peak_mib = first[0]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(sample_interval_s):
            cur = _used_mib()
            if cur is not None:
                report.samples += 1
                report.used_peak_mib = max(report.used_peak_mib or 0, cur[0])

    sampler = threading.Thread(target=sample, daemon=True)
    if first is not None:
        sampler.start()
    try:
        yield report
    finally:
        stop.set()
        if sampler.is_alive():
            sampler.join(timeout=2)
        release_gpu_memory()
        last = _used_mib()
        if last is not None:
            report.used_after_mib = last[0]


def is_cuda_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "out of memory" in text or "outofmemory" in text or "cublas_status_alloc_failed" in text
