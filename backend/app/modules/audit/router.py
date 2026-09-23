# backend/app/modules/audit/router.py
"""The Admin audit-search and state-repair API (Plan 08 T9; spec §25.4
adjacency, §30; quality-gates G12): the audit-log search surface and
the two named repair commands from W5a — thin routes, never a second
implementation of their rules.

Guard pair (G10, the admin family posture): every route here mounts
``require_admin_actor`` (Admin role + ACTIVE + confirmed TOTP, spec
§33.4) plus the store-backed ``require_management_network_from_store``
(assembled in identity's admin router — the W4 footgun closure), so a
Teacher/Student direct call is 403 and an enabled network policy
refuses out-of-network peers.

``GET /admin/audit-logs`` (spec §30 retrieval; plan T9 step 2):

- Offset-paginated with the family cap (limit <= 50), newest first
  (``created_at`` DESC, id DESC tiebreak), with equality filters
  (``action`` / ``actor_user_id`` / ``target_type``) and a HALF-OPEN
  time range (``created_from`` inclusive, ``created_to`` exclusive —
  the G14 boundary discipline: the pair composes into contiguous
  pages without double-counting an instant).
- The DTO returns the stored snapshot columns VERBATIM: redaction is a
  WRITE-side invariant (``AuditLogWriter`` forces every structured
  payload through ``redact``), so what the admin reads is exactly the
  durable record — no second redaction pass that could silently differ
  from what was persisted, and no extra fields the row never carried.
  Audit search itself is not a G12 sensitive read (it is the audit
  trail's consumption surface, already behind the management gate);
  the sensitive READS the spec names (identity reveal, PII exports)
  are the ones their own services audit.

``POST /admin/repairs/*`` (W5a's ``RepairService``): both commands
take the typed target id plus the mandatory reason, thread
``AuditContext``, and answer the service's typed envelopes — 404 for
unknown targets, 409 ``CONFLICT`` when the state is not the wedge the
command exists for. The service is the single authority on what a
repair may touch (G7: immutable Claim/Submission/Ledger history is
never rewritten); these routes add no new repair vocabulary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.session import get_db_session
from app.modules.audit.context import AuditContext
from app.modules.audit.models import AuditLog
from app.modules.audit.repair_service import RepairService
from app.modules.identity.admin_router import require_management_network_from_store
from app.modules.identity.dependencies import get_business_clock, require_admin_actor
from app.modules.identity.events import Actor
from app.modules.notifications.enums import DeliveryStatus
from app.modules.tasks.enums import AssignmentAvailability

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

router = APIRouter(
    dependencies=[
        Depends(require_admin_actor),
        Depends(require_management_network_from_store),
    ],
)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AdminActor = Annotated[Actor, Depends(require_admin_actor)]
PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]


# --- provider dependencies (module composition root) ---------------------


def get_repair_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> RepairService:
    # The SENDING-lease timestamp domain stays the service clock (one
    # business-time source per request, shared with the actor guard).
    return RepairService(clock=clock)


RepairServiceDep = Annotated[RepairService, Depends(get_repair_service)]


# --- transport DTOs (explicit field sets) --------------------------------


class AuditLogResponse(BaseModel):
    """One durable audit row, verbatim (write-side redaction is the
    invariant; nothing is added or removed at read time)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    actor_user_id: str
    actor_role: str
    action: str
    target_type: str
    target_id: str
    reason: str | None
    details: dict[str, object] | None
    before_snapshot: dict[str, object] | None
    after_snapshot: dict[str, object] | None
    ip_address: str | None
    request_id: str | None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    """Offset-paginated audit page, newest first."""

    model_config = ConfigDict(extra="forbid")

    items: list[AuditLogResponse]
    total: int
    limit: int
    offset: int


class ReleaseOccupiedAssignmentRequest(BaseModel):
    """The dangling Assignment id plus the mandatory why (the service
    refuses blank reasons before any read)."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: UUID
    reason: str = Field(min_length=1)


class ReleaseOccupiedAssignmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    availability_status: str


class ForceFailDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: UUID
    reason: str = Field(min_length=1)


class ForceFailDeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: str
    last_error: str
    attempts: int
    updated_at: datetime


# --- audit search (spec §30; plan T9 step 2) -----------------------------


@router.get("/admin/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    db: DbSession,
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor_user_id: Annotated[UUID | None, Query()] = None,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> AuditLogListResponse:
    """The audit-log page: newest first, equality filters, half-open
    [created_from, created_to) time range (read-only listing built
    inline — the community-router listing precedent)."""
    conditions = []
    if action is not None:
        conditions.append(AuditLog.action == action)
    if actor_user_id is not None:
        conditions.append(AuditLog.actor_user_id == actor_user_id)
    if target_type is not None:
        conditions.append(AuditLog.target_type == target_type)
    if created_from is not None:
        conditions.append(AuditLog.created_at >= created_from)
    if created_to is not None:
        conditions.append(AuditLog.created_at < created_to)
    total = int(
        await db.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    )
    rows = await db.scalars(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return AuditLogListResponse(
        items=[_audit_log_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _audit_log_response(row: AuditLog) -> AuditLogResponse:
    """Serialize one row verbatim (explicit field enumeration — never
    serialized from the ORM object directly)."""
    return AuditLogResponse(
        id=str(row.id),
        actor_user_id=str(row.actor_user_id),
        actor_role=row.actor_role,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        reason=row.reason,
        details=row.details,
        before_snapshot=row.before_snapshot,
        after_snapshot=row.after_snapshot,
        ip_address=row.ip_address,
        request_id=row.request_id,
        created_at=row.created_at,
    )


# --- named state repairs (W5a; plan T7) -----------------------------------


@router.post(
    "/admin/repairs/release-occupied-assignment",
    response_model=ReleaseOccupiedAssignmentResponse,
)
async def release_occupied_assignment(
    body: ReleaseOccupiedAssignmentRequest,
    actor: AdminActor,
    db: DbSession,
    service: RepairServiceDep,
    request: Request,
) -> ReleaseOccupiedAssignmentResponse:
    """Put a dangling OCCUPIED Assignment back to AVAILABLE (Claim
    history read, never written; the audited repair commits with the
    command). 404 unknown id; 409 ``CONFLICT`` when the occupancy is
    legitimate or the claim is not release-terminal."""
    assignment = await service.release_occupied_assignment(
        db,
        actor,
        body.assignment_id,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return ReleaseOccupiedAssignmentResponse(
        id=str(assignment.id),
        task_id=str(assignment.task_id),
        availability_status=AssignmentAvailability(
            assignment.availability_status
        ).value,
    )


@router.post(
    "/admin/repairs/force-fail-delivery",
    response_model=ForceFailDeliveryResponse,
)
async def force_fail_delivery(
    body: ForceFailDeliveryRequest,
    actor: AdminActor,
    db: DbSession,
    service: RepairServiceDep,
    request: Request,
) -> ForceFailDeliveryResponse:
    """Fail a wedged SENDING delivery (the manual stuck-SENDING
    backstop; the message and its history are untouched). 404 unknown
    id; 409 ``CONFLICT`` for any non-SENDING status."""
    delivery = await service.force_fail_delivery(
        db,
        actor,
        body.delivery_id,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return ForceFailDeliveryResponse(
        id=str(delivery.id),
        status=DeliveryStatus(delivery.status).value,
        last_error=delivery.last_error or "",
        attempts=delivery.attempts,
        updated_at=delivery.updated_at,
    )
