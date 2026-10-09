"""Bearer owner evidence management, with audited attachment-only reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.core.clock import Clock
from app.core.config import get_settings
from app.integrations.evidence_scanner_clamd import create_evidence_scanner
from app.integrations.object_storage_s3 import S3ObjectStorage
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.evidence_schemas import (
    EvidenceIntentCreate,
    EvidenceIntentResponse,
    EvidenceListResponse,
    EvidenceResponse,
)
from app.modules.innovation.evidence_service import EvidenceService
from app.modules.innovation.router import Database, Student, _private_response

router = APIRouter(
    prefix="/ie/me/project-drafts/{project_id}/achievements/{achievement_id}/evidence",
    tags=["innovation evidence"],
    dependencies=[Depends(_private_response)],
)


def get_evidence_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> EvidenceService:
    settings = get_settings()
    return EvidenceService(
        clock=clock,
        storage=S3ObjectStorage(settings, clock=clock),
        scanner=create_evidence_scanner(settings),
    )


Service = Annotated[EvidenceService, Depends(get_evidence_service)]


@router.post("", response_model=EvidenceIntentResponse, status_code=201)
async def create(
    project_id: UUID,
    achievement_id: UUID,
    payload: EvidenceIntentCreate,
    request: Request,
    response: Response,
    db: Database,
    actor: Student,
    service: Service,
) -> EvidenceIntentResponse:
    result, created = await service.create_intent(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )
    response.status_code = 201 if created else 200
    return result


@router.get("", response_model=EvidenceListResponse)
async def list_owned(
    project_id: UUID,
    achievement_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> EvidenceListResponse:
    return await service.list_owned(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        context=AuditContext.from_request(request),
    )


@router.post("/{evidence_id}/complete", response_model=EvidenceResponse)
async def complete(
    project_id: UUID,
    achievement_id: UUID,
    evidence_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> EvidenceResponse:
    return await service.complete(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        evidence_id=evidence_id,
        context=AuditContext.from_request(request),
    )


@router.delete("/{evidence_id}", status_code=204)
async def remove(
    project_id: UUID,
    achievement_id: UUID,
    evidence_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> Response:
    await service.remove(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
        evidence_id=evidence_id,
        context=AuditContext.from_request(request),
    )
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.get("/{evidence_id}/content", response_class=Response)
async def content(
    project_id: UUID,
    achievement_id: UUID,
    evidence_id: UUID,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> Response:
    obj = await service.read_content(
        db,
        actor=actor,
        project_id=project_id,
        achievement_id=achievement_id,
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
