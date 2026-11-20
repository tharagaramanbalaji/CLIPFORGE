"""Celery tasks: thin wrappers around the pipeline orchestrator (state lives in Postgres)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from clipforge.config import get_settings
from clipforge.db.models import StageRun
from clipforge.db.session import new_session
from clipforge.engine.common.gpu import detect_gpus, resolve_device
from clipforge.engine.media.tools import ffmpeg_capabilities
from clipforge.pipeline.handlers import HANDLERS
from clipforge.pipeline.orchestrator import Outcome, execute_stage
from clipforge.workers.celery_app import app

EXECUTE_STAGE = "clipforge.pipeline.execute_stage"


def environment_report() -> dict[str, Any]:
    settings = get_settings()
    gpus = detect_gpus()
    caps = ffmpeg_capabilities(settings.ffmpeg_bin)
    return {
        "gpus": [asdict(g) for g in gpus],
        "device": resolve_device(settings.device, gpus).value,
        "ffmpeg": {**asdict(caps), "ok": caps.ok},
    }


def celery_dispatch(sr: StageRun, queue: str) -> str | None:
    result = app.send_task(EXECUTE_STAGE, args=[str(sr.id)], queue=queue)
    return str(result.id)


@app.task(name="clipforge.system.ping")
def ping() -> str:
    return "pong"


@app.task(name="clipforge.system.environment_report")
def environment_report_task() -> dict[str, Any]:
    return environment_report()


@app.task(name=EXECUTE_STAGE, bind=True, max_retries=None)
def execute_stage_task(self: Any, stage_run_id: str) -> str:
    with new_session() as session:
        result = execute_stage(session, uuid.UUID(stage_run_id), celery_dispatch, HANDLERS, new_session)
    if result.outcome is Outcome.RETRY:
        queue = (self.request.delivery_info or {}).get("routing_key")
        raise self.retry(countdown=result.retry_in_s, queue=queue)
    return result.outcome.value
