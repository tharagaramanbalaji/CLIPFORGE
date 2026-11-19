"""Phase 0 system tasks. Stage tasks arrive in Phase 1."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from clipforge.config import get_settings
from clipforge.engine.common.gpu import detect_gpus, resolve_device
from clipforge.engine.media.tools import ffmpeg_capabilities
from clipforge.workers.celery_app import app


def environment_report() -> dict[str, Any]:
    settings = get_settings()
    gpus = detect_gpus()
    caps = ffmpeg_capabilities(settings.ffmpeg_bin)
    return {
        "gpus": [asdict(g) for g in gpus],
        "device": resolve_device(settings.device, gpus).value,
        "ffmpeg": {**asdict(caps), "ok": caps.ok},
    }


@app.task(name="clipforge.system.ping")
def ping() -> str:
    return "pong"


@app.task(name="clipforge.system.environment_report")
def environment_report_task() -> dict[str, Any]:
    return environment_report()
