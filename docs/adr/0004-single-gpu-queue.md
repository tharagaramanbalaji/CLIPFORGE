# ADR-0004: One GPU queue with concurrency 1 and explicit model load/unload

**Status:** Accepted, 2026-11-19

## Context
12 GB VRAM. WhisperX large-v3 (int8_float16) takes about 3–5 GB, pyannote 1–2 GB, and the face models are small. NVENC encoding uses a separate hardware engine but shares VRAM.

## Decision
- The `gpu` Celery queue runs with concurrency 1.
- Each stage loads its model inside a `gpu_model_scope()` context manager. The manager frees the model and calls `torch.cuda.empty_cache()` on exit.
- The `render` queue (NVENC) runs with concurrency 1–2 at first.
- Device, model size and compute type are set in config, with a CPU fallback.

## Consequences
GPU stages for different videos run one after another. That's acceptable for a single operator, and the GPU queue never runs out of memory.
