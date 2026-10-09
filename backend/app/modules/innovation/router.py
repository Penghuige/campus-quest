"""Private management API. Bearer-only, like existing task business routes.

Refresh-cookie authentication and its CSRF rules remain in identity/auth;
these endpoints never derive authority from an ambient cookie.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.session import get_db_session
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_student_actor,
)
from app.modules.identity.events import Actor
from app.modules.innovation.schemas import (
    ProjectDraftCreate,
    ProjectDraftListResponse,
    ProjectDraftResponse,
    ProjectDraftUpdate,
)
from app.modules.innovation.service import ProjectDraftService


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    prefix="/ie/me/project-drafts",
    tags=["innovation drafts"],
    dependencies=[Depends(_private_response)],
)
Database = Annotated[AsyncSession, Depends(get_db_session)]
Student = Annotated[Actor, Depends(require_active_student_actor)]


def get_project_draft_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> ProjectDraftService:
    return ProjectDraftService(clock=clock)


Service = Annotated[ProjectDraftService, Depends(get_project_draft_service)]


@router.post("", response_model=ProjectDraftResponse, status_code=201)
async def create_draft(
    payload: ProjectDraftCreate,
    response: Response,
    db: Database,
    actor: Student,
    service: Service,
) -> ProjectDraftResponse:
    draft, created = await service.create(db, actor=actor, payload=payload)
    response.status_code = 201 if created else 200
    return draft


@router.get("", response_model=ProjectDraftListResponse)
async def list_drafts(
    db: Database,
    actor: Student,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectDraftListResponse:
    return await service.list_owned(db, actor=actor, limit=limit, offset=offset)


@router.get("/{draft_id}", response_model=ProjectDraftResponse)
async def read_draft(
    draft_id: UUID,
    db: Database,
    actor: Student,
    service: Service,
) -> ProjectDraftResponse:
    return await service.get_owned(db, actor=actor, draft_id=draft_id)


@router.patch("/{draft_id}", response_model=ProjectDraftResponse)
async def update_draft(
    draft_id: UUID,
    payload: ProjectDraftUpdate,
    db: Database,
    actor: Student,
    service: Service,
) -> ProjectDraftResponse:
    return await service.update(db, actor=actor, draft_id=draft_id, payload=payload)
