# backend/app/modules/notifications/inbox_service.py
"""The notification inbox read/mark-read service (spec §25/§28; plan
07 T8) behind the student and admin notification API surfaces.

Three queries, one write — all scoped so the transport layer can never
leak another user's data:

- **Inbox listing** reads ONLY the caller's own `Notification` rows
  (the logical per-user messages, newest first) that have actually been
  DELIVERED in-app: the row lists iff its IN_APP `NotificationDelivery`
  reached terminal SENT through a real send — status SENT without a
  "skipped:" last_error marker. This is spec §25.2's timing and
  cancellation semantics enforced on the read side: a deadline reminder
  scheduled for a future instant stays invisible before dispatch (a
  Notification row commits with the claim transaction, but the message
  only enters the inbox when its IN_APP delivery completes — the
  delivery_service module contract), and a policy skip (e.g. the claim
  moved to UNDER_REVIEW, "未发送的普通 DDL 提醒必须取消/跳过") resolves
  the delivery SENT WITH the marker, so the cancelled message never
  lists. Rows with no IN_APP delivery at all (the task's channel set
  dropped IN_APP, or the account cannot receive) are not inbox
  messages. Provider and delivery state stays invisible here by
  construction — it is operational data, not inbox content; the staff
  failures surface below is its only API projection.
- **Mark-read** is idempotent per message: read state is a property of
  the logical Notification (models.py), so re-marking returns the row
  with its FIRST read_at unchanged rather than bumping the timestamp.
  Ownership is judged under the row lock and answers
  `NotificationNotOwnedError` (PERMISSION_DENIED, the claim-module
  precedent for "the row exists but belongs to another user");
  a missing row answers `NotificationNotFoundError`.
- **Failures listing** is the spec §25.4 "后台可查询失败原因"
  surface (Plan 08 consumes it): FAILED deliveries newest-first with
  `last_error` and `attempts`, staff-guarded at the transport layer.

Transaction ownership (backend-engineering §5): listing methods only
read through the caller's session; `mark_read` commits exactly once,
on the success path, after mutating rows.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.notifications.enums import (
    SKIPPED_LAST_ERROR_PREFIX,
    DeliveryStatus,
    NotificationChannel,
)
from app.modules.notifications.models import Notification, NotificationDelivery

_NOTIFICATION_NOT_FOUND_MESSAGE = "通知不存在"
_NOTIFICATION_NOT_OWNED_MESSAGE = "不能操作他人的通知"


def _in_app_delivery_completed() -> Exists:
    """EXISTS criterion: the notification's IN_APP delivery completed a
    REAL send — terminal SENT WITHOUT a policy-skip "skipped:" marker.

    This is the §25.2 visibility gate (see module docstring): PENDING /
    RETRYABLE / SENDING rows (not yet dispatched — a future-scheduled
    reminder) and SENT-by-skip rows (a cancelled reminder) fail it, as
    does a notification with no IN_APP delivery at all. Correlated
    against `Notification.id`, so it drops into both the page query and
    the count; the delivery-side `ix_notification_deliveries_
    notification_id` index carries the probe.
    """

    skip_marker = f"{SKIPPED_LAST_ERROR_PREFIX}%"
    return (
        select(NotificationDelivery.id)
        .where(
            NotificationDelivery.notification_id == Notification.id,
            NotificationDelivery.channel == NotificationChannel.IN_APP.value,
            NotificationDelivery.status == DeliveryStatus.SENT.value,
            or_(
                NotificationDelivery.last_error.is_(None),
                ~NotificationDelivery.last_error.like(skip_marker),
            ),
        )
        .exists()
    )


class NotificationNotFoundError(BusinessError):
    """No Notification row for the id (the §29 404 shape)."""

    def __init__(self, notification_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _NOTIFICATION_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"notification_id": str(notification_id)},
        )


class NotificationNotOwnedError(BusinessError):
    """The notification exists but belongs to another user (the
    claim-module ClaimNotOwnedError precedent: PERMISSION_DENIED 403)."""

    def __init__(self, notification_id: UUID, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOTIFICATION_NOT_OWNED_MESSAGE,
            status_code=403,
            details={
                "notification_id": str(notification_id),
                "user_id": str(user_id),
            },
        )


class InboxService:
    """Own-scoped inbox queries and the idempotent mark-read write."""

    def __init__(self, *, clock: Clock) -> None:
        # `clock` stamps read_at: one business-time source per request,
        # shared with the composition root that built this service.
        self._clock = clock

    async def list_inbox(
        self,
        db: AsyncSession,
        user_id: UUID,
        *,
        unread_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Notification], int]:
        """The user's own delivered notifications, newest first, plus
        the total matching the filter (the page metadata).

        Only rows whose IN_APP delivery completed a real send list
        (`_in_app_delivery_completed`, the §25.2 timing/cancellation
        gate); the same predicate bounds `total` and the `unread_only`
        variant, so counts and pages can never disagree about what an
        inbox message is.
        """

        conditions = [
            Notification.user_id == user_id,
            _in_app_delivery_completed(),
        ]
        if unread_only:
            conditions.append(Notification.read_at.is_(None))
        rows = (
            await db.scalars(
                select(Notification)
                .where(*conditions)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        total = (
            await db.scalar(
                select(func.count()).select_from(Notification).where(*conditions)
            )
            or 0
        )
        return list(rows), int(total)

    async def mark_read(
        self, db: AsyncSession, notification_id: UUID, user_id: UUID
    ) -> Notification:
        """Mark the caller's own notification read, idempotently.

        The row is locked for the ownership check + write so two
        concurrent marks cannot interleave; an already-read row returns
        unchanged (first read_at wins). Commits exactly once, only when
        a write happened.
        """

        notification = (
            await db.scalars(
                select(Notification)
                .where(Notification.id == notification_id)
                .with_for_update()
            )
        ).one_or_none()
        if notification is None:
            raise NotificationNotFoundError(notification_id)
        if notification.user_id != user_id:
            raise NotificationNotOwnedError(notification_id, user_id)
        if notification.read_at is None:
            notification.read_at = self._clock.now()
            await db.commit()
        return notification

    async def list_failures(
        self, db: AsyncSession, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[NotificationDelivery], int]:
        """FAILED deliveries, most recently updated first, with the
        total (the spec §25.4 staff failure query Plan 08 builds on).

        Ordering by `updated_at` DESC puts the freshest failure at the
        top of page one; `last_error` carries the taxonomy token the
        delivery service records ("permanent:..." / "temporary:..." /
        "unknown_outcome:...").
        """

        condition = NotificationDelivery.status == DeliveryStatus.FAILED.value
        rows = (
            await db.scalars(
                select(NotificationDelivery)
                .where(condition)
                .order_by(
                    NotificationDelivery.updated_at.desc(),
                    NotificationDelivery.id.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
        ).all()
        total = (
            await db.scalar(
                select(func.count()).select_from(NotificationDelivery).where(condition)
            )
            or 0
        )
        return list(rows), int(total)
