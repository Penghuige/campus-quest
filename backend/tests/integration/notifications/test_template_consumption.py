# backend/tests/integration/notifications/test_template_consumption.py
"""Managed NotificationTemplate consumption at the registration point
(PR #5 final-review fix C; spec §25.5).

The record point (`NotificationPort._persist_intent`) consults the
(event_type, IN_APP) template row inside the caller's transaction. The
owner's four invariants, pinned here on real PostgreSQL:

- **New events consume the CURRENT enabled template**: an Admin text
  edit changes what the NEXT recorded event renders (version 2 copy on
  the second event).
- **Created notifications keep their creation-time snapshot**: the
  first event's row still renders the version 1 copy after the edit —
  a template change never rewrites an existing notification (the
  models.py snapshot philosophy; a negative test, not just absence of
  a rewrite path).
- **No row → seed fallback** (the G7 seed semantics): the render
  resolves from `DEFAULT_TEMPLATES` exactly as before the wiring.
- **disabled = the channel is OFF** (the gfix-C ruling): no IN_APP
  delivery row is recorded — an IN_APP-only recipient persists
  NOTHING, a multi-channel recipient keeps its SMS/EMAIL rows — and
  other channels are untouched. Spec §25.5 names the `enabled` column
  but spells no disabled semantics; the ruling follows W4's deferral
  ("enabled flags a channel switch"), consistent with the eligibility
  skip precedent: an explicit downgrade, never a silent seed fallback
  for copy the admin turned off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
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
from app.modules.notifications.port import NotificationPort
from app.modules.notifications.templates import DEFAULT_TEMPLATES

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_EVENT = NotificationEventType.REVISION_REQUIRED

_V1_TITLE = "提交需要立即修改"
_V1_BODY = "管理员副本：《{task_title}》需修改。意见：{review_comment}"
_V2_TITLE = "提交需要修改（新版）"
_V2_BODY = "新版副本：《{task_title}》——{review_comment}"


def _student(username: str, *, phone: str | None, email: str | None) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=f"同学{username[-4:]}",
        phone_e164=phone,
        email_normalized=email,
        email_verified_at=_NOW - timedelta(days=1) if email else None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _template(*, enabled: bool = True, title: str = _V1_TITLE, body: str = _V1_BODY):
    return NotificationTemplate(
        event_type=_EVENT.value,
        channel=NotificationChannel.IN_APP.value,
        title=title,
        template_body=body,
        enabled=enabled,
        version=1,
    )


def _payload() -> dict[str, object]:
    return {
        "task_title": "校园咖啡店客流记录",
        "revision_deadline_at": _NOW + timedelta(hours=48),
        "review_comment": "第3行缺少时间戳。",
    }


async def _notification(
    db_session: AsyncSession, event_key: str, user_id: object
) -> Notification | None:
    return await db_session.scalar(
        select(Notification).where(
            Notification.event_key == event_key,
            Notification.user_id == user_id,
        )
    )


async def _deliveries(
    db_session: AsyncSession, event_key: str, user_id: object
) -> list[str]:
    return list(
        await db_session.scalars(
            select(NotificationDelivery.channel)
            .where(
                NotificationDelivery.event_key == event_key,
                NotificationDelivery.user_id == user_id,
            )
            .order_by(NotificationDelivery.channel)
        )
    )


# --- invariant 1 + 2: current template consumed; old snapshot immutable ---------------


@pytest.mark.integration
async def test_new_events_consume_the_current_enabled_template_text(
    db_session: AsyncSession,
) -> None:
    """An enabled managed row is the render source, and a text edit
    takes effect on the NEXT event: event A renders the version 1 copy,
    event B (after the edit) renders the version 2 copy."""
    student = _student(f"tpl{uuid4().hex[:8]}", phone=None, email=None)
    db_session.add(student)
    db_session.add(_template())
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key_a = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key_a, _EVENT, student.id, _payload())

    row = await db_session.scalar(select(NotificationTemplate))
    assert row is not None
    row.title = _V2_TITLE
    row.template_body = _V2_BODY
    row.version += 1
    await db_session.flush()

    key_b = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key_b, _EVENT, student.id, _payload())

    first = await _notification(db_session, key_a, student.id)
    second = await _notification(db_session, key_b, student.id)
    assert first is not None and second is not None

    expected_v1_body = (
        "管理员副本：《校园咖啡店客流记录》需修改。意见：第3行缺少时间戳。"
    )
    assert (first.title, first.body) == (_V1_TITLE, expected_v1_body)
    assert (second.title, second.body) == (
        _V2_TITLE,
        "新版副本：《校园咖啡店客流记录》——第3行缺少时间戳。",
    )
    # The edit moved the row itself (version 2 on the row).
    await db_session.refresh(row)
    assert (row.title, row.version) == (_V2_TITLE, 2)


@pytest.mark.integration
async def test_created_notification_keeps_its_creation_time_snapshot(
    db_session: AsyncSession,
) -> None:
    """Negative pin: after the template edit (and even a re-record of
    the SAME event key), the already-created notification still renders
    the copy it was created with — a template change never rewrites an
    existing row."""
    student = _student(f"tpl{uuid4().hex[:8]}", phone=None, email=None)
    db_session.add(student)
    db_session.add(_template())
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key, _EVENT, student.id, _payload())

    row = await db_session.scalar(select(NotificationTemplate))
    assert row is not None
    row.title = _V2_TITLE
    row.template_body = _V2_BODY
    row.version += 1
    await db_session.flush()

    # A re-record of the same key is the strongest rewrite attempt: the
    # §25.3 first-write-wins rule discards the new render.
    await port.record_event(db_session, key, _EVENT, student.id, _payload())

    notification = await _notification(db_session, key, student.id)
    assert notification is not None
    assert notification.title == _V1_TITLE
    assert notification.body.startswith("管理员副本：《校园咖啡店客流记录》")
    assert notification.body.endswith("意见：第3行缺少时间戳。")


# --- no row → seed fallback (G7 seed semantics) ---------------------------------------


@pytest.mark.integration
async def test_no_template_row_falls_back_to_the_seed(
    db_session: AsyncSession,
) -> None:
    """No (event_type, IN_APP) row: the render resolves from
    DEFAULT_TEMPLATES exactly as before the consumption wiring."""
    student = _student(f"tpl{uuid4().hex[:8]}", phone=None, email=None)
    db_session.add(student)
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key, _EVENT, student.id, _payload())

    notification = await _notification(db_session, key, student.id)
    assert notification is not None
    seed = DEFAULT_TEMPLATES[(_EVENT, NotificationChannel.IN_APP)]
    deadline_iso = (_NOW + timedelta(hours=48)).isoformat()
    assert notification.title == seed.title
    assert notification.body == seed.body.format(
        task_title="校园咖啡店客流记录",
        revision_deadline_at=deadline_iso,
        review_comment="第3行缺少时间戳。",
    )


# --- disabled = the IN_APP channel is OFF (the gfix-C ruling) ----------------------


@pytest.mark.integration
async def test_disabled_in_app_template_records_no_in_app_delivery(
    db_session: AsyncSession,
) -> None:
    """A disabled (event_type, IN_APP) row deactivates the channel: an
    IN_APP-only recipient persists NOTHING (the no-eligible-channel
    rule), and the bookkeeping snapshot for surviving channels falls
    back to the seed copy the admin did not turn off."""
    in_app_only = _student(f"tpl{uuid4().hex[:8]}", phone=None, email=None)
    multi_channel = _student(
        f"tpl{uuid4().hex[:8]}", phone="+8613800138000", email="multi@example.com"
    )
    db_session.add_all([in_app_only, multi_channel])
    db_session.add(_template(enabled=False))
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key_a = f"submission:{uuid4().hex}:revision_required"
    key_b = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key_a, _EVENT, in_app_only.id, _payload())
    await port.record_event(db_session, key_b, _EVENT, multi_channel.id, _payload())

    # IN_APP was the only eligible channel: nothing at all persists.
    assert await _notification(db_session, key_a, in_app_only.id) is None
    assert await _deliveries(db_session, key_a, in_app_only.id) == []

    # Other channels are untouched: the logical row still backs the
    # SMS/EMAIL deliveries, with the seed snapshot as bookkeeping.
    notification = await _notification(db_session, key_b, multi_channel.id)
    assert notification is not None
    seed = DEFAULT_TEMPLATES[(_EVENT, NotificationChannel.IN_APP)]
    assert notification.title == seed.title
    channels = await _deliveries(db_session, key_b, multi_channel.id)
    assert channels == ["EMAIL", "SMS"]
    statuses = await db_session.scalars(
        select(NotificationDelivery.status).where(
            NotificationDelivery.event_key == key_b
        )
    )
    assert set(statuses) == {DeliveryStatus.PENDING.value}
