"""Explicit assigned-review routes in the student operations workspace."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.clock import Clock
from app.core.config import get_settings
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.review_operations_service import ReviewOperationsService
from app.modules.innovation.review_schemas import (
    ReviewCaseSummary,
    ReviewDecisionCommand,
    ReviewPrivateDetail,
    ReviewQueueResponse,
    ReviewVersionCommand,
)
from app.modules.innovation.router import Database, Student, _private_response

router = APIRouter(
    prefix="/ie/ops/achievement-reviews",
    tags=["innovation reviews"],
    dependencies=[Depends(_private_response)],
)


def get_review_operations_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> ReviewOperationsService:
    return ReviewOperationsService(
        clock=clock, storage=S3ObjectStorage(get_settings(), clock=clock)
    )


Service = Annotated[ReviewOperationsService, Depends(get_review_operations_service)]


@router.get("", response_model=ReviewQueueResponse)
async def queue(
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewQueueResponse:
    return await service.list_queue(
        db,
        actor=actor,
        limit=limit,
        offset=offset,
        context=AuditContext.from_request(request),
    )


@router.post("/{case_id}/claim", response_model=ReviewCaseSummary)
async def claim(
    case_id: UUID,
    payload: ReviewVersionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> ReviewCaseSummary:
    return await service.claim(
        db,
        actor=actor,
        case_id=case_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{case_id}/conflict", status_code=204)
async def conflict(
    case_id: UUID,
    payload: ReviewVersionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> Response:
    await service.declare_conflict(
        db,
        actor=actor,
        case_id=case_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.get("/{case_id}", response_model=ReviewPrivateDetail)
async def detail(
    case_id: UUID, request: Request, db: Database, actor: Student, service: Service
) -> ReviewPrivateDetail:
    return await service.detail(
        db, actor=actor, case_id=case_id, context=AuditContext.from_request(request)
    )


@router.post("/{case_id}/decision", response_model=ReviewCaseSummary)
async def decision(
    case_id: UUID,
    payload: ReviewDecisionCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> ReviewCaseSummary:
    return await service.decision(
        db,
        actor=actor,
        case_id=case_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.get("/{case_id}/evidence/{evidence_id}/content", response_class=Response)
async def content(
    case_id: UUID,
    evidence_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> Response:
    obj = await service.read_content(
        db,
        actor=actor,
        case_id=case_id,
        evidence_id=evidence_id,
        context=AuditContext.from_request(request),
    )
    extension = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}[
        obj.content_type
    ]
    return Response(
        content=obj.content,
        media_type=obj.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="evidence.{extension}"',
        },
    )
