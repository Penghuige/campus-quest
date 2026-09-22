# backend/tests/integration/notifications/test_notification_constraints.py
"""Database-level notification constraints (spec §25).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects them even when the application forgets:

- UNIQUE(event_key, user_id, channel) on notification_deliveries is the
  delivery idempotency boundary (spec §25.3): a duplicate event or a
  Celery retry can never create a second row for the same channel;
- UNIQUE(event_key, user_id) on notifications keeps one logical message
  per event per user, while a different user may carry the same
  event_key;
- delivery channel and status accept only the frozen member sets
  (interfaces.md), and `attempts` never goes negative;
- UNIQUE(event_type, channel) on notification_templates keeps one
  template per routing pair, and template event types and channels are
  closed sets.

Rows are built after their parents flush: ids are server-generated, so a
transient parent's id is still None at construction time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationTemplate,
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _student(username: str = "20250010001") -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _notification(user: User, **overrides: Any) -> Notification:
    fields: dict[str, Any] = {
        "user_id": user.id,
        "event_key": "claim:123:deadline_4h",
        "event_type": NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        "title": "任务还有 4 小时截止",
        "body": "您领取的任务将在 4 小时后截止，请尽快提交。",
    }
    fields.update(overrides)
    return Notification(**fields)


def _delivery(
    notification: Notification,
    *,
    channel: str = NotificationChannel.SMS,
    **overrides: Any,
) -> NotificationDelivery:
    fields: dict[str, Any] = {
        "notification_id": notification.id,
        "user_id": notification.user_id,
        "event_key": notification.event_key,
        "channel": channel,
        "scheduled_at": datetime.now(UTC) + timedelta(hours=4),
    }
    fields.update(overrides)
    return NotificationDelivery(**fields)


def _template(**overrides: Any) -> NotificationTemplate:
    fields: dict[str, Any] = {
        "event_type": NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        "channel": NotificationChannel.SMS,
        "title": "任务截止提醒",
        "template_body": "您领取的任务将在 {hours} 小时后截止。",
    }
    fields.update(overrides)
    return NotificationTemplate(**fields)


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


@pytest.mark.integration
async def test_duplicate_delivery_event_user_channel_rejected(
    db_session: AsyncSession,
) -> None:
    """(event_key, user_id, channel) is unique (spec §25.3): the same
    reminder event cannot enqueue two SMS deliveries for one student, no
    matter how many times the event or the Celery job re-fires."""
    student = _student()
    await _flush(db_session, student)
    notification = _notification(student)
    await _flush(db_session, notification)

    await _flush(db_session, _delivery(notification, channel=NotificationChannel.SMS))

    db_session.add(_delivery(notification, channel=NotificationChannel.SMS))
    with pytest.raises(
        IntegrityError, match="uq_notification_deliveries_event_key_user_id_channel"
    ):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_same_event_key_other_channel_or_user_allowed(
    db_session: AsyncSession,
) -> None:
    """Only the full triple is unique: one event fans out to all three
    channels for one student, and the same event_key may target another
    student's notification (spec §25)."""
    student_a = _student("20250010001")
    student_b = _student("20250010002")
    await _flush(db_session, student_a, student_b)
    notification_a = _notification(student_a, event_key="claim:42:deadline_4h")
    notification_b = _notification(student_b, event_key="claim:42:deadline_4h")
    await _flush(db_session, notification_a, notification_b)

    await _flush(
        db_session,
        _delivery(notification_a, channel=NotificationChannel.SMS),
        _delivery(notification_a, channel=NotificationChannel.EMAIL),
        _delivery(notification_a, channel=NotificationChannel.IN_APP),
        _delivery(notification_b, channel=NotificationChannel.SMS),
    )


