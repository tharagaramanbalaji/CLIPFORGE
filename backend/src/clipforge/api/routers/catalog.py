"""Creators, rights grants and projects. Every query is scoped to the caller's workspace."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from clipforge.api.deps import CurrentPrincipal, DbSession
from clipforge.db.models import Creator, Project, RightsBasis, RightsGrant
from clipforge.db.types import utcnow
from clipforge.errors import NotFound, ValidationFailed

router = APIRouter(tags=["catalog"])


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- creators --------------------------------------------------------------------------------------


class CreatorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    platform_handles: dict[str, str] = {}
    default_speaker_count: int | None = Field(default=None, ge=1, le=20)
    notes: str | None = None


class CreatorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    platform_handles: dict[str, str] | None = None
    default_speaker_count: int | None = Field(default=None, ge=1, le=20)
    notes: str | None = None


class CreatorOut(_Out):
    id: uuid.UUID
    name: str
    platform_handles: dict[str, Any]
    default_speaker_count: int | None
    notes: str | None
    created_at: datetime


def get_creator(db: DbSession, p: CurrentPrincipal, creator_id: uuid.UUID) -> Creator:
    creator = db.get(Creator, creator_id)
    if creator is None or creator.workspace_id != p.workspace_id:
        raise NotFound("Creator not found.")
    return creator


@router.get("/creators", response_model=list[CreatorOut])
def list_creators(db: DbSession, p: CurrentPrincipal) -> list[Creator]:
    return list(
        db.scalars(select(Creator).where(Creator.workspace_id == p.workspace_id).order_by(Creator.name))
    )


@router.post("/creators", response_model=CreatorOut, status_code=status.HTTP_201_CREATED)
def create_creator(body: CreatorIn, db: DbSession, p: CurrentPrincipal) -> Creator:
    creator = Creator(workspace_id=p.workspace_id, **body.model_dump())
    db.add(creator)
    db.commit()
    return creator


@router.get("/creators/{creator_id}", response_model=CreatorOut)
def read_creator(creator_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> Creator:
    return get_creator(db, p, creator_id)


@router.patch("/creators/{creator_id}", response_model=CreatorOut)
def update_creator(creator_id: uuid.UUID, body: CreatorPatch, db: DbSession, p: CurrentPrincipal) -> Creator:
    creator = get_creator(db, p, creator_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(creator, k, v)
    db.commit()
    return creator


# --- rights grants ---------------------------------------------------------------------------------


class GrantScope(BaseModel):
    platforms: list[str] = Field(default_factory=lambda: ["youtube_shorts", "instagram_reels", "tiktok"])
    monetization_allowed: bool = False
    attribution_text: str | None = None
    contains_copyrighted_music: bool = False


class GrantIn(BaseModel):
    basis: RightsBasis
    scope: GrantScope = Field(default_factory=GrantScope)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    evidence_note: str | None = Field(default=None, max_length=5000)


class GrantOut(_Out):
    id: uuid.UUID
    creator_id: uuid.UUID
    basis: RightsBasis
    scope: dict[str, Any]
    valid_from: datetime
    valid_until: datetime | None
    evidence_note: str | None
    revoked_at: datetime | None
    is_active: bool = False


def _grant_out(g: RightsGrant) -> GrantOut:
    out = GrantOut.model_validate(g)
    out.is_active = g.is_active_at(utcnow())
    return out


@router.get("/creators/{creator_id}/rights-grants", response_model=list[GrantOut])
def list_grants(creator_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> list[GrantOut]:
    creator = get_creator(db, p, creator_id)
    grants = db.scalars(
        select(RightsGrant)
        .where(RightsGrant.creator_id == creator.id)
        .order_by(RightsGrant.created_at.desc())
    )
    return [_grant_out(g) for g in grants]


@router.post(
    "/creators/{creator_id}/rights-grants", response_model=GrantOut, status_code=status.HTTP_201_CREATED
)
def create_grant(creator_id: uuid.UUID, body: GrantIn, db: DbSession, p: CurrentPrincipal) -> GrantOut:
    creator = get_creator(db, p, creator_id)
    valid_from = body.valid_from or utcnow()
    if body.valid_until is not None and body.valid_until <= valid_from:
        raise ValidationFailed("valid_until must be after valid_from.")
    grant = RightsGrant(
        workspace_id=p.workspace_id,
        creator_id=creator.id,
        basis=body.basis,
        scope=body.scope.model_dump(),
        valid_from=valid_from,
        valid_until=body.valid_until,
        evidence_note=body.evidence_note,
        recorded_by_user_id=p.user.id,
    )
    db.add(grant)
    db.commit()
    return _grant_out(grant)


@router.post("/rights-grants/{grant_id}/revoke", response_model=GrantOut)
def revoke_grant(grant_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> GrantOut:
    grant = db.get(RightsGrant, grant_id)
    if grant is None or grant.workspace_id != p.workspace_id:
        raise NotFound("Rights grant not found.")
    if grant.revoked_at is None:
        grant.revoked_at = utcnow()
        db.commit()
    return _grant_out(grant)


# --- projects --------------------------------------------------------------------------------------


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    creator_id: uuid.UUID


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class ProjectOut(_Out):
    id: uuid.UUID
    name: str
    creator_id: uuid.UUID
    status: str
    archived_at: datetime | None
    created_at: datetime


def get_project(db: DbSession, p: CurrentPrincipal, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.workspace_id != p.workspace_id:
        raise NotFound("Project not found.")
    return project


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: DbSession, p: CurrentPrincipal) -> list[Project]:
    return list(
        db.scalars(
            select(Project).where(Project.workspace_id == p.workspace_id).order_by(Project.created_at.desc())
        )
    )


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectIn, db: DbSession, p: CurrentPrincipal) -> Project:
    creator = get_creator(db, p, body.creator_id)
    project = Project(workspace_id=p.workspace_id, creator_id=creator.id, name=body.name, settings={})
    db.add(project)
    db.commit()
    return project


@router.get("/projects/{project_id}", response_model=ProjectOut)
def read_project(project_id: uuid.UUID, db: DbSession, p: CurrentPrincipal) -> Project:
    return get_project(db, p, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: uuid.UUID, body: ProjectPatch, db: DbSession, p: CurrentPrincipal) -> Project:
    project = get_project(db, p, project_id)
    if body.name is not None:
        project.name = body.name
    if body.archived is not None:
        project.archived_at = utcnow() if body.archived else None
        project.status = "archived" if body.archived else "active"
    db.commit()
    return project
