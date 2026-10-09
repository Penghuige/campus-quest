"""Bearer-only self profile, with no other-account resource endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.owner_schemas import (
    OwnerProfileReadResponse,
    OwnerProfileResponse,
    OwnerProfileSave,
)
from app.modules.innovation.owner_service import OwnerProfileService
from app.modules.innovation.router import Database, Student, _private_response

router = APIRouter(
    prefix="/ie/me/owner-profile",
    tags=["innovation owner profile"],
    dependencies=[Depends(_private_response)],
)


def get_owner_profile_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> OwnerProfileService:
    return OwnerProfileService(clock=clock)


Service = Annotated[OwnerProfileService, Depends(get_owner_profile_service)]


@router.get("", response_model=OwnerProfileReadResponse)
async def read_owner_profile(
    request: Request, db: Database, actor: Student, service: Service
) -> OwnerProfileReadResponse:
    return await service.get_owned(
        db, actor=actor, context=AuditContext.from_request(request)
    )


@router.put("", response_model=OwnerProfileResponse)
async def save_owner_profile(
    payload: OwnerProfileSave,
    request: Request,
    db: Database,
    actor: Student,
    service: Service,
) -> OwnerProfileResponse:
    return await service.save(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )
