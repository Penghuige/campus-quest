"""Owner commands on saved achievement versions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.review_schemas import (
    AchievementWorkflowResponse,
    SavedRevisionCommand,
    WithdrawCommand,
)
from app.modules.innovation.review_service import AchievementReviewService
from app.modules.innovation.router import Database, Student, _private_response

router = APIRouter(
    prefix="/ie/me/project-drafts/{project_id}/achievements/{achievement_id}",
    tags=["innovation review workflow"],
    dependencies=[Depends(_private_response)],
)


def get_review_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> AchievementReviewService:
    return AchievementReviewService(clock=clock)


Service = Annotated[AchievementReviewService, Depends(get_review_service)]


@router.get("/workflow", response_model=AchievementWorkflowResponse)
async def workflow(
    project_id: UUID,
    achievement_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementWorkflowResponse:
    return await service.workflow(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        context=AuditContext.from_request(request),
    )


@router.post("/submit", response_model=AchievementWorkflowResponse)
async def submit(
    project_id: UUID,
    achievement_id: UUID,
    payload: SavedRevisionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementWorkflowResponse:
    return await service.submit(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/withdraw", response_model=AchievementWorkflowResponse)
async def withdraw(
    project_id: UUID,
    achievement_id: UUID,
    payload: WithdrawCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementWorkflowResponse:
    return await service.withdraw(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/submit-update", response_model=AchievementWorkflowResponse)
async def submit_update(
    project_id: UUID,
    achievement_id: UUID,
    payload: SavedRevisionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> AchievementWorkflowResponse:
    return await service.submit_update(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
