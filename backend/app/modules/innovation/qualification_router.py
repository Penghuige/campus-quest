"""Separate self state and guarded, audited ADMIN application endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.admin_router import require_management_network_from_store
from app.modules.identity.dependencies import get_business_clock, require_admin_actor
from app.modules.identity.events import Actor
from app.modules.innovation.qualification_schemas import (
    QualificationApply,
    QualificationApprove,
    QualificationDetail,
    QualificationQueue,
    QualificationState,
)
from app.modules.innovation.qualification_service import OwnerQualificationService
from app.modules.innovation.router import Database, Student, _private_response

self_router = APIRouter(
    prefix="/ie/me/owner-qualification",
    tags=["innovation owner qualification"],
    dependencies=[Depends(_private_response)],
)
admin_router = APIRouter(
    prefix="/admin/ie/owner-qualifications",
    tags=["innovation owner qualification admin"],
    dependencies=[
        Depends(require_admin_actor),
        Depends(require_management_network_from_store),
        Depends(_private_response),
    ],
)
Admin = Annotated[Actor, Depends(require_admin_actor)]


def get_qualification_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> OwnerQualificationService:
    return OwnerQualificationService(clock=clock)


Service = Annotated[OwnerQualificationService, Depends(get_qualification_service)]


@self_router.get("", response_model=QualificationState)
async def read_owned(
    db: Database, actor: Student, service: Service
) -> QualificationState:
    return await service.read_owned(db, actor=actor)


@self_router.post("", response_model=QualificationState)
async def apply(
    payload: QualificationApply,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> QualificationState:
    return await service.apply(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )


@admin_router.get("", response_model=QualificationQueue)
async def queue(
    request: Request,
    db: Database,
    actor: Admin,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QualificationQueue:
    return await service.queue(
        db,
        actor=actor,
        limit=limit,
        offset=offset,
        context=AuditContext.from_request(request),
    )


@admin_router.get("/{user_id}", response_model=QualificationDetail)
async def reveal(
    user_id: UUID, request: Request, db: Database, actor: Admin, service: Service
) -> QualificationDetail:
    return await service.reveal(
        db, actor=actor, user_id=user_id, context=AuditContext.from_request(request)
    )


@admin_router.post("/{user_id}/approve", response_model=QualificationState)
async def approve(
    user_id: UUID,
    payload: QualificationApprove,
    request: Request,
    db: Database,
    actor: Admin,
    service: Service,
) -> QualificationState:
    return await service.approve(
        db,
        actor=actor,
        user_id=user_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
