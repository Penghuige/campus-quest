"""Bearer-only, private owner recruitment routes; transaction ownership in service."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.recruitment_schemas import (
    RecruitmentActionResult,
    RecruitmentDraftCreate,
    RecruitmentDraftListResponse,
    RecruitmentDraftResponse,
    RecruitmentDraftUpdate,
    RecruitmentVersionCommand,
    RevokeRecruitmentContactCommand,
)
from app.modules.innovation.recruitment_service import RecruitmentService
from app.modules.innovation.router import Database, Student, _private_response

router = APIRouter(
    prefix="/ie/me/project-drafts/{project_id}/recruitments",
    tags=["innovation recruitment"],
    dependencies=[Depends(_private_response)],
)


def get_recruitment_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> RecruitmentService:
    return RecruitmentService(clock=clock)


Service = Annotated[RecruitmentService, Depends(get_recruitment_service)]


@router.post("", status_code=201)
async def create(
    project_id: UUID,
    payload: RecruitmentDraftCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentDraftResponse:
    result, created = await service.create(
        db,
        actor=actor,
        project_id=project_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
    response.status_code = 201 if created else 200
    return result


@router.get("")
async def list_owned(
    project_id: UUID,
    db: Database,
    actor: Student,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecruitmentDraftListResponse:
    return await service.list_owned(
        db, actor=actor, project_id=project_id, limit=limit, offset=offset
    )


@router.get("/{recruitment_id}")
async def get_owned(
    project_id: UUID,
    recruitment_id: UUID,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentDraftResponse:
    return await service.get_owned(
        db, actor=actor, project_id=project_id, recruitment_id=recruitment_id
    )


@router.patch("/{recruitment_id}")
async def update(
    project_id: UUID,
    recruitment_id: UUID,
    payload: RecruitmentDraftUpdate,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentDraftResponse:
    return await service.update(
        db,
        actor=actor,
        project_id=project_id,
        recruitment_id=recruitment_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{recruitment_id}/publish")
async def publish(
    project_id: UUID,
    recruitment_id: UUID,
    payload: RecruitmentVersionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentActionResult:
    return await service.publish(
        db,
        actor=actor,
        project_id=project_id,
        recruitment_id=recruitment_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{recruitment_id}/close")
async def close(
    project_id: UUID,
    recruitment_id: UUID,
    payload: RecruitmentVersionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentActionResult:
    return await service.close(
        db,
        actor=actor,
        project_id=project_id,
        recruitment_id=recruitment_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{recruitment_id}/reopen")
async def reopen(
    project_id: UUID,
    recruitment_id: UUID,
    payload: RecruitmentVersionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentActionResult:
    return await service.reopen(
        db,
        actor=actor,
        project_id=project_id,
        recruitment_id=recruitment_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{recruitment_id}/confirm-active")
async def confirm_active(
    project_id: UUID,
    recruitment_id: UUID,
    payload: RecruitmentVersionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentActionResult:
    return await service.confirm_active(
        db,
        actor=actor,
        project_id=project_id,
        recruitment_id=recruitment_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{recruitment_id}/revoke-contact")
async def revoke_contact(
    project_id: UUID,
    recruitment_id: UUID,
    payload: RevokeRecruitmentContactCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> RecruitmentActionResult:
    return await service.revoke_contact(
        db,
        actor=actor,
        project_id=project_id,
        recruitment_id=recruitment_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