@pytest.mark.integration
@pytest.mark.parametrize("channel", ["PUSH", "sms", ""])
async def test_delivery_off_frozen_channel_rejected(
    db_session: AsyncSession, channel: str
) -> None:
    """Channel is a closed set (SMS/EMAIL/IN_APP, interfaces.md): unknown
    or case-variant values are rejected by the database CHECK, not only by
    application validation."""
    student = _student()
    await _flush(db_session, student)
    notification = _notification(student)
    await _flush(db_session, notification)

    db_session.add(_delivery(notification, channel=channel))
    with pytest.raises(IntegrityError, match="ck_notification_deliveries_channel"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("status", ["QUEUED", "sent", "CANCELLED"])
async def test_delivery_off_frozen_status_rejected(
    db_session: AsyncSession, status: str
) -> None:
    """Status is the frozen DeliveryStatus lifecycle (interfaces.md):
    unknown or case-variant values never reach the delivery scan."""
    student = _student()
    await _flush(db_session, student)
    notification = _notification(student)
    await _flush(db_session, notification)

    db_session.add(_delivery(notification, status=status))
    with pytest.raises(IntegrityError, match="ck_notification_deliveries_status"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_delivery_negative_attempts_rejected(
    db_session: AsyncSession,
) -> None:
    """`attempts` counts send tries from zero (spec §25.4); negative
    values are rejected. The retry ceiling itself stays runtime
    configuration, so no upper bound is enforced here."""
    student = _student()
    await _flush(db_session, student)
    notification = _notification(student)
    await _flush(db_session, notification)

    db_session.add(_delivery(notification, attempts=-1))
    with pytest.raises(IntegrityError, match="ck_notification_deliveries_attempts"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_notification_event_user_rejected(
    db_session: AsyncSession,
) -> None:
    """One logical Notification per (event_key, user_id): replaying the
    same event for the same student cannot create a second inbox message
    (the spec §25.3 idempotency extends to the logical row)."""
    student = _student()
    await _flush(db_session, student)
    await _flush(db_session, _notification(student))

    db_session.add(_notification(student))
    with pytest.raises(IntegrityError, match="uq_notifications_event_key_user_id"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_template_event_type_channel_rejected(
    db_session: AsyncSession,
) -> None:
    """(event_type, channel) is unique on templates (spec §25.5): Admin
    edits the existing row (bumping version), never a second template for
    the same routing pair."""
    await _flush(db_session, _template())

    db_session.add(_template())
    with pytest.raises(
        IntegrityError, match="uq_notification_templates_event_type_channel"
    ):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_same_template_event_type_other_channel_allowed(
    db_session: AsyncSession,
) -> None:
    """Each channel carries its own template copy for an event type
    (spec §25.5); only the exact pair collides."""
    await _flush(
        db_session,
        _template(channel=NotificationChannel.SMS),
        _template(channel=NotificationChannel.EMAIL),
        _template(channel=NotificationChannel.IN_APP),
    )


@pytest.mark.integration
@pytest.mark.parametrize("event_type", ["DEADLINE_2H", "revision_required", ""])
async def test_template_off_frozen_event_type_rejected(
    db_session: AsyncSession, event_type: str
) -> None:
    """Template event types are the closed canonical event set
    (interfaces.md): unknown or case-variant names are rejected."""
    db_session.add(_template(event_type=event_type))
    with pytest.raises(IntegrityError, match="ck_notification_templates_event_type"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_template_off_frozen_channel_rejected(
    db_session: AsyncSession,
) -> None:
    """Template channels are the closed channel set (interfaces.md)."""
    db_session.add(_template(channel="PUSH"))
    with pytest.raises(IntegrityError, match="ck_notification_templates_channel"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_delivery_defaults_and_terminal_field_roundtrip(
    db_session: AsyncSession,
) -> None:
    """A minimal delivery starts PENDING with attempts 0 and NULL
    sent_at/provider_message_id/last_error (server defaults over the
    spec §25.3 field set), and the full field set round-trips after a
    terminal transition, including tz-aware scheduled_at."""
    student = _student()
    await _flush(db_session, student)
    notification = _notification(student)
    await _flush(db_session, notification)

    delivery = _delivery(notification)
    await _flush(db_session, delivery)
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(NotificationDelivery).where(NotificationDelivery.id == delivery.id)
    )
    assert loaded is not None
    assert loaded.status == DeliveryStatus.PENDING
    assert loaded.attempts == 0
    assert loaded.sent_at is None
    assert loaded.provider_message_id is None
    assert loaded.last_error is None
    assert loaded.scheduled_at is not None
    assert loaded.scheduled_at.tzinfo is not None

    loaded.status = DeliveryStatus.SENT
    loaded.attempts = 3
    loaded.sent_at = datetime.now(UTC)
    loaded.provider_message_id = "sms-provider-msg-0001"
    await _flush(db_session)
    db_session.expunge_all()

    sent = await db_session.scalar(
        select(NotificationDelivery).where(NotificationDelivery.id == delivery.id)
    )
    assert sent is not None
    assert sent.status == DeliveryStatus.SENT
    assert sent.attempts == 3
    assert sent.sent_at is not None
    assert sent.provider_message_id == "sms-provider-msg-0001"
    assert sent.last_error is None
