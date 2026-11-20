# ClipForge

AI-assisted engine that turns long-form videos (streams, podcasts, interviews) into short vertical clips.
For personal use; all source content must be your own or used with the creator's permission (tracked per upload).

- Spec: [docs/ENGINEERING_SPEC.md](docs/ENGINEERING_SPEC.md)
- Decisions: [docs/adr/](docs/adr/)
- Phase status: [docs/phases/](docs/phases/)

## Layout
```
backend/   FastAPI API, Celery workers, clipping engine (Python 3.12)
frontend/  React + Vite + TypeScript + Tailwind
docs/      spec, ADRs, phase reports, runbooks
```

## Run the full stack (WSL2 + Docker, see docs/runbooks/wsl2-gpu-setup.md)
```bash
cp .env.example .env
docker compose up --build -d
cd frontend && npm install && npm run dev      # http://localhost:5173
```
On first visit, create the owner account (registration closes after that: single operator).
Then: add a creator, record a rights grant for them, create a project, upload a video.
After changing worker code, restart workers (`docker compose restart worker-cpu worker-render worker-gpu`);
the API reloads by itself.

## Tests inside the stack
```bash
docker compose exec api pytest                                  # unit tests
docker compose exec api pytest -m "integration or media"        # real Postgres + FFmpeg (uses a throwaway *_test DB)
docker compose exec api python scripts/e2e_ingest.py --big-gb 5 # fresh install only: real workers, 5 GB resume test
docker compose exec worker-gpu pytest -m gpu -s tests/gpu       # real ASR models: word-timing accuracy on the GPU
```

## Disk space
Docker's disk lives at `D:\DockerData` and only grows. Clean up after rebuilds with
`docker builder prune -f`. Budget about 25 GB for images + models, plus about 2.5× the size of each video
being processed (original + working copy + preview + audio). See [phase-2.md](docs/phases/phase-2.md).

## Backend checks on Windows (no Docker needed)
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check src tests alembic
.\.venv\Scripts\python -m mypy
.\.venv\Scripts\python -m clipforge.cli env     # GPU + FFmpeg capability report
```
Integration and media tests (`-m integration`, `-m media`) need Postgres/FFmpeg and run inside the compose stack.
