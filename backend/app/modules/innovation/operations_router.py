"""Production admin guards, plus student-only self capability query."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.clock import Clock
from app.modules.audit.context import AuditContext
from app.modules.identity.admin_router import require_management_network_from_store
from app.modules.identity.dependencies import get_business_clock, require_admin_actor
from app.modules.identity.events import Actor
from app.modules.innovation.operations_schemas import (
    InnovationCapabilitiesResponse,
    OperationsGrantResponse,
    OperationsGrantSave,
)
from app.modules.innovation.operations_service import OperationsGrantService
from app.modules.innovation.router import Database, Student, _private_response

admin_router = APIRouter(
    prefix="/admin/ie/operations-grants",
    tags=["innovation operations grants"],
    dependencies=[
        Depends(require_admin_actor),
        Depends(require_management_network_from_store),
        Depends(_private_response),
    ],
)
student_router = APIRouter(
    prefix="/ie/me",
    tags=["innovation capabilities"],
    dependencies=[Depends(_private_response)],
)
Admin = Annotated[Actor, Depends(require_admin_actor)]


def get_operations_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> OperationsGrantService:
    return OperationsGrantService(clock=clock)


Service = Annotated[OperationsGrantService, Depends(get_operations_service)]


@admin_router.get("/{user_id}", response_model=OperationsGrantResponse)
async def read_grant(
    user_id: UUID, request: Request, db: Database, actor: Admin, service: Service
) -> OperationsGrantResponse:
    return await service.read(
        db, actor=actor, user_id=user_id, context=AuditContext.from_request(request)
    )


@admin_router.put("/{user_id}", response_model=OperationsGrantResponse)
async def save_grant(
    user_id: UUID,
    payload: OperationsGrantSave,
    request: Request,
    db: Database,
    actor: Admin,
    service: Service,
) -> OperationsGrantResponse:
    return await service.save(
        db,
        actor=actor,
        user_id=user_id,
        payload=payload,
        context=AuditContext.from_request(request),
    )


@student_router.get("/capabilities", response_model=InnovationCapabilitiesResponse)
async def capabilities(
    db: Database, actor: Student, service: Service
) -> InnovationCapabilitiesResponse:
    return await service.capabilities(db, actor=actor)
