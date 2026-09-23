# backend/tests/integration/notifications/test_template_consumption.py
"""Managed NotificationTemplate consumption at the registration point
(PR #5 final-review fix D, Option A; spec §25.5).

The record point (`NotificationPort._persist_intent`) consults the
(event_type, channel) template rows inside the caller's transaction and
renders EVERY channel there, with the same event-variable namespace the
W4 write gate validates. The owner's invariants, pinned here on real
PostgreSQL:

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
- **disabled = "no override" (the gfix-D/Option A ruling, overturning
  gfix-C's channel-off reading)**: a disabled IN_APP row falls back to
  the seed copy for the snapshot AND the IN_APP delivery row is still
  recorded — whether a channel sends is the channel policy's decision
  (spec §25.1 Task toggles; §25 IN_APP fallback), never a template
  row's. A disabled SMS row is likewise not consumed.
- **Per-channel frozen snapshots**: an enabled SMS/EMAIL row's
  finished text (event variables already substituted) freezes into a
  snapshot Notification row keyed ``event_key + ":" + channel`` and
  the channel's delivery points at it; the canonical row keeps the
  IN_APP render, and channels without a consumed row keep pointing at
  the canonical row (the historic provider contract).
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

_SMS_V1_TITLE = "短信修改通知"
_SMS_V1_BODY = "《{task_title}》需修改：{review_comment}"
_SMS_V2_TITLE = "短信修改通知（新版）"
_SMS_V2_BODY = "【CampusQuest】《{task_title}》——{review_comment}"


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


def _template(
    *,
    enabled: bool = True,
    title: str = _V1_TITLE,
    body: str = _V1_BODY,
    channel: NotificationChannel = NotificationChannel.IN_APP,
):
    return NotificationTemplate(
        event_type=_EVENT.value,
        channel=channel.value,
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
) -> list[NotificationDelivery]:
    return list(
        await db_session.scalars(
            select(NotificationDelivery)
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


# --- disabled = "no override": seed fallback, channel still records ------------------


@pytest.mark.integration
async def test_disabled_in_app_template_falls_back_to_seed_and_still_records(
    db_session: AsyncSession,
) -> None:
    """A disabled (event_type, IN_APP) row is simply not consumed: the
    snapshot falls back to the seed copy AND the IN_APP delivery row is
    still recorded — an IN_APP-only recipient persists the notification
    exactly as with no row at all. The send decision belongs to channel
    policy, never to a template row (the gfix-D/Option A ruling)."""
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

    # IN_APP-only recipient: notification + IN_APP delivery persist, the
    # snapshot is the seed copy (nothing was suppressed).
    notification = await _notification(db_session, key_a, in_app_only.id)
    assert notification is not None
    seed = DEFAULT_TEMPLATES[(_EVENT, NotificationChannel.IN_APP)]
    deadline_iso = (_NOW + timedelta(hours=48)).isoformat()
    assert notification.title == seed.title
    assert notification.body == seed.body.format(
        task_title="校园咖啡店客流记录",
        revision_deadline_at=deadline_iso,
        review_comment="第3行缺少时间戳。",
    )
    deliveries = await _deliveries(db_session, key_a, in_app_only.id)
    assert [delivery.channel for delivery in deliveries] == ["IN_APP"]

    # Multi-channel recipient: all three channels recorded, every
    # delivery pointing at that event's canonical seed snapshot.
    multi = await _deliveries(db_session, key_b, multi_channel.id)
    assert [delivery.channel for delivery in multi] == ["EMAIL", "IN_APP", "SMS"]
    canonical_b = await _notification(db_session, key_b, multi_channel.id)
    assert canonical_b is not None
    assert {delivery.notification_id for delivery in multi} == {canonical_b.id}
    assert {delivery.status for delivery in multi} == {DeliveryStatus.PENDING.value}


# --- per-channel frozen snapshots (Option A) -----------------------------------------


@pytest.mark.integration
async def test_managed_sms_template_freezes_a_per_channel_snapshot(
    db_session: AsyncSession,
) -> None:
    """An enabled (event_type, SMS) row renders AT REGISTRATION with the
    event-variable namespace (the write gate's own grammar): the
    finished text — {task_title} already substituted — freezes into a
    snapshot Notification row keyed ``event_key:SMS``, and the SMS
    delivery points at it. The canonical row keeps the IN_APP seed
    render, and channels without a consumed row (EMAIL here) keep the
    historic contract of pointing at the canonical row."""
    student = _student(
        f"tpl{uuid4().hex[:8]}", phone="+8613800138000", email="multi@example.com"
    )
    db_session.add(student)
    db_session.add(
        _template(
            channel=NotificationChannel.SMS, title=_SMS_V1_TITLE, body=_SMS_V1_BODY
        )
    )
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key, _EVENT, student.id, _payload())

    canonical = await _notification(db_session, key, student.id)
    assert canonical is not None
    seed = DEFAULT_TEMPLATES[(_EVENT, NotificationChannel.IN_APP)]
    deadline_iso = (_NOW + timedelta(hours=48)).isoformat()
    assert canonical.title == seed.title
    assert canonical.body == seed.body.format(
        task_title="校园咖啡店客流记录",
        revision_deadline_at=deadline_iso,
        review_comment="第3行缺少时间戳。",
    )

    deliveries = await _deliveries(db_session, key, student.id)
    by_channel = {delivery.channel: delivery for delivery in deliveries}
    assert set(by_channel) == {"EMAIL", "IN_APP", "SMS"}
    # EMAIL and IN_APP ride the canonical row (no managed row consumed).
    assert by_channel["EMAIL"].notification_id == canonical.id
    assert by_channel["IN_APP"].notification_id == canonical.id

    # The SMS delivery points at its own frozen snapshot: the row text
    # with the event variables already substituted.
    sms_snapshot = await db_session.get(Notification, by_channel["SMS"].notification_id)
    assert sms_snapshot is not None
    assert sms_snapshot is not canonical
    assert sms_snapshot.event_key == f"{key}:SMS"
    assert sms_snapshot.user_id == student.id
    assert sms_snapshot.title == _SMS_V1_TITLE
    assert sms_snapshot.body == "《校园咖啡店客流记录》需修改：第3行缺少时间戳。"


@pytest.mark.integration
async def test_disabled_sms_row_is_not_consumed(
    db_session: AsyncSession,
) -> None:
    """A disabled SMS row means "no override": the SMS delivery keeps
    the historic contract (canonical row), exactly like a missing row —
    the channel still sends, with the seed-flavored snapshot."""
    student = _student(f"tpl{uuid4().hex[:8]}", phone="+8613800138000", email=None)
    db_session.add(student)
    db_session.add(
        _template(
            channel=NotificationChannel.SMS,
            enabled=False,
            title=_SMS_V1_TITLE,
            body=_SMS_V1_BODY,
        )
    )
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key, _EVENT, student.id, _payload())

    canonical = await _notification(db_session, key, student.id)
    assert canonical is not None
    deliveries = await _deliveries(db_session, key, student.id)
    by_channel = {delivery.channel: delivery for delivery in deliveries}
    assert set(by_channel) == {"IN_APP", "SMS"}
    assert all(delivery.notification_id == canonical.id for delivery in deliveries), (
        "a disabled row must not freeze a per-channel snapshot"
    )


@pytest.mark.integration
async def test_channel_snapshot_is_immutable_across_edits(
    db_session: AsyncSession,
) -> None:
    """The per-channel snapshot is frozen at creation: after an Admin
    edit, even a re-record of the SAME event key keeps the version 1
    snapshot row (first-write-wins on the suffixed key), while the NEXT
    event freezes the version 2 copy."""
    student = _student(f"tpl{uuid4().hex[:8]}", phone="+8613800138000", email=None)
    db_session.add(student)
    db_session.add(
        _template(
            channel=NotificationChannel.SMS, title=_SMS_V1_TITLE, body=_SMS_V1_BODY
        )
    )
    await db_session.flush()

    port = NotificationPort(clock=FrozenClock(_NOW))
    key_a = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key_a, _EVENT, student.id, _payload())

    row = await db_session.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.channel == NotificationChannel.SMS.value
        )
    )
    assert row is not None
    row.title = _SMS_V2_TITLE
    row.template_body = _SMS_V2_BODY
    row.version += 1
    await db_session.flush()

    # The strongest rewrite attempt: the SAME key re-recorded after the
    # edit, plus a fresh key for the new copy.
    await port.record_event(db_session, key_a, _EVENT, student.id, _payload())
    key_b = f"submission:{uuid4().hex}:revision_required"
    await port.record_event(db_session, key_b, _EVENT, student.id, _payload())

    for key, expected_body in (
        (key_a, "《校园咖啡店客流记录》需修改：第3行缺少时间戳。"),
        (key_b, "【CampusQuest】《校园咖啡店客流记录》——第3行缺少时间戳。"),
    ):
        deliveries = await _deliveries(db_session, key, student.id)
        sms = next(d for d in deliveries if d.channel == "SMS")
        snapshot = await db_session.get(Notification, sms.notification_id)
        assert snapshot is not None
        assert snapshot.event_key == f"{key}:SMS"
        expected_title = _SMS_V1_TITLE if key == key_a else _SMS_V2_TITLE
        assert (snapshot.title, snapshot.body) == (expected_title, expected_body)
