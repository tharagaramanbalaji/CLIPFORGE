"""Resumable chunked uploads (spec §8 "Uploads & media").

Protocol: create a session (needs a valid rights grant) -> PUT chunks in any order, any number of
times (idempotent; each chunk is written at its own offset in a preallocated file) -> GET the session
to see which chunks arrived (resume) -> complete, which moves the file into place and starts ingest.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db.models import (
    Artifact,
    ArtifactKind,
    MediaAsset,
    MediaStatus,
    PipelineKind,
    PipelineRun,
    Project,
    RightsGrant,
    UploadSession,
    User,
)
from clipforge.db.types import utcnow
from clipforge.errors import Conflict, Gone, NotFound, RightsNotGranted, ValidationFailed
from clipforge.pipeline import orchestrator
from clipforge.pipeline.orchestrator import Dispatch
from clipforge.storage import LocalStorage

ALLOWED_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".flv", ".ts", ".avi")
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def require_active_grant(
    session: Session, project: Project, grant_id: uuid.UUID, when: datetime
) -> RightsGrant:
    grant = session.get(RightsGrant, grant_id)
    if grant is None or grant.workspace_id != project.workspace_id:
        raise RightsNotGranted("Rights grant not found. Record the creator's permission before uploading.")
    if grant.creator_id != project.creator_id:
        raise RightsNotGranted("This rights grant belongs to a different creator than the project.")
    if not grant.is_active_at(when):
        raise RightsNotGranted("This rights grant is revoked, expired, or not yet valid.")
    return grant


def clean_filename(name: str) -> str:
    base = PurePath(name.replace("\\", "/")).name
    cleaned = _UNSAFE.sub("_", base).strip(" .") or "upload"
    return cleaned[:200]


def create_upload(
    session: Session,
    store: LocalStorage,
    settings: Settings,
    *,
    project: Project,
    user: User,
    filename: str,
    size_bytes: int,
    rights_grant_id: uuid.UUID,
) -> tuple[MediaAsset, UploadSession]:
    now = utcnow()
    grant = require_active_grant(session, project, rights_grant_id, now)
    name = clean_filename(filename)
    ext = PurePath(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationFailed(
            f"Unsupported file type '{ext or name}'. Upload one of: {', '.join(ALLOWED_EXTENSIONS)}."
        )
    if size_bytes <= 0:
        raise ValidationFailed("The file is empty.")
    if size_bytes > settings.max_upload_bytes:
        raise ValidationFailed(
            f"The file is {size_bytes / 1024**3:.1f} GB; "
            f"the limit is {settings.max_upload_bytes / 1024**3:.0f} GB."
        )
    asset = MediaAsset(
        project_id=project.id,
        rights_grant_id=grant.id,
        original_filename=name,
        size_bytes=size_bytes,
        status=MediaStatus.UPLOADING,
        uploaded_by_user_id=user.id,
    )
    session.add(asset)
    session.flush()
    up = UploadSession(
        media_asset_id=asset.id,
        total_bytes=size_bytes,
        chunk_size=settings.upload_chunk_size,
        received_bytes=0,
        parts=[],
        storage_key="",
        expires_at=now + timedelta(hours=settings.upload_session_ttl_hours),
    )
    session.add(up)
    session.flush()
    up.storage_key = f"uploads/{up.id}/data.part"
    path = store.local_path(up.storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.truncate(size_bytes)  # sparse preallocation; chunks are written at their offsets
    session.commit()
    return asset, up


def check_chunk(up: UploadSession, n: int, length: int, now: datetime) -> None:
    if up.completed_at is not None:
        raise Conflict("This upload is already complete.")
    if up.expires_at <= now:
        raise Gone("This upload session expired. Start the upload again.")
    if n < 0 or n >= up.total_chunks:
        raise ValidationFailed(f"Chunk {n} is out of range (0..{up.total_chunks - 1}).")
    expected = up.expected_chunk_len(n)
    if length != expected:
        raise ValidationFailed(f"Chunk {n} must be exactly {expected} bytes; got {length}.")


def write_chunk(store: LocalStorage, up: UploadSession, n: int, data: bytes, sha256_hex: str | None) -> None:
    if sha256_hex is not None and hashlib.sha256(data).hexdigest() != sha256_hex.lower():
        raise ValidationFailed(f"Chunk {n} checksum mismatch; resend it.")
    with store.local_path(up.storage_key).open("r+b") as f:
        f.seek(n * up.chunk_size)
        f.write(data)


def record_chunk(session: Session, upload_id: uuid.UUID, n: int) -> UploadSession:
    up = session.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if up is None:
        raise NotFound("Upload not found.")
    if n not in up.parts:
        up.parts = sorted([*up.parts, n])
        up.received_bytes = sum(up.expected_chunk_len(i) for i in up.parts)
    session.commit()
    return up


def complete_upload(
    session: Session,
    store: LocalStorage,
    *,
    upload_id: uuid.UUID,
    user: User,
    dispatch: Dispatch,
) -> tuple[MediaAsset, PipelineRun]:
    up = session.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if up is None:
        raise NotFound("Upload not found.")
    asset = session.get(MediaAsset, up.media_asset_id)
    assert asset is not None
    if up.completed_at is not None:
        run = orchestrator.latest_run(session, asset.id, PipelineKind.ANALYSIS)
        assert run is not None
        session.commit()
        return asset, run
    if up.expires_at <= utcnow():
        raise Gone("This upload session expired. Start the upload again.")
    missing = sorted(set(range(up.total_chunks)) - set(up.parts))
    if missing:
        raise Conflict(f"{len(missing)} chunk(s) still missing.", details={"missing": missing[:1000]})
    # The grant could have been revoked while the upload was running.
    require_active_grant(session, asset.project, asset.rights_grant_id, utcnow())

    ext = PurePath(asset.original_filename).suffix.lower()
    key = f"assets/{asset.id}/original/source{ext}"
    store.move(up.storage_key, key)
    session.add(
        Artifact(
            media_asset_id=asset.id,
            kind=ArtifactKind.ORIGINAL,
            storage_key=key,
            size_bytes=up.total_bytes,
            config_hash="original",
            meta={},
        )
    )
    up.completed_at = utcnow()
    asset.status = MediaStatus.VALIDATING
    run = orchestrator.create_run(session, asset, PipelineKind.ANALYSIS, requested_by=user.id)
    session.commit()
    run = orchestrator.advance(session, run.id, dispatch)
    return asset, run
