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
Integration tests (`-m integration`) need Postgres/Redis and run inside the compose stack.
