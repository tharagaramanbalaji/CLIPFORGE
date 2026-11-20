"""FastAPI application factory.  Run: uvicorn clipforge.api.main:app"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from clipforge import __version__
from clipforge.api.routers import (
    analysis,
    auth,
    catalog,
    clips,
    events,
    health,
    highlights,
    media,
    review,
    uploads,
)
from clipforge.config import Environment, Settings, get_settings
from clipforge.errors import (
    ClipForgeError,
    ConfigError,
    Conflict,
    Gone,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from clipforge.log import configure_logging, get_logger

API_PREFIX = "/api/v1"
log = get_logger(__name__)

_STATUS: tuple[tuple[type[ClipForgeError], int], ...] = (
    (NotFound, 404),
    (PermissionDenied, 403),
    (Conflict, 409),
    (Gone, 410),
    (ValidationFailed, 422),
)


async def _clipforge_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ClipForgeError)
    code = next((c for cls, c in _STATUS if isinstance(exc, cls)), 500)
    if code == 500:
        log.error("unhandled_clipforge_error", error=exc.message, type=type(exc).__name__)
        return JSONResponse({"error": {"code": "internal", "message": "Internal error."}}, status_code=500)
    details = {k: v for k, v in exc.details.items() if k != "args"}
    return JSONResponse(
        {"error": {"code": type(exc).__name__, "message": exc.message, "details": details}}, status_code=code
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.env is Environment.PROD and settings.uses_default_secret:
        raise ConfigError("CLIPFORGE_SECRET_KEY must be set to a random value in prod.")
    app = FastAPI(title="ClipForge API", version=__version__)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(ClipForgeError, _clipforge_error)
    for module in (health, auth, catalog, uploads, media, analysis, highlights, clips, review, events):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
