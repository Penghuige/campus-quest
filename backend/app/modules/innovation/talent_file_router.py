"""Student-only talent uploads use independent storage and scanner composition."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.core.clock import Clock
from app.core.config import get_settings
from app.integrations.evidence_scanner_clamd import create_evidence_scanner
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.router import Database, Student, _private_response
from app.modules.innovation.talent_file_schemas import (
    TalentFileIntentCreate,
    TalentFileIntentResponse,
    TalentFileListResponse,
    TalentFileResponse,
)
from app.modules.innovation.talent_file_service import TalentFileService
from app.modules.innovation.talent_schemas import TalentActionResult, TalentFileCommand

router = APIRouter(
    prefix="/ie/me/talent-profile/files",
    tags=["innovation talent files"],
    dependencies=[Depends(_private_response)],
)


def get_talent_file_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> TalentFileService:
    settings = get_settings()
    return TalentFileService(
        clock=clock,
        storage=S3ObjectStorage(settings, clock=clock),
        scanner=create_evidence_scanner(settings),
    )


Service = Annotated[TalentFileService, Depends(get_talent_file_service)]


@router.post("", response_model=TalentFileIntentResponse, status_code=201)
async def create(
    payload: TalentFileIntentCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Student,
    service: Service,
) -> TalentFileIntentResponse:
    result, created = await service.create_intent(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )
    response.status_code = 201 if created else 200
    return result


@router.post("/{file_id}/complete", response_model=TalentFileResponse)
async def complete(
    file_id: UUID, request: Request, db: Database, actor: Student, service: Service
) -> TalentFileResponse:
    return await service.complete(
        db, actor=actor, file_id=file_id, context=AuditContext.from_request(request)
    )


@router.get("", response_model=TalentFileListResponse)
async def list_owned(
    request: Request, db: Database, actor: Student, service: Service
) -> TalentFileListResponse:
    return await service.list_owned(
        db, actor=actor, context=AuditContext.from_request(request)
    )


@router.post("/{file_id}/remove", response_model=TalentActionResult)
async def remove(
    file_id: UUID,
    payload: TalentFileCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> TalentActionResult:
    return await service.remove(
        db,
        actor=actor,
        file_id=file_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.post("/{file_id}/revoke", response_model=TalentActionResult)
async def revoke(
    file_id: UUID,
    payload: TalentFileCommand,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> TalentActionResult:
    return await service.revoke(
        db,
        actor=actor,
        file_id=file_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@router.get("/{file_id}/content")
async def content(
    file_id: UUID, request: Request, db: Database, actor: Student, service: Service
) -> Response:
    obj = await service.read_owned_content(
        db, actor=actor, file_id=file_id, context=AuditContext.from_request(request)
    )
    extension = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}[
        obj.content_type
    ]
    return Response(
        content=obj.content,
        media_type=obj.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="talent-work.{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
