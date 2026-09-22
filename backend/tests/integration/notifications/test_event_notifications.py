# backend/tests/integration/notifications/test_event_notifications.py
"""Durable business-event wiring into notifications (spec §25; plan 07
task 5; the outbox rule in docs/architecture/interfaces.md).

`NotificationPort.record_event` is the cross-module port domain services
call INSIDE their business transaction. These tests prove the three
properties that rule demands:

- **Transaction durability:** rows ride the caller's session and its
  transaction only — a rollback leaves zero rows, a commit makes them
  visible before any Celery dispatch has run.
- **Event idempotency:** the same event key processed twice collapses
  onto one logical Notification and one delivery per channel
  (UNIQUE(event_key, user_id) / UNIQUE(event_key, user_id, channel),
  spec §25.3); the record-time render snapshot is first-write-wins.
- **Routing:** T2 eligibility at registration decides the channel set —
  critical events fan out to IN_APP whenever the account can receive,
  with SMS/EMAIL account-gated; deadline reminders come from the T3
  planner via the CLAIM_CREATED trigger.

The claim-created test drives the REAL `ClaimService` success path with
the port constructor-injected (the sanctioned tasks-side touch);
submission/points/identity-security emitters land at merge, so their
events are recorded synthetically here — the durability and idempotency
proofs above are exactly what carries over.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.notifications.port import NotificationPort
from app.modules.notifications.templates import render_template
from app.modules.tasks.claim_service import ClaimService
from app.modules.tasks.enums import (
    AssignmentAvailability,
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, Task

_NOW = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


# --- seeding helpers ---------------------------------------------------------------


def _student(
    username: str,
    *,
    phone: str | None = "+8613800138000",
    email: str | None = "student@example.com",
    email_verified: bool = True,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=f"同学{username[-4:]}",
        phone_e164=phone,
        email_normalized=email,
        email_verified_at=(_NOW - timedelta(days=1)) if email_verified else None,
        role=Role.STUDENT,
        status=status,
    )


def _teacher(username: str) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=f"老师{username[-4:]}",
        phone_e164=None,
        email_normalized=None,
        email_verified_at=None,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User, *, duration_minutes: int, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL,
        "rarity": TaskRarity.NORMAL,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": duration_minutes,
        "submission_schema": {"columns": [{"name": "note", "type": "string"}]},
        "submission_schema_version": 2,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS", "EMAIL", "IN_APP"],
    }
    fields.update(overrides)
    return Task(**fields)


def _assignment(task: Task, *, keyword: str) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.AVAILABLE,
    )


async def _persist(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


# --- assertion helpers ---------------------------------------------------------------


async def _count(db_session: AsyncSession, *entities: Any) -> int:
    return int(
        await db_session.scalar(select(func.count()).select_from(*entities)) or 0
    )


async def _notification(
    db_session: AsyncSession, event_key: str, user_id: UUID
) -> Notification | None:
    return await db_session.scalar(
        select(Notification).where(
            Notification.event_key == event_key,
            Notification.user_id == user_id,
        )
    )


async def _deliveries(
    db_session: AsyncSession, event_key: str, user_id: UUID
) -> list[NotificationDelivery]:
    return list(
        (
            await db_session.scalars(
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.event_key == event_key,
                    NotificationDelivery.user_id == user_id,
                )
                .order_by(NotificationDelivery.channel)
            )
        ).all()
    )


def _expected_render(
    event_type: NotificationEventType, variables: dict[str, str]
) -> tuple[str, str]:
    rendered = render_template(event_type, NotificationChannel.IN_APP, variables)
    return rendered.title, rendered.body


# --- transaction durability (interfaces.md outbox rule) ------------------------------


@pytest.mark.integration
async def test_record_event_rolls_back_with_the_domain_transaction(
    db_session: AsyncSession,
) -> None:
    """record_event persists through the CALLER's session and never
    commits: a transaction that rolls back leaves zero rows, and the
    committed rerun produces its rows before any Celery dispatch."""
    student = _student("20250061001")
    await _persist(db_session, student)
    # Release the seeding savepoint so the user survives the rollback
    # below while the notification rows do not. The id is captured now
    # because the rollback expires the ORM instance.
    await db_session.commit()
    student_id = student.id

    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:revision_required"
    payload = {
        "task_title": "校园咖啡店客流记录",
        "revision_deadline_at": _NOW + timedelta(hours=48),
        "review_comment": "第3行缺少时间戳。",
    }
    await port.record_event(
        db_session, key, NotificationEventType.REVISION_REQUIRED, student_id, payload
    )
    await db_session.rollback()

    assert await _count(db_session, Notification) == 0
    assert await _count(db_session, NotificationDelivery) == 0

    await port.record_event(
        db_session, key, NotificationEventType.REVISION_REQUIRED, student_id, payload
    )
    await db_session.commit()

    assert await _count(db_session, Notification) == 1
    assert await _count(db_session, NotificationDelivery) == 3


# --- event idempotency (spec §25.3) ---------------------------------------------------


@pytest.mark.integration
async def test_same_revision_required_key_twice_yields_one_delivery_set(
    db_session: AsyncSession,
) -> None:
    """A duplicate event key is a no-op: one logical Notification, one
    delivery per channel, and the FIRST render wins as the snapshot (a
    later payload cannot rewrite an already-created notification)."""
    student = _student("20250061002")
    await _persist(db_session, student)
    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:revision_required"
    first = {
        "task_title": "图书馆座位使用调查",
        "revision_deadline_at": _NOW + timedelta(hours=36),
        "review_comment": "图片模糊。",
    }
    second = {
        "task_title": "被篡改的标题",
        "revision_deadline_at": _NOW + timedelta(hours=1),
        "review_comment": "被篡改的意见。",
    }

    await port.record_event(
        db_session, key, NotificationEventType.REVISION_REQUIRED, student.id, first
    )
    await port.record_event(
        db_session, key, NotificationEventType.REVISION_REQUIRED, student.id, second
    )

    assert await _count(db_session, Notification) == 1
    notification = await _notification(db_session, key, student.id)
    assert notification is not None
    expected_title, expected_body = _expected_render(
        NotificationEventType.REVISION_REQUIRED,
        {
            "task_title": "图书馆座位使用调查",
            "revision_deadline_at": (_NOW + timedelta(hours=36)).isoformat(),
            "review_comment": "图片模糊。",
        },
    )
    assert notification.title == expected_title
    assert notification.body == expected_body
    assert "被篡改" not in notification.body

    deliveries = await _deliveries(db_session, key, student.id)
    assert [d.channel for d in deliveries] == ["EMAIL", "IN_APP", "SMS"]
    assert all(d.status == DeliveryStatus.PENDING for d in deliveries)
    assert all(d.attempts == 0 for d in deliveries)
    assert all(d.scheduled_at == _NOW for d in deliveries)


# --- routing: critical fan-out (T2 eligibility at registration) ----------------------


@pytest.mark.integration
async def test_critical_event_fans_out_in_app_with_gated_sms_email(
    db_session: AsyncSession,
) -> None:
    """A critical event always creates the IN_APP delivery when the
    account can receive; SMS additionally requires a bound phone (the
    student here has none)."""
    student = _student("20250061003", phone=None)
    await _persist(db_session, student)
    port = NotificationPort(clock=FrozenClock(_NOW))
    key = f"submission:{uuid4().hex}:approved"

    await port.record_event(
        db_session,
        key,
        NotificationEventType.SUBMISSION_APPROVED,
        student.id,
        {"task_title": "食堂菜品评价整理", "reward_points": 80},
    )

    notification = await _notification(db_session, key, student.id)
    assert notification is not None
    assert notification.event_type == NotificationEventType.SUBMISSION_APPROVED.value
    expected_title, expected_body = _expected_render(
        NotificationEventType.SUBMISSION_APPROVED,
        {"task_title": "食堂菜品评价整理", "reward_points": "80"},
    )
    assert notification.title == expected_title
    assert notification.body == expected_body

    deliveries = await _deliveries(db_session, key, student.id)
    assert [d.channel for d in deliveries] == ["EMAIL", "IN_APP"]


@pytest.mark.integration
async def test_banned_account_records_nothing(db_session: AsyncSession) -> None:
    """An account that cannot receive on any channel yields no rows at
    all — no logical notification, no deliveries."""
    student = _student("20250061004", status=UserStatus.BANNED)
    await _persist(db_session, student)
    port = NotificationPort(clock=FrozenClock(_NOW))

    await port.record_event(
        db_session,
        f"account:{uuid4().hex}:security",
        NotificationEventType.ACCOUNT_SECURITY,
        student.id,
        {"event_summary": "登录地点异常", "event_time": _NOW},
    )

    assert await _count(db_session, Notification) == 0
    assert await _count(db_session, NotificationDelivery) == 0


# --- claim-created deadline scheduling via the REAL claim service --------------------


@pytest.mark.integration
async def test_claim_creation_schedules_deadline_deliveries_in_same_transaction(
    db_session: AsyncSession,
) -> None:
    """The claim success path records the claim:...:created trigger
    inside the claim transaction: the T3 planner's Notification and
    NotificationDelivery rows commit with the claim (outbox rule), with
    the registration-time render snapshot and PENDING due-scheduled
    rows. 72h of runway plans both reminders."""
    teacher = _teacher("t00061001")
    student = _student("20250061005")
    await _persist(db_session, teacher, student)
    task = _task(teacher, duration_minutes=4320)  # 72h RELATIVE
    await _persist(db_session, task)
    await _persist(db_session, _assignment(task, keyword="考研英语"))

    clock = FrozenClock(_NOW)
    port = NotificationPort(clock=clock)
    service = ClaimService(clock=clock, notification_recorder=port)
    claim = await service.claim_random_assignment(db_session, student.id, task.id)

    deadline = _NOW + timedelta(minutes=4320)
    expected_vars = {"task_title": task.title, "deadline_at": deadline.isoformat()}
    for suffix, event_type, lead in (
        (
            "deadline_24h",
            NotificationEventType.ASSIGNMENT_DEADLINE_24H,
            timedelta(hours=24),
        ),
        (
            "deadline_4h",
            NotificationEventType.ASSIGNMENT_DEADLINE_4H,
            timedelta(hours=4),
        ),
    ):
        key = f"claim:{claim.id}:{suffix}"
        notification = await _notification(db_session, key, student.id)
        assert notification is not None, key
        assert notification.event_type == event_type.value
        expected_title, expected_body = _expected_render(event_type, expected_vars)
        assert notification.title == expected_title
        assert notification.body == expected_body

        deliveries = await _deliveries(db_session, key, student.id)
        assert [d.channel for d in deliveries] == ["EMAIL", "IN_APP", "SMS"]
        assert all(d.status == DeliveryStatus.PENDING for d in deliveries)
        assert all(d.attempts == 0 for d in deliveries)
        assert all(d.scheduled_at == deadline - lead for d in deliveries)

    # The claim:...:created trigger is a planning input, never a
    # persisted notification of its own.
    trigger_notification = await _notification(
        db_session, f"claim:{claim.id}:created", student.id
    )
    assert trigger_notification is None


@pytest.mark.integration
async def test_claim_at_exactly_24h_boundary_plans_reminder_for_now(
    db_session: AsyncSession,
) -> None:
    """A claim with exactly 24h of runway still plans the 24h reminder —
    scheduled_at == now, the "for now once" boundary (spec §25.2; the
    FrozenClock makes the boundary exact because the port shares the
    claim service's clock)."""
    teacher = _teacher("t00061002")
    student = _student("20250061006")
    await _persist(db_session, teacher, student)
    task = _task(teacher, duration_minutes=1440)  # exactly 24h RELATIVE
    await _persist(db_session, task)
    await _persist(db_session, _assignment(task, keyword="考研政治"))

    clock = FrozenClock(_NOW)
    port = NotificationPort(clock=clock)
    service = ClaimService(clock=clock, notification_recorder=port)
    claim = await service.claim_random_assignment(db_session, student.id, task.id)

    deliveries_24h = await _deliveries(
        db_session, f"claim:{claim.id}:deadline_24h", student.id
    )
    deliveries_4h = await _deliveries(
        db_session, f"claim:{claim.id}:deadline_4h", student.id
    )
    assert len(deliveries_24h) == 3
    assert len(deliveries_4h) == 3
    assert all(d.scheduled_at == _NOW for d in deliveries_24h)
    assert all(d.scheduled_at == _NOW + timedelta(hours=20) for d in deliveries_4h)


@pytest.mark.integration
async def test_failed_claim_records_no_notification_intent(
    db_session: AsyncSession,
) -> None:
    """No domain change, no notification intent: a claim refused with a
    §8.4 business error leaves zero notification rows (the outbox rule
    cuts both ways)."""
    teacher = _teacher("t00061003")
    student = _student("20250061007")
    await _persist(db_session, teacher, student)
    task = _task(teacher, duration_minutes=4320)
    await _persist(db_session, task)
    # No assignment row: the task has nothing AVAILABLE to claim.

    clock = FrozenClock(_NOW)
    port = NotificationPort(clock=clock)
    service = ClaimService(clock=clock, notification_recorder=port)
    with pytest.raises(BusinessError) as exc_info:
        await service.claim_random_assignment(db_session, student.id, task.id)
    assert exc_info.value.code == ErrorCode.NO_ASSIGNMENT_AVAILABLE

    assert await _count(db_session, Notification) == 0
    assert await _count(db_session, NotificationDelivery) == 0


# --- validation-failed re-arm (synthetic; the emitter lands at merge) -----------------


@pytest.mark.integration
async def test_validation_failed_rearm_plans_only_future_deliveries(
    db_session: AsyncSession,
) -> None:
    """Spec §25.2 re-arm: after a machine validation failure rolls the
    claim back to an actionable status, re-planning creates only
    still-future reminders. The 24h window has passed (scheduled 6h in,
    re-arm at 10h in) so its channels are NOT extended; the 4h reminder
    is still future so a newly eligible channel IS added — per-channel
    dedupe keeps the original rows untouched."""
    teacher = _teacher("t00061004")
    # Phone bound but email unverified at claim time: SMS + IN_APP only.
    student = _student("20250061008", email_verified=False)
    await _persist(db_session, teacher, student)
    task = _task(teacher, duration_minutes=1800)  # 30h RELATIVE
    await _persist(db_session, task)

    deadline = _NOW + timedelta(minutes=1800)
    claim_id = uuid4()
    port = NotificationPort(clock=FrozenClock(_NOW))
    await port.record_event(
        db_session,
        f"claim:{claim_id}:created",
        "CLAIM_CREATED",
        student.id,
        {"claim_id": claim_id, "deadline_at": deadline, "task_title": task.title},
        task_policy=task,
    )

    key_24h = f"claim:{claim_id}:deadline_24h"
    key_4h = f"claim:{claim_id}:deadline_4h"
    assert [d.channel for d in await _deliveries(db_session, key_24h, student.id)] == [
        "IN_APP",
        "SMS",
    ]
    assert [d.channel for d in await _deliveries(db_session, key_4h, student.id)] == [
        "IN_APP",
        "SMS",
    ]

    # Between claim and validation failure the email gets verified.
    student.email_verified_at = _NOW + timedelta(hours=1)
    await db_session.flush()

    validation_key = f"submission:{uuid4().hex}:validation_failed"
    await NotificationPort(clock=FrozenClock(_NOW + timedelta(hours=10))).record_event(
        db_session,
        validation_key,
        NotificationEventType.SUBMISSION_VALIDATION_FAILED,
        student.id,
        {
            "task_title": task.title,
            "validation_summary": "第2列缺少必填值。",
            "claim_id": claim_id,
            "deadline_at": deadline,
        },
        task_policy=task,
    )

    # The critical fan-out for the validation failure itself: all three
    # channels, rendered snapshot, due now.
    notification = await _notification(db_session, validation_key, student.id)
    assert notification is not None
    assert (
        notification.event_type
        == NotificationEventType.SUBMISSION_VALIDATION_FAILED.value
    )
    expected_title, expected_body = _expected_render(
        NotificationEventType.SUBMISSION_VALIDATION_FAILED,
        {"task_title": task.title, "validation_summary": "第2列缺少必填值。"},
    )
    assert notification.title == expected_title
    assert notification.body == expected_body
    validation_channels = [
        d.channel for d in await _deliveries(db_session, validation_key, student.id)
    ]
    assert validation_channels == ["EMAIL", "IN_APP", "SMS"]

    # Missed window: the 24h reminder gains no EMAIL row.
    channels_24h = [
        d.channel for d in await _deliveries(db_session, key_24h, student.id)
    ]
    assert channels_24h == ["IN_APP", "SMS"]
    # Still-future window: the 4h reminder gains EMAIL and keeps its
    # original scheduled_at.
    deliveries_4h = await _deliveries(db_session, key_4h, student.id)
    assert [d.channel for d in deliveries_4h] == ["EMAIL", "IN_APP", "SMS"]
    assert all(d.scheduled_at == deadline - timedelta(hours=4) for d in deliveries_4h)

    # Two deadline notifications from claim time plus the validation
    # failure: nothing duplicated.
    assert await _count(db_session, Notification) == 3


# --- guards -------------------------------------------------------------------------


@pytest.mark.integration
async def test_unknown_event_type_rejected(db_session: AsyncSession) -> None:
    """The port accepts the frozen canonical set plus the CLAIM_CREATED
    trigger; anything else is a programming error, not a silent skip."""
    student = _student("20250061009")
    await _persist(db_session, student)
    port = NotificationPort(clock=FrozenClock(_NOW))

    with pytest.raises(ValueError, match="unknown event type"):
        await port.record_event(
            db_session, "claim:abc:not_a_real_event", "SOMETHING_ELSE", student.id, {}
        )


@pytest.mark.integration
async def test_claim_created_without_task_policy_rejected(
    db_session: AsyncSession,
) -> None:
    """Deadline (re)planning requires the task's notification policy
    (T2 contract); a missing policy is a programming error."""
    student = _student("20250061010")
    await _persist(db_session, student)
    port = NotificationPort(clock=FrozenClock(_NOW))

    with pytest.raises(ValueError, match="task policy"):
        await port.record_event(
            db_session,
            f"claim:{uuid4()}:created",
            "CLAIM_CREATED",
            student.id,
            {"claim_id": uuid4(), "deadline_at": _NOW + timedelta(hours=30)},
        )
