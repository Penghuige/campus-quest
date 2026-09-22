# backend/tests/integration/notifications/test_inbox_visibility.py
"""The composed dispatch -> inbox visibility semantics for IN_APP
deliveries (spec §25.2; hardening step 9, terminal review N1).

`InboxService.list_inbox` lists a Notification row only when its IN_APP
delivery completed a REAL send (terminal SENT without a "skipped:"
marker). These tests prove that gate against the actual dispatch
service over real PostgreSQL — the timing boundary and the cancellation
path, not just seeded row states (test_notification_api.py pins those):

- **Timing (§25.2 "计划 24h 与 4h").** A reminder whose Notification row
  commits with the claim transaction is INVISIBLE before dispatch: at
  t0 a 30h-runway student must not see the "4 小时后截止" message. The
  boundary is exact (G14): dispatch refuses only `scheduled_at > now`,
  so at now == scheduled_at — pinned with a FrozenClock — the send
  completes and the message enters the inbox, never earlier.
- **Cancellation (§25.2 "未发送的普通 DDL 提醒必须取消/跳过").** A claim
  that entered UNDER_REVIEW suppresses its ordinary reminder at
  dispatch: the IN_APP delivery resolves SENT with the
  "skipped:deadline_claim_status:UNDER_REVIEW" marker and the message
  never lists — invisible before AND after the dispatch that cancelled
  it.
- **Read semantics do not regress.** Once visible, the message marks
  read idempotently and drops out of the ?unread filter while staying
  in the full listing.

Real commits are required (DeliveryService opens its own sessions, so
the outer-rollback harness cannot shield these rows): every seeded
user/notification/delivery triple is deleted in teardown, the same
pattern as tests/workers/test_notification_delivery.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.delivery_service import DeliveryService, SendOutcome
from app.modules.notifications.enums import (
    SKIPPED_LAST_ERROR_PREFIX,
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.inbox_service import InboxService
from app.modules.notifications.models import Notification, NotificationDelivery
from tests.fakes.integrations import FakeEmailSender, FakeSmsSender

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
_FOUR_HOURS = timedelta(hours=4)

# Direct-insert password stub (argon2 hash of an unguessable test secret).
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


@pytest_asyncio.fixture
async def db_engine() -> AsyncEngine:
    """A private NullPool engine over the guarded integration database:
    the delivery service must observe COMMITTED rows through its own
    sessions, which the outer-rollback harness cannot provide."""
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


def _delivery_service(
    engine: AsyncEngine,
    *,
    clock: FrozenClock,
    claim_status_resolver: Any = None,
) -> DeliveryService:
    return DeliveryService(
        session_maker=async_sessionmaker(engine, expire_on_commit=False),
        sms_sender=FakeSmsSender(),
        email_sender=FakeEmailSender(),
        clock=clock,
        claim_status_resolver=claim_status_resolver,
    )


async def _seed_in_app_reminder(
    engine: AsyncEngine,
    *,
    event_key: str | None = None,
    scheduled_at: datetime,
) -> tuple[User, Notification, NotificationDelivery]:
    """Commit a student + deadline-reminder Notification + its PENDING
    IN_APP delivery due at `scheduled_at`, and return the rows."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user = User(
        username=f"stu-{uuid4().hex[:12]}",
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        role=Role.STUDENT.value,
        status=UserStatus.ACTIVE.value,
    )
    key = event_key if event_key is not None else f"claim:{uuid4()}:deadline_4h"
    async with maker() as session:
        session.add(user)
        await session.flush()
        notification = Notification(
            user_id=user.id,
            event_key=key,
            event_type=NotificationEventType.ASSIGNMENT_DEADLINE_4H.value,
            title="任务将于4小时后截止",
            body="您领取的任务将于4小时后截止，请尽快提交。",
        )
        session.add(notification)
        await session.flush()
        delivery = NotificationDelivery(
            notification_id=notification.id,
            user_id=user.id,
            event_key=key,
            channel=NotificationChannel.IN_APP.value,
            status=DeliveryStatus.PENDING.value,
            scheduled_at=scheduled_at,
        )
        session.add(delivery)
        await session.commit()
        return user, notification, delivery


async def _cleanup(
    engine: AsyncEngine,
    user: User,
    notification: Notification,
    delivery: NotificationDelivery,
) -> None:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            delete(NotificationDelivery).where(NotificationDelivery.id == delivery.id)
        )
        await session.execute(
            delete(Notification).where(Notification.id == notification.id)
        )
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


async def _list_inbox(
    engine: AsyncEngine,
    user: User,
    clock: FrozenClock,
    *,
    unread_only: bool = False,
) -> tuple[list[Notification], int]:
    """InboxService.list_inbox through a fresh session (the API read
    path; the service takes the caller's session, like the router)."""
    service = InboxService(clock=clock)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        return await service.list_inbox(session, user.id, unread_only=unread_only)


# --- timing: invisible before dispatch, visible at the exact boundary ---------------


