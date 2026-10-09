"""Bearer-only owner-scoped private child routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.achievement_schemas import (
    AchievementDraftCreate,
    AchievementDraftListResponse,
    AchievementDraftResponse,
    AchievementDraftUpdate,
)
from app.modules.innovation.achievement_service import AchievementDraftService
from app.modules.innovation.router import Database, Student, _private_response

router = APIRouter(
    prefix="/ie/me/project-drafts/{project_id}/achievements",
    tags=["innovation achievement drafts"],
    dependencies=[Depends(_private_response)],
)


def get_achievement_draft_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> AchievementDraftService:
    return AchievementDraftService(clock=clock)


Service = Annotated[AchievementDraftService, Depends(get_achievement_draft_service)]


@router.post("", response_model=AchievementDraftResponse, status_code=201)
async def create(
    project_id: UUID,
    payload: AchievementDraftCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementDraftResponse:
    result, created = await service.create(
        db,
        actor=actor,
        project_id=project_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
    response.status_code = 201 if created else 200
    return result


@router.get("", response_model=AchievementDraftListResponse)
async def list_owned(
    project_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AchievementDraftListResponse:
    return await service.list_owned(
        db,
        actor=actor,
        project_id=project_id,
        limit=limit,
        offset=offset,
        context=AuditContext.from_request(request),
    )


@router.get("/{achievement_id}", response_model=AchievementDraftResponse)
async def get_owned(
    project_id: UUID,
    achievement_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementDraftResponse:
    return await service.get_owned(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        context=AuditContext.from_request(request),
    )


@router.patch("/{achievement_id}", response_model=AchievementDraftResponse)
async def update(
    project_id: UUID,
    achievement_id: UUID,
    payload: AchievementDraftUpdate,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementDraftResponse:
    return await service.update(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
