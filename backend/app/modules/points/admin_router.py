# backend/app/modules/points/admin_router.py
"""The Admin reward/points/notification-administration API (Plan 08
T9; spec §4.3, §15, §16, §25.5): reward catalogue CRUD, the scoped
review-authorization lifecycle, manual points adjustment, and
NotificationTemplate administration — thin routes over the W3/W4
services (plus the settings surface's full-key extension, which lives
in the system router with the store it writes).

Guard pair (G10, the admin family posture): every route here mounts
``require_admin_actor`` (Admin role + ACTIVE + confirmed TOTP, spec
§33.4) plus the store-backed ``require_management_network_from_store``
(assembled in identity's admin router — the W4 footgun closure), so a
Teacher/Student direct call is 403 and an enabled network policy
refuses out-of-network peers. The review-grant routes stay Admin-only
even though the GRANTS they manage are what widens review power to
Teachers — granting review authority is itself the Admin capability
(the delegation ruling's write side).

Transport decisions:

- **AuditContext threads through every write** (§30): the services
  append their audit rows inside the write transaction; the routes
  derive ``AuditContext.from_request(request)`` so those rows carry
  the request correlation pair.
- **PATCH uses field-presence, not nullability** (the service's UNSET
  discipline): an absent field leaves the column unchanged; an
  explicit ``null`` clears a nullable bound (stock/limit/window).
  The request DTO is a plain ``| None = None`` model and the route
  builds ``RewardItemChanges`` from ``model_fields_set``, with the
  non-nullable fields (name/point_cost/requires_manual_review)
  refusing an explicit ``null`` at the transport (the service's
  validator would otherwise see a type it was never meant to receive).
- **Window bounds must be timezone-aware** (G14): naive datetimes are
  refused at the transport (422) because the timezone-aware PostgreSQL
  columns would otherwise fail deep inside the write.
- **Revocation's reason rides a query parameter** on DELETE (a DELETE
  body is poorly supported across clients); the service's
  reason-mandatory gate treats blank the same either way.
- **Template DTOs are the admin surface of W4's
  ``NotificationTemplateAdminService``**: create/patch/enable/disable
  with the service's own markup validation answering the typed 422s.
- **The three admin LISTINGS are offset-paginated with the family cap
  (limit <= 50)**, the identity admin-router listing precedent (T10's
  query gap-fill): ``GET /admin/rewards`` returns the FULL catalogue
  including disabled rows (the student ``GET /rewards`` stays the
  enabled-only shelf), ``GET /admin/notification-templates`` every
  template row including disabled ones, and ``GET
  /admin/reward-review-grants`` the live grants with the teacher's
  display nickname resolved through the directory port inside
  ``RewardAdminService``. Read-only listings with no stateful logic
  are built inline; the grants page goes through the service because
  the directory port lives there.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.modules.audit.context import AuditContext
from app.modules.identity.admin_router import require_management_network_from_store
from app.modules.identity.dependencies import require_admin_actor
from app.modules.identity.events import Actor
from app.modules.notifications.models import NotificationTemplate
from app.modules.notifications.template_admin import NotificationTemplateAdminService
from app.modules.points.admin_service import (
    PointsAdminService,
    RewardAdminService,
    RewardItemChanges,
    RewardReviewGrantLine,
)
from app.modules.points.models import RewardItem

__all__ = ["router"]

router = APIRouter(
    dependencies=[
        Depends(require_admin_actor),
        Depends(require_management_network_from_store),
    ],
)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AdminActor = Annotated[Actor, Depends(require_admin_actor)]

# The documented V1 pagination choice, family cap included (the
# identity/audit/notifications router bounds).
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50
PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]


# --- provider dependencies (module composition root) ---------------------


def get_reward_admin_service() -> RewardAdminService:
    return RewardAdminService()


def get_points_admin_service() -> PointsAdminService:
    return PointsAdminService()


def get_template_admin_service() -> NotificationTemplateAdminService:
    return NotificationTemplateAdminService()


RewardAdminDep = Annotated[RewardAdminService, Depends(get_reward_admin_service)]
PointsAdminDep = Annotated[PointsAdminService, Depends(get_points_admin_service)]
TemplateAdminDep = Annotated[
    NotificationTemplateAdminService, Depends(get_template_admin_service)
]


# --- transport DTOs (defined in admin_wire_schemas.py) -------------------

from app.modules.points.admin_wire_schemas import (  # noqa: E402
    AdminNotificationTemplateListResponse,
    AdminNotificationTemplateResponse,
    AdminRewardItemListResponse,
    AdminRewardItemResponse,
    NotificationTemplateCreateRequest,
    NotificationTemplateUpdateRequest,
    PointsAdjustmentRequest,
    PointsAdjustmentResponse,
    RewardItemCreateRequest,
    RewardItemDisableRequest,
    RewardItemUpdateRequest,
    RewardReviewGrantListResponse,
    RewardReviewGrantRequest,
    RewardReviewGrantResponse,
)

# --- reward catalogue administration (W3; spec §16) -----------------------


@router.get("/admin/rewards", response_model=AdminRewardItemListResponse)
async def list_reward_items(
    db: DbSession,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> AdminRewardItemListResponse:
    """The FULL catalogue page for management, disabled rows INCLUDED
    (the student ``GET /rewards`` listing stays the enabled-only shelf;
    no query filter — the whole catalogue, paginated; read-only listing
    built inline — the identity admin-router listing precedent)."""
    total = int(await db.scalar(select(func.count()).select_from(RewardItem)))
    rows = await db.scalars(
        select(RewardItem)
        .order_by(RewardItem.name, RewardItem.id)
        .limit(limit)
        .offset(offset)
    )
    return AdminRewardItemListResponse(
        items=[_reward_item_response(item) for item in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/admin/rewards", response_model=AdminRewardItemResponse)
async def create_reward_item(
    body: RewardItemCreateRequest,
    actor: AdminActor,
    db: DbSession,
    service: RewardAdminDep,
    request: Request,
) -> AdminRewardItemResponse:
    """Insert one enabled RewardItem (future requests see the new
    economics; open redemptions keep their snapshots) and audit the
    creation in the same transaction."""
    item = await service.create_reward_item(
        db,
        actor,
        name=body.name,
        point_cost=body.point_cost,
        description=body.description,
        stock=body.stock,
        per_user_term_limit=body.per_user_term_limit,
        available_from=body.available_from,
        available_until=body.available_until,
        requires_manual_review=body.requires_manual_review,
        fulfillment_instructions=body.fulfillment_instructions,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return _reward_item_response(item)


@router.patch("/admin/rewards/{reward_item_id}", response_model=AdminRewardItemResponse)
async def update_reward_item(
    reward_item_id: UUID,
    body: RewardItemUpdateRequest,
    actor: AdminActor,
    db: DbSession,
    service: RewardAdminDep,
    request: Request,
) -> AdminRewardItemResponse:
    """Apply a partial update under the item row lock; cost/limit/stock
    edits bind FUTURE requests only (the snapshot discipline). Absent
    fields are unchanged, explicit nulls clear the nullable bounds."""
    changes = _reward_item_changes(body)
    item = await service.update_reward_item(
        db,
        actor,
        reward_item_id,
        changes,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return _reward_item_response(item)


@router.post(
    "/admin/rewards/{reward_item_id}/disable", response_model=AdminRewardItemResponse
)
async def disable_reward_item(
    reward_item_id: UUID,
    body: RewardItemDisableRequest,
    actor: AdminActor,
    db: DbSession,
    service: RewardAdminDep,
    request: Request,
) -> AdminRewardItemResponse:
    """enabled -> false (下架), reason mandatory; a replay on an
    already-disabled item is the idempotent no-op."""
    item = await service.disable_reward_item(
        db,
        actor,
        reward_item_id,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )
    return _reward_item_response(item)


# --- scoped review authorization (W3; spec §4.2/§4.3) ---------------------


@router.get("/admin/reward-review-grants", response_model=RewardReviewGrantListResponse)
async def list_reward_review_grants(
    actor: AdminActor,
    db: DbSession,
    service: RewardAdminDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> RewardReviewGrantListResponse:
    """The live-grant page, newest first: who currently holds the
    REWARD_REVIEW authorization, the granting Admin, and when — the
    teacher's display nickname resolved through the directory port
    inside the service (the module boundary holds on reads)."""
    lines, total = await service.list_reward_review_grants(
        db, actor, limit=limit, offset=offset
    )
    return RewardReviewGrantListResponse(
        items=[_grant_line_response(line) for line in lines],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/admin/reward-review-grants", status_code=204)
async def grant_reward_review(
    body: RewardReviewGrantRequest,
    actor: AdminActor,
    db: DbSession,
    service: RewardAdminDep,
    request: Request,
) -> None:
    """Grant the global REWARD_REVIEW authorization to a Teacher
    (target must be a TEACHER account; a live grant is the typed 409
    ``CONFLICT``). Effective on the review guard's next read."""
    await service.grant_reward_review(
        db,
        actor,
        body.teacher_id,
        reason=body.reason,
        audit_context=AuditContext.from_request(request),
    )


