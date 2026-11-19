"""FastAPI application factory.  Run: uvicorn clipforge.api.main:app"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from clipforge import __version__
from clipforge.api.routers import health
from clipforge.config import Settings, get_settings
from clipforge.log import configure_logging

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app = FastAPI(title="ClipForge API", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router, prefix=API_PREFIX)
    return app


app = create_app()
