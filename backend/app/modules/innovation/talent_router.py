"""Student-owned talent commands and authenticated standard tag browsing."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.dependencies import get_business_clock
from app.modules.innovation.public_achievement_router import Viewer
from app.modules.innovation.router import Database, Student, _private_response
from app.modules.innovation.talent_schemas import (
    TalentActionResult,
    TalentProfileReadResponse,
    TalentRevokeContactCommand,
    TalentSaveCommand,
    TalentSuggestionCommand,
    TalentSuggestionResponse,
    TalentTagListResponse,
    TalentVersionCommand,
)
from app.modules.innovation.talent_service import TalentService

router = APIRouter(
    prefix="/ie", tags=["innovation talents"], dependencies=[Depends(_private_response)]
)


def get_talent_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> TalentService:
    return TalentService(clock=clock)


Service = Annotated[TalentService, Depends(get_talent_service)]


@router.get("/me/talent-profile", response_model=TalentProfileReadResponse)
async def read(
    db: Database, actor: Student, service: Service, request: Request
) -> TalentProfileReadResponse:
    return await service.get_owned(
        db, actor=actor, context=AuditContext.from_request(request)
    )


@router.put("/me/talent-profile", response_model=TalentActionResult)
async def save(
    payload: TalentSaveCommand,
    db: Database,
    actor: Student,
    service: Service,
    request: Request,
) -> TalentActionResult:
    return await service.save(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )


@router.post("/me/talent-profile/publish", response_model=TalentActionResult)
async def publish(
    payload: TalentVersionCommand,
    db: Database,
    actor: Student,
    service: Service,
    request: Request,
) -> TalentActionResult:
    return await service.publish(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )


@router.post("/me/talent-profile/hide", response_model=TalentActionResult)
async def hide(
    payload: TalentVersionCommand,
    db: Database,
    actor: Student,
    service: Service,
    request: Request,
) -> TalentActionResult:
    return await service.hide(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )


@router.post("/me/talent-profile/revoke-contact", response_model=TalentActionResult)
async def revoke_contact(
    payload: TalentRevokeContactCommand,
    db: Database,
    actor: Student,
    service: Service,
    request: Request,
) -> TalentActionResult:
    return await service.revoke_contact(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )


@router.get("/talent-tags", response_model=TalentTagListResponse)
async def tags(db: Database, actor: Viewer, service: Service) -> TalentTagListResponse:
    return await service.list_tags(db)


@router.post("/me/talent-tag-suggestions", response_model=TalentSuggestionResponse)
async def suggest(
    payload: TalentSuggestionCommand,
    db: Database,
    actor: Student,
    service: Service,
    request: Request,
) -> TalentSuggestionResponse:
    return await service.suggest(
        db, actor=actor, payload=payload, context=AuditContext.from_request(request)
    )
