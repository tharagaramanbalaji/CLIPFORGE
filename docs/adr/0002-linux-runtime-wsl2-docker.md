# ADR-0002: Backend and workers run on Linux (WSL2 + Docker), not native Windows

**Status:** Accepted, 2026-11-19

## Context
The dev machine is Windows 11 with an RTX 3060 (12 GB). Celery has no supported Windows worker pool. pyannote, WhisperX and CUDA builds are tested mainly on Linux, and production will be Linux.

## Decision
All backend processes (API, workers, Postgres, Redis) run in Docker containers on Docker Desktop's WSL2 backend with NVIDIA GPU passthrough. Media data lives on the WSL2 filesystem (a Docker named volume), not under `C:\`, because cross-filesystem I/O is 5–10× slower.

Engine code stays OS-neutral (pathlib, argument-list subprocess calls), so unit tests also run natively on Windows.

## Consequences
- One-time setup: WSL2 + Docker Desktop (see `docs/runbooks/wsl2-gpu-setup.md`).
- A single CUDA-based backend image is reused for every process role.