@router.delete("/admin/reward-review-grants/{teacher_id}", status_code=204)
async def revoke_reward_review(
    teacher_id: UUID,
    actor: AdminActor,
    db: DbSession,
    service: RewardAdminDep,
    request: Request,
    reason: Annotated[str, Query(min_length=1)],
) -> None:
    """Revoke the authorization (DELETE the grant row; history stays in
    the audit stream). Effective on the review guard's next read; an
    absent grant is the typed 404."""
    await service.revoke_reward_review(
        db,
        actor,
        teacher_id,
        reason=reason,
        audit_context=AuditContext.from_request(request),
    )


# --- manual points adjustment (W3; spec §15) ------------------------------


@router.post(
    "/admin/users/{user_id}/points-adjustment",
    response_model=PointsAdjustmentResponse,
)
async def adjust_user_points(
    user_id: UUID,
    body: PointsAdjustmentRequest,
    actor: AdminActor,
    db: DbSession,
    service: PointsAdminDep,
    request: Request,
) -> PointsAdjustmentResponse:
    """Post one ADMIN_ADJUSTMENT ledger entry (never a direct wallet
    write); the wallet migration is audited in the same transaction.
    The body's ``operation_id`` is the idempotency key: a replay with
    the same intent returns the original entry (one row, one balance
    move, one audit row), and the same id under a different decision is
    the typed 409 CONFLICT (PR #5 fix A, P1)."""
    entry = await service.admin_adjust_points(
        db,
        actor,
        user_id,
        body.amount,
        reason=body.reason,
        operation_id=body.operation_id,
        audit_context=AuditContext.from_request(request),
    )
    return PointsAdjustmentResponse(
        ledger_entry_id=str(entry.id),
        user_id=str(entry.user_id),
        amount=entry.amount,
        reason=entry.reason or body.reason,
    )


