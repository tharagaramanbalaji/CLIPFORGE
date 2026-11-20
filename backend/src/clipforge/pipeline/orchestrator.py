"""Pipeline orchestrator (spec §6.1).

`advance()` is idempotent and serialized per run by a Postgres advisory lock. It queues every pending
stage whose dependencies are done, reusing ("skipping") stages whose idempotency key already succeeded
in an earlier run. `execute_stage()` runs one stage and is what the Celery task calls.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from clipforge.db.models import (
    MediaAsset,
    MediaStatus,
    PipelineKind,
    PipelineRun,
    RunStatus,
    StageRun,
    StageStatus,
)
from clipforge.db.types import utcnow
from clipforge.engine.common.hashing import stable_hash
from clipforge.errors import Canceled, ClipForgeError
from clipforge.log import get_logger
from clipforge.pipeline.dag import dag_for, stage_def
from clipforge.services import events

log = get_logger(__name__)

# (stage_run, queue) -> task id. Celery in production, an in-memory list in tests.
Dispatch = Callable[[StageRun, str], str | None]


class Outcome(StrEnum):
    DONE = "done"
    RETRY = "retry"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class StageResult:
    outcome: Outcome
    retry_in_s: float = 0.0


def _workspace_id(session: Session, asset_id: uuid.UUID) -> uuid.UUID:
    asset = session.get(MediaAsset, asset_id)
    assert asset is not None
    return asset.project.workspace_id


def _emit_stage(session: Session, run: PipelineRun, sr: StageRun) -> None:
    events.publish(
        _workspace_id(session, run.media_asset_id),
        "stage",
        run_id=str(run.id),
        media_id=str(run.media_asset_id),
        stage=sr.stage.value,
        status=sr.status.value,
        progress=sr.progress,
    )


def _emit_run(session: Session, run: PipelineRun) -> None:
    events.publish(
        _workspace_id(session, run.media_asset_id),
        "run",
        run_id=str(run.id),
        media_id=str(run.media_asset_id),
        kind=run.kind.value,
        status=run.status.value,
    )


def create_run(
    session: Session,
    asset: MediaAsset,
    kind: PipelineKind,
    *,
    config: dict[str, Any] | None = None,
    requested_by: uuid.UUID | None = None,
) -> PipelineRun:
    cfg = config or {}
    run = PipelineRun(
        media_asset_id=asset.id,
        kind=kind,
        status=RunStatus.PENDING,
        requested_by=requested_by,
        config=cfg,
        config_hash=stable_hash(cfg),
    )
    session.add(run)
    session.flush()
    for sd in dag_for(kind):
        session.add(StageRun(pipeline_run_id=run.id, stage=sd.name, max_attempts=sd.max_attempts))
    session.flush()
    session.refresh(run)
    return run


def _lock_run(session: Session, run_id: uuid.UUID) -> None:
    # Advisory lock keyed by the low 63 bits of the run id; released at transaction end.
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": run_id.int & ((1 << 63) - 1)})


def _run_level(config: dict[str, Any]) -> dict[str, Any]:
    """Run-wide settings (top-level keys that aren't per-stage dicts) are part of every stage's key."""
    return {k: v for k, v in config.items() if not isinstance(v, dict)}


def idempotency_key(run: PipelineRun, sr: StageRun, dep_keys: list[str], stage_config: dict[str, Any]) -> str:
    sd = stage_def(run.kind, sr.stage)
    return stable_hash("stage", sr.stage.value, sd.version, str(run.media_asset_id), dep_keys, stage_config)


def _finish_run(
    session: Session, run: PipelineRun, status: RunStatus, error: dict[str, Any] | None = None
) -> None:
    run.status = status
    run.finished_at = utcnow()
    run.error = error
    asset = session.get(MediaAsset, run.media_asset_id)
    assert asset is not None
    if run.kind is PipelineKind.ANALYSIS and asset.status is not MediaStatus.ARCHIVED:
        if status is RunStatus.SUCCEEDED:
            asset.status, asset.error = MediaStatus.READY, None
        elif status is RunStatus.FAILED:
            asset.status, asset.error = MediaStatus.FAILED, error


def advance(session: Session, run_id: uuid.UUID, dispatch: Dispatch) -> PipelineRun:
    """Queue every runnable stage; finish the run when all stages are done. Commits."""
    _lock_run(session, run_id)
    run = session.get(PipelineRun, run_id, populate_existing=True)
    if run is None:
        raise ClipForgeError(f"pipeline run {run_id} not found")
    session.refresh(run, ["stages"])
    if run.status.is_terminal:
        session.commit()
        return run

    by_stage = {sr.stage: sr for sr in run.stages}
    to_dispatch: list[tuple[StageRun, str]] = []
    changed: list[StageRun] = []
    for sd in dag_for(run.kind):
        sr = by_stage[sd.name]
        if sr.status is not StageStatus.PENDING:
            continue
        deps = [by_stage[d] for d in sd.deps]
        if not all(d.status.is_done for d in deps):
            continue
        sr.idempotency_key = idempotency_key(
            run,
            sr,
            [d.idempotency_key or "" for d in deps],
            {**sd.default_config(), **run.config.get(sd.name.value, {}), "_run": _run_level(run.config)},
        )
        prior = session.scalar(
            select(StageRun)
            .where(
                StageRun.idempotency_key == sr.idempotency_key,
                StageRun.status == StageStatus.SUCCEEDED,
                StageRun.id != sr.id,
            )
            .order_by(StageRun.finished_at.desc())
            .limit(1)
        )
        if prior is not None:
            sr.status, sr.progress, sr.finished_at = StageStatus.SKIPPED, 1.0, utcnow()
            sr.metrics = {"reused_stage_run_id": str(prior.id)}
        else:
            sr.status = StageStatus.QUEUED
            to_dispatch.append((sr, sd.queue))
        changed.append(sr)

    if run.status is RunStatus.PENDING:
        run.status, run.started_at = RunStatus.RUNNING, utcnow()
    if all(sr.status.is_done for sr in run.stages):
        _finish_run(session, run, RunStatus.SUCCEEDED)
    session.commit()

    for sr in changed:
        _emit_stage(session, run, sr)
    if run.status.is_terminal:
        _emit_run(session, run)
    for sr, queue in to_dispatch:
        task_id = dispatch(sr, queue)
        if task_id:
            sr.celery_task_id = task_id
    if to_dispatch:
        session.commit()
    return run


# --- stage execution -----------------------------------------------------------------------------

StageHandler = Callable[["StageContext"], dict[str, Any]]


@dataclass
class StageContext:
    session: Session
    run: PipelineRun
    stage_run: StageRun
    asset: MediaAsset
    report_progress: Callable[[float], bool]  # returns False when the run was canceled


def _progress_reporter(
    session_factory: Callable[[], Session], run: PipelineRun, sr: StageRun, workspace_id: uuid.UUID
) -> Callable[[float], bool]:
    """Writes progress in its own short transaction (at most every 2 s) and reports cancellation."""
    import time

    last = [0.0]

    def report(fraction: float) -> bool:
        now = time.monotonic()
        if now - last[0] < 2.0 and fraction < 1.0:
            return True
        last[0] = now
        with session_factory() as s:
            s.query(StageRun).filter(StageRun.id == sr.id).update({"progress": round(fraction, 4)})
            status = s.scalar(select(PipelineRun.status).where(PipelineRun.id == run.id))
            s.commit()
        events.publish(
            workspace_id,
            "stage",
            run_id=str(run.id),
            media_id=str(run.media_asset_id),
            stage=sr.stage.value,
            status="running",
            progress=round(fraction, 4),
        )
        return status is not RunStatus.CANCELED

    return report


def _error_payload(exc: BaseException, stage: str) -> dict[str, Any]:
    if isinstance(exc, ClipForgeError):
        details = {k: v for k, v in exc.details.items() if k != "args"}  # never leak full argv/paths
        return {"stage": stage, "message": exc.message, "type": type(exc).__name__, "details": details}
    return {"stage": stage, "message": "Internal error while processing this stage.", "type": "InternalError"}


def execute_stage(
    session: Session,
    stage_run_id: uuid.UUID,
    dispatch: Dispatch,
    handlers: dict[str, StageHandler],
    session_factory: Callable[[], Session],
) -> StageResult:
    sr = session.scalar(
        select(StageRun)
        .where(StageRun.id == stage_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if sr is None or sr.status not in (StageStatus.QUEUED, StageStatus.RUNNING):
        session.commit()
        return StageResult(Outcome.IGNORED)
    run = sr.run
    asset = session.get(MediaAsset, run.media_asset_id)
    assert asset is not None
    workspace_id = asset.project.workspace_id
    if run.status is RunStatus.CANCELED:
        sr.status, sr.finished_at = StageStatus.CANCELED, utcnow()
        session.commit()
        return StageResult(Outcome.DONE)

    sr.status, sr.attempt, sr.started_at = StageStatus.RUNNING, sr.attempt + 1, utcnow()
    sr.worker_host, sr.error = socket.gethostname(), None
    session.commit()
    _emit_stage(session, run, sr)

    ctx = StageContext(session, run, sr, asset, _progress_reporter(session_factory, run, sr, workspace_id))
    try:
        metrics = handlers[sr.stage.value](ctx)
    except Canceled:
        session.rollback()
        sr = session.get(StageRun, stage_run_id, populate_existing=True)
        assert sr is not None
        sr.status, sr.finished_at = StageStatus.CANCELED, utcnow()
        session.commit()
        _emit_stage(session, sr.run, sr)
        return StageResult(Outcome.DONE)
    except Exception as exc:
        session.rollback()
        sr = session.get(StageRun, stage_run_id, populate_existing=True)
        assert sr is not None
        run = sr.run
        retryable = isinstance(exc, ClipForgeError) and exc.retryable and sr.attempt < sr.max_attempts
        payload = _error_payload(exc, sr.stage.value)
        if isinstance(exc, ClipForgeError):
            log.warning(
                "stage_failed",
                stage=sr.stage.value,
                run_id=str(run.id),
                error=exc.message,
                retryable=retryable,
                attempt=sr.attempt,
            )
        else:
            log.exception("stage_crashed", stage=sr.stage.value, run_id=str(run.id))
        sr.error = payload
        if retryable:
            sr.status = StageStatus.QUEUED
            session.commit()
            return StageResult(Outcome.RETRY, retry_in_s=min(300.0, 10.0 * 2 ** (sr.attempt - 1)))
        sr.status, sr.finished_at = StageStatus.FAILED, utcnow()
        _lock_run(session, run.id)
        session.refresh(run)
        if not run.status.is_terminal:
            for other in run.stages:
                if other.status in (StageStatus.PENDING, StageStatus.QUEUED):
                    other.status = StageStatus.CANCELED
            _finish_run(session, run, RunStatus.FAILED, payload)
        session.commit()
        _emit_stage(session, run, sr)
        _emit_run(session, run)
        return StageResult(Outcome.DONE)

    sr.status, sr.progress, sr.finished_at = StageStatus.SUCCEEDED, 1.0, utcnow()
    sr.metrics = metrics
    session.commit()
    _emit_stage(session, run, sr)
    advance(session, run.id, dispatch)
    return StageResult(Outcome.DONE)


def cancel_run(session: Session, run: PipelineRun) -> PipelineRun:
    _lock_run(session, run.id)
    session.refresh(run)
    if not run.status.is_terminal:
        for sr in run.stages:
            if sr.status in (StageStatus.PENDING, StageStatus.QUEUED):
                sr.status = StageStatus.CANCELED
        run.status, run.finished_at = RunStatus.CANCELED, utcnow()
        asset = session.get(MediaAsset, run.media_asset_id)
        if asset is not None and asset.status in (MediaStatus.UPLOADED, MediaStatus.VALIDATING):
            asset.status = MediaStatus.FAILED
            asset.error = {"message": "Processing was canceled.", "type": "Canceled"}
    session.commit()
    _emit_run(session, run)
    return run


def retry_run(session: Session, run: PipelineRun, dispatch: Dispatch) -> PipelineRun:
    """Reset failed/canceled stages to pending and continue from where the run stopped."""
    _lock_run(session, run.id)
    session.refresh(run)
    if run.status not in (RunStatus.FAILED, RunStatus.CANCELED):
        session.commit()
        return run
    for sr in run.stages:
        if sr.status in (StageStatus.FAILED, StageStatus.CANCELED):
            sr.status, sr.attempt, sr.error, sr.progress = StageStatus.PENDING, 0, None, 0.0
    run.status, run.finished_at, run.error = RunStatus.RUNNING, None, None
    asset = session.get(MediaAsset, run.media_asset_id)
    if asset is not None and asset.status is MediaStatus.FAILED:
        asset.status, asset.error = MediaStatus.VALIDATING, None
    session.commit()
    return advance(session, run.id, dispatch)


def latest_run(session: Session, asset_id: uuid.UUID, kind: PipelineKind) -> PipelineRun | None:
    return session.scalar(
        select(PipelineRun)
        .where(PipelineRun.media_asset_id == asset_id, PipelineRun.kind == kind)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
