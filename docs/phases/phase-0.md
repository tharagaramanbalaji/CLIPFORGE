# Phase 0: Architecture & scaffolding

**Status:** Complete. Docker-stack acceptance checks passed on 2026-11-19: `/ready` ok (db + redis), RTX 3060 visible in worker-gpu and worker-render, FFmpeg 6.1.1 with NVENC h264/hevc + libass, 29/29 tests pass in Linux.

## Scope
Repository layout, configuration, logging, error types, subprocess wrapper, storage interface, DB base + Alembic baseline, Celery app with queues, health/readiness API, GPU/FFmpeg capability report, Docker Compose stack, frontend shell, ADRs 0001–0005.

## What was built
| Area | Files |
|------|-------|
| Config / logging / errors | `backend/src/clipforge/{config,log,errors}.py` |
| Engine common | `engine/common/{process,tempfiles,gpu}.py`, `engine/media/tools.py` |
| Storage | `storage/{base,local}.py`: key validation, atomic writes |
| DB | `db/{base,session}.py`, `alembic/` with an empty baseline revision `0001` |
| Workers | `workers/celery_app.py` (queues gpu/cpu/render/llm), `workers/tasks.py` |
| API | `api/main.py`, `api/routers/health.py` (`/api/v1/health`, `/api/v1/ready`) |
| CLI | `python -m clipforge.cli env` |
| Infra | `backend/Dockerfile` (CUDA 12.6 + cuDNN, Ubuntu 24.04, FFmpeg), `docker-compose.yml`, `.env.example` |
| Frontend | Vite + React 18 + TS + Tailwind 4 + TanStack Query; status page |

## Acceptance criteria and results (2026-11-19)
| Criterion | Result |
|-----------|--------|
| Unit tests pass | ✅ 29 passed (Windows, Python 3.12.9) |
| Lint (`ruff check`, `ruff format --check`) | ✅ clean |
| Type check (`mypy --strict`) | ✅ no issues (26 files) |
| Engine doesn't import FastAPI/Celery/DB | ✅ enforced by `tests/unit/test_architecture.py` |
| API serves `/health`; `/ready` reports dependency state without leaking details | ✅ verified live with uvicorn (503 without DB/Redis, as expected) |
| GPU detected; FFmpeg has NVENC + libass (host) | ✅ RTX 3060 12288 MiB, driver 610.88; FFmpeg 9.0.2 with h264/hevc_nvenc, libx264, libass |
| Frontend type-checks and builds | ✅ `npm run build` |
| `docker compose up` works; `/ready` = ready; GPU and NVENC visible in `worker-gpu`/`worker-render` | ✅ 2026-11-19: `/ready` ok (database, redis); RTX 3060 + NVENC h264/hevc + libass in both workers; 29/29 tests pass inside the container |
| Alembic `upgrade head` runs against Postgres | ✅ runs as the `migrate` service |

## Notes and decisions
- The frontend runs on the host with Vite's proxy instead of in a container: faster reloads, one less image.
- `httpx` vs `httpx2` deprecation warning from Starlette's TestClient. Harmless; revisit when bumping FastAPI.
- No ESLint yet. `tsc --strict` is the frontend gate until the UI grows (Phase 5/8).