# --- notification template administration (W4; spec §25.5) ----------------


@router.get(
    "/admin/notification-templates",
    response_model=AdminNotificationTemplateListResponse,
)
async def list_notification_templates(
    db: DbSession,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> AdminNotificationTemplateListResponse:
    """Every template row for administration, disabled ones INCLUDED
    (dispatch-read rows are irrelevant to the management view), ordered
    by the UNIQUE (event_type, channel) pair (read-only listing built
    inline — the identity admin-router listing precedent)."""
    total = int(await db.scalar(select(func.count()).select_from(NotificationTemplate)))
    rows = await db.scalars(
        select(NotificationTemplate)
        .order_by(NotificationTemplate.event_type, NotificationTemplate.channel)
        .limit(limit)
        .offset(offset)
    )
    return AdminNotificationTemplateListResponse(
        items=[_template_response(template) for template in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/admin/notification-templates",
    response_model=AdminNotificationTemplateResponse,
)
async def create_notification_template(
    body: NotificationTemplateCreateRequest,
    actor: AdminActor,
    db: DbSession,
    service: TemplateAdminDep,
    request: Request,
) -> AdminNotificationTemplateResponse:
    """Create the (event_type, channel) template at version 1 — or the
    typed 409 ``CONFLICT`` when one already exists. Unsafe markup and
    unknown placeholders are the typed 422 at write time."""
    template = await service.create(
        db,
        actor=actor,
        event_type=body.event_type,
        channel=body.channel,
        title=body.title,
        template_body=body.template_body,
        audit_context=AuditContext.from_request(request),
    )
    return _template_response(template)


@router.patch(
    "/admin/notification-templates/{template_id}",
    response_model=AdminNotificationTemplateResponse,
)
async def update_notification_template(
    template_id: UUID,
    body: NotificationTemplateUpdateRequest,
    actor: AdminActor,
    db: DbSession,
    service: TemplateAdminDep,
    request: Request,
) -> AdminNotificationTemplateResponse:
    """Edit title/body; version bumps by one and both §30 snapshots
    ride the audit row."""
    template = await service.update(
        db,
        actor=actor,
        template_id=template_id,
        title=body.title,
        template_body=body.template_body,
        audit_context=AuditContext.from_request(request),
    )
    return _template_response(template)


@router.post(
    "/admin/notification-templates/{template_id}/enable",
    response_model=AdminNotificationTemplateResponse,
)
async def enable_notification_template(
    template_id: UUID,
    actor: AdminActor,
    db: DbSession,
    service: TemplateAdminDep,
    request: Request,
) -> AdminNotificationTemplateResponse:
    """Enable the template (version does not move; an idempotent toggle
    writes nothing)."""
    template = await service.set_enabled(
        db,
        actor=actor,
        template_id=template_id,
        enabled=True,
        audit_context=AuditContext.from_request(request),
    )
    return _template_response(template)


@router.post(
    "/admin/notification-templates/{template_id}/disable",
    response_model=AdminNotificationTemplateResponse,
)
async def disable_notification_template(
    template_id: UUID,
    actor: AdminActor,
    db: DbSession,
    service: TemplateAdminDep,
    request: Request,
) -> AdminNotificationTemplateResponse:
    """Disable the template (version does not move; an idempotent toggle
    writes nothing)."""
    template = await service.set_enabled(
        db,
        actor=actor,
        template_id=template_id,
        enabled=False,
        audit_context=AuditContext.from_request(request),
    )
    return _template_response(template)


# --- serializers -----------------------------------------------------------


def _reward_item_response(item: RewardItem) -> AdminRewardItemResponse:
    """Serialize one RewardItem row (explicit field enumeration)."""
    return AdminRewardItemResponse(
        id=str(item.id),
        name=item.name,
        description=item.description,
        point_cost=item.point_cost,
        stock=item.stock,
        per_user_term_limit=item.per_user_term_limit,
        available_from=item.available_from,
        available_until=item.available_until,
        enabled=item.enabled,
        requires_manual_review=item.requires_manual_review,
        fulfillment_instructions=item.fulfillment_instructions,
    )


def _template_response(
    template: NotificationTemplate,
) -> AdminNotificationTemplateResponse:
    """Serialize one NotificationTemplate row (explicit field
    enumeration)."""
    return AdminNotificationTemplateResponse(
        id=str(template.id),
        event_type=template.event_type,
        channel=template.channel,
        title=template.title,
        template_body=template.template_body,
        enabled=template.enabled,
        version=template.version,
    )


def _grant_line_response(line: RewardReviewGrantLine) -> RewardReviewGrantResponse:
    """Serialize one listing line from the service's frozen dataclass
    (explicit field enumeration)."""
    return RewardReviewGrantResponse(
        teacher_id=str(line.teacher_id),
        nickname=line.nickname,
        granted_by=str(line.granted_by),
        granted_at=line.granted_at,
    )


def _reward_item_changes(body: RewardItemUpdateRequest) -> RewardItemChanges:
    """Translate field PRESENCE into the service's UNSET discipline:
    only ``model_fields_set`` fields ride the command, an explicit null
    clears a nullable column, and the non-nullable fields refuse an
    explicit null at the transport."""
    provided = body.model_fields_set
    values: dict[str, object] = {}
    for nullable_field in (
        "description",
        "stock",
        "per_user_term_limit",
        "available_from",
        "available_until",
        "fulfillment_instructions",
    ):
        if nullable_field in provided:
            values[nullable_field] = getattr(body, nullable_field)
    for non_nullable_field in ("name", "point_cost", "requires_manual_review"):
        if non_nullable_field in provided:
            value = getattr(body, non_nullable_field)
            if value is None:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    "该字段不允许为空",
                    status_code=422,
                    details={"field": non_nullable_field},
                )
            values[non_nullable_field] = value
    return RewardItemChanges(**values)  # type: ignore[arg-type]
