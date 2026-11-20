"""Resumable chunked upload endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from clipforge.api.deps import AppSettings, CurrentPrincipal, DbSession, Dispatcher, Store
from clipforge.api.routers.catalog import get_project
from clipforge.db.models import MediaAsset, UploadSession
from clipforge.db.types import utcnow
from clipforge.errors import NotFound, ValidationFailed
from clipforge.services import uploads as svc

router = APIRouter(tags=["uploads"])


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=1000)
    size_bytes: int = Field(gt=0)
    rights_grant_id: uuid.UUID


class UploadOut(BaseModel):
    upload_id: uuid.UUID
    media_id: uuid.UUID
    filename: str
    total_bytes: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    received_bytes: int
    complete: bool
    expires_at: datetime


class CompleteOut(BaseModel):
    media_id: uuid.UUID
    status: str
    pipeline_run_id: uuid.UUID


def _out(up: UploadSession, asset: MediaAsset) -> UploadOut:
    return UploadOut(
        upload_id=up.id,
        media_id=asset.id,
        filename=asset.original_filename,
        total_bytes=up.total_bytes,
        chunk_size=up.chunk_size,
        total_chunks=up.total_chunks,
        received_chunks=list(up.parts),
        received_bytes=up.received_bytes,
        complete=up.completed_at is not None,
        expires_at=up.expires_at,
    )


def _get_upload(db: DbSession, p: CurrentPrincipal, upload_id: uuid.UUID) -> tuple[UploadSession, MediaAsset]:
    up = db.get(UploadSession, upload_id)
    asset = db.get(MediaAsset, up.media_asset_id) if up else None
    if up is None or asset is None or asset.project.workspace_id != p.workspace_id:
        raise NotFound("Upload not found.")
    return up, asset


@router.post("/projects/{project_id}/uploads", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
def create_upload(
    project_id: uuid.UUID,
    body: UploadIn,
    db: DbSession,
    p: CurrentPrincipal,
    store: Store,
    settings: AppSettings,
) -> UploadOut:
    project = get_project(db, p, project_id)
    asset, up = svc.create_upload(
        db,
        store,
        settings,
        project=project,
        user=p.user,
        filename=body.filename,
        size_bytes=body.size_bytes,
        rights_grant_id=body.rights_grant_id,
    )
    return _out(up, asset)


@router.get("/uploads/{upload_id}", response_model=UploadOut)
def upload_status(upload_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> UploadOut:
    up, asset = _get_upload(db, p, upload_id)
    return _out(up, asset)


@router.put("/uploads/{upload_id}/chunks/{n}", response_model=UploadOut)
async def put_chunk(
    upload_id: uuid.UUID,
    n: int,
    request: Request,
    db: DbSession,
    p: CurrentPrincipal,
    store: Store,
    x_chunk_sha256: Annotated[str | None, Header()] = None,
) -> UploadOut:
    up, asset = await run_in_threadpool(_get_upload, db, p, upload_id)
    declared = request.headers.get("content-length")
    expected = up.expected_chunk_len(n) if 0 <= n < up.total_chunks else -1
    if declared is not None and declared.isdigit() and int(declared) != expected:
        svc.check_chunk(up, n, int(declared), utcnow())  # raises a precise error
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > up.chunk_size:
            raise ValidationFailed(f"Chunk {n} is larger than the chunk size ({up.chunk_size} bytes).")
    svc.check_chunk(up, n, len(body), utcnow())
    await run_in_threadpool(svc.write_chunk, store, up, n, bytes(body), x_chunk_sha256)
    up = await run_in_threadpool(svc.record_chunk, db, upload_id, n)
    return _out(up, asset)


@router.post("/uploads/{upload_id}/complete", response_model=CompleteOut)
def complete(
    upload_id: uuid.UUID, db: DbSession, p: CurrentPrincipal, store: Store, dispatch: Dispatcher
) -> CompleteOut:
    _get_upload(db, p, upload_id)
    asset, run = svc.complete_upload(db, store, upload_id=upload_id, user=p.user, dispatch=dispatch)
    return CompleteOut(media_id=asset.id, status=asset.status.value, pipeline_run_id=run.id)