async def test_reminder_invisible_at_t0_visible_at_exact_scheduled_at(
    db_engine: AsyncEngine,
) -> None:
    """A 4h reminder scheduled for T0+4h (the 30h-runway claim of the
    N1 evidence): invisible at t0 — even though its Notification row
    committed with the claim and dispatch already ran once — and it
    enters the inbox exactly at now == scheduled_at, when dispatch is
    first allowed to complete it (G14: the boundary is pinned with a
    FrozenClock, both clocks reading the same instant)."""
    boundary = _T0 + _FOUR_HOURS
    user, notification, delivery = await _seed_in_app_reminder(
        db_engine, scheduled_at=boundary
    )
    try:
        # t0: the row exists, is unread, and is invisible in every view.
        rows, total = await _list_inbox(db_engine, user, FrozenClock(_T0))
        assert rows == []
        assert total == 0
        unread_rows, unread_total = await _list_inbox(
            db_engine, user, FrozenClock(_T0), unread_only=True
        )
        assert unread_rows == []
        assert unread_total == 0

        # An early dispatch attempt is refused (NOT_DUE) and leaves the
        # row PENDING — still invisible after it.
        early = await _delivery_service(db_engine, clock=FrozenClock(_T0)).send(
            delivery.id, "req-visibility-early"
        )
        assert early.outcome is SendOutcome.NOT_DUE
        rows, total = await _list_inbox(db_engine, user, FrozenClock(_T0))
        assert (rows, total) == ([], 0)

        # Exactly at the boundary (now == scheduled_at, to the tick):
        # dispatch completes the delivery and the message lists.
        at_boundary = await _delivery_service(
            db_engine, clock=FrozenClock(boundary)
        ).send(delivery.id, "req-visibility-boundary")
        assert at_boundary.outcome is SendOutcome.SENT
        assert at_boundary.scheduled_at == boundary
        rows, total = await _list_inbox(db_engine, user, FrozenClock(boundary))
        assert total == 1
        assert [row.id for row in rows] == [notification.id]
        assert rows[0].read_at is None

        # Read semantics do not regress once visible: mark-read drops
        # the message from the unread filter, not from the inbox.
        service = InboxService(clock=FrozenClock(boundary))
        maker = async_sessionmaker(db_engine, expire_on_commit=False)
        async with maker() as session:
            marked = await service.mark_read(session, notification.id, user.id)
            assert marked.read_at == boundary
        unread_rows, unread_total = await _list_inbox(
            db_engine, user, FrozenClock(boundary), unread_only=True
        )
        assert (unread_rows, unread_total) == ([], 0)
        rows, total = await _list_inbox(db_engine, user, FrozenClock(boundary))
        assert total == 1
        assert [row.id for row in rows] == [notification.id]
    finally:
        await _cleanup(db_engine, user, notification, delivery)


# --- cancellation: a suppressed claim keeps the reminder invisible ------------------


async def test_suppressed_claim_skip_never_becomes_visible(
    db_engine: AsyncEngine,
) -> None:
    """The claim entered UNDER_REVIEW before the reminder's due instant:
    dispatch resolves the IN_APP delivery SENT with the skip marker
    (§25.2 "取消/跳过", the frozen no-SKIPPED-status design), and the
    message is invisible before AND after that dispatch — the cancelled
    reminder can never flash into the inbox."""
    claim_id = uuid4()

    async def resolver(candidate: UUID) -> str | None:
        assert candidate == claim_id
        return "UNDER_REVIEW"

    due = _T0 - timedelta(minutes=1)
    user, notification, delivery = await _seed_in_app_reminder(
        db_engine,
        event_key=f"claim:{claim_id}:deadline_4h",
        scheduled_at=due,
    )
    try:
        # Due but undispatched: PENDING, invisible.
        rows, total = await _list_inbox(db_engine, user, FrozenClock(_T0))
        assert (rows, total) == ([], 0)

        result = await _delivery_service(
            db_engine,
            clock=FrozenClock(_T0),
            claim_status_resolver=resolver,
        ).send(delivery.id, "req-visibility-skip")
        assert result.outcome is SendOutcome.SKIPPED
        assert (
            result.skip_reason
            == f"{SKIPPED_LAST_ERROR_PREFIX}deadline_claim_status:UNDER_REVIEW"
        )

        # The terminal row is SENT-with-marker: the visibility gate
        # reads it as cancelled, in the full and the unread view alike.
        rows, total = await _list_inbox(db_engine, user, FrozenClock(_T0))
        assert (rows, total) == ([], 0)
        unread_rows, unread_total = await _list_inbox(
            db_engine, user, FrozenClock(_T0), unread_only=True
        )
        assert (unread_rows, unread_total) == ([], 0)
    finally:
        await _cleanup(db_engine, user, notification, delivery)


# --- an actionable claim delivers: the reminder really does appear -------------------


async def test_actionable_claim_reminder_lists_after_dispatch(
    db_engine: AsyncEngine,
) -> None:
    """The suppression is status-scoped, not a blanket hide: a CLAIMED
    claim's due reminder dispatches for real and lists — proving the
    gate opens on genuine sends (spec §25.2 reminders reach the student
    who still needs them)."""
    claim_id = uuid4()

    async def resolver(candidate: UUID) -> str | None:
        assert candidate == claim_id
        return "CLAIMED"

    due = _T0 - timedelta(minutes=1)
    user, notification, delivery = await _seed_in_app_reminder(
        db_engine,
        event_key=f"claim:{claim_id}:deadline_4h",
        scheduled_at=due,
    )
    try:
        result = await _delivery_service(
            db_engine,
            clock=FrozenClock(_T0),
            claim_status_resolver=resolver,
        ).send(delivery.id, "req-visibility-actionable")
        assert result.outcome is SendOutcome.SENT
        assert result.skip_reason is None
        rows, total = await _list_inbox(db_engine, user, FrozenClock(_T0))
        assert total == 1
        assert [row.id for row in rows] == [notification.id]
    finally:
        await _cleanup(db_engine, user, notification, delivery)
