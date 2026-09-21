# backend/app/modules/notifications/router.py
"""Notification API surfaces (spec §25/§28; plan 07 T8): the student
inbox and the staff failure query.

- **`GET /api/v1/notifications`** — the student's own inbox page:
  `require_active_student_actor` (notifications are a Student-facing
  capability surface in V1; staff inboxes have no product surface yet,
  so a staff token answers PERMISSION_DENIED rather than opening an
  unscoped listing). Rows are the caller's OWN `Notification` rows,
  newest first, with the V1 `?unread=true` filter and offset
  pagination (the documented V1 pagination choice, same bounds as the
  tasks module). The DTO carries title/body/read_at/created_at and the
  event type ONLY: provider internals, delivery-channel state, and
  every other user's rows are absent by construction (the service
  scopes the query to `actor.user_id`).
- **`POST /api/v1/notifications/{id}/read`** — mark one own
  notification read, idempotently (a re-read keeps the FIRST read_at).
  A foreign id answers PERMISSION_DENIED (the ClaimNotOwnedError
  precedent), a missing id NOT_FOUND.
- **`GET /api/v1/admin/notification-failures`** — spec §25.4
  "后台可查询失败原因": FAILED deliveries with last_error +
  attempts, newest first, behind `require_staff_management_actor`
  (the V1 staff guard; Plan 08 builds the full admin operations
  surface on this seam). Delivery data is operational state, so it is
  exposed HERE — staff-guarded — and never on the student inbox.

Every typed exception these handlers raise subclasses BusinessError
with its frozen code/status, so the core envelope handler renders them;
no module-local handler registration is needed. This module is also
the module composition root: providers assemble `InboxService` from
injected clock dependencies (tests override a dependency, never
service internals).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.session import get_db_session
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_student_actor,
    require_staff_management_actor,
)
from app.modules.identity.events import Actor
from app.modules.notifications.inbox_service import InboxService

# --- pagination bounds (the documented offset choice; tasks-module parity) --------

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

router = APIRouter()

# --- DTOs ---------------------------------------------------------------------------


class NotificationItemResponse(BaseModel):
    """One inbox message: the logical Notification's own fields only."""

    id: UUID
    event_type: str
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime


class NotificationInboxResponse(BaseModel):
    """Offset-paginated inbox page (the documented V1 choice)."""

    items: list[NotificationItemResponse]
    total: int
    limit: int
    offset: int


class NotificationFailureResponse(BaseModel):
    """One FAILED delivery for the staff failure query (spec §25.4).

    Operational state (attempts, error token, channel) — visible only
    behind the staff management guard, never on the student inbox.
    """

    id: UUID
    notification_id: UUID
    user_id: UUID
    event_key: str
    channel: str
    attempts: int
    last_error: str | None
    scheduled_at: datetime
    updated_at: datetime


class NotificationFailuresResponse(BaseModel):
    """Offset-paginated failures page, newest failure first."""

    items: list[NotificationFailureResponse]
    total: int
    limit: int
    offset: int


# --- provider dependencies (module composition root) --------------------------------


def get_inbox_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
) -> InboxService:
    # The SAME clock instance the identity guard used for this request
    # stamps read_at — one business-time source per request.
    return InboxService(clock=clock)


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
StudentActor = Annotated[Actor, Depends(require_active_student_actor)]
StaffActor = Annotated[Actor, Depends(require_staff_management_actor)]
InboxServiceDep = Annotated[InboxService, Depends(get_inbox_service)]
PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]


# --- student surfaces (spec §28) -------------------------------------------------


@router.get("/notifications", response_model=NotificationInboxResponse)
async def list_notifications(
    actor: StudentActor,
    service: InboxServiceDep,
    db: DbSession,
    unread: Annotated[bool, Query()] = False,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> NotificationInboxResponse:
    """The caller's own inbox page, newest first (V1: ?unread=true
    filters to read_at IS NULL; pagination is offset-based)."""
    notifications, total = await service.list_inbox(
        db,
        actor.user_id,
        unread_only=unread,
        limit=limit,
        offset=offset,
    )
    return NotificationInboxResponse(
        items=[
            NotificationItemResponse(
                id=notification.id,
                event_type=notification.event_type,
                title=notification.title,
                body=notification.body,
                read_at=notification.read_at,
                created_at=notification.created_at,
            )
            for notification in notifications
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationItemResponse,
)
async def mark_notification_read(
    notification_id: UUID,
    actor: StudentActor,
    service: InboxServiceDep,
    db: DbSession,
) -> NotificationItemResponse:
    """Mark one own notification read — idempotent: a re-read returns
    the message with its first read_at unchanged."""
    notification = await service.mark_read(db, notification_id, actor.user_id)
    return NotificationItemResponse(
        id=notification.id,
        event_type=notification.event_type,
        title=notification.title,
        body=notification.body,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


# --- staff surface (spec §25.4; Plan 08 consumes) -------------------------------


@router.get(
    "/admin/notification-failures",
    response_model=NotificationFailuresResponse,
)
async def list_notification_failures(
    actor: StaffActor,
    service: InboxServiceDep,
    db: DbSession,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> NotificationFailuresResponse:
    """FAILED deliveries with last_error + attempts, newest first (the
    spec §25.4 staff failure query; V1 staff guard: the management
    actor check)."""
    deliveries, total = await service.list_failures(db, limit=limit, offset=offset)
    return NotificationFailuresResponse(
        items=[
            NotificationFailureResponse(
                id=delivery.id,
                notification_id=delivery.notification_id,
                user_id=delivery.user_id,
                event_key=delivery.event_key,
                channel=delivery.channel,
                attempts=delivery.attempts,
                last_error=delivery.last_error,
                scheduled_at=delivery.scheduled_at,
                updated_at=delivery.updated_at,
            )
            for delivery in deliveries
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
