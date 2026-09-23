# backend/tests/integration/notifications/test_template_admin.py
"""NotificationTemplate administration over real PostgreSQL (Plan 08
T5; spec §25.5): the Admin-only service surface for create / text-edit
/ enable-disable — version bumps on content edits, the typed 409 on
UNIQUE(event_type, channel), the write-time unsafe-markup gate (V1
templates are pure ``{name}`` substitution), the same-transaction audit
rows, and the consumption seam fact (gfix C): an enabled stored row IS
the record-time render source, so template rows change dispatch
behavior through the port's render, not around it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import hash_password
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationTemplate
from app.modules.notifications.port import NotificationPort
from app.modules.notifications.template_admin import (
    AUDIT_NOTIFICATION_TEMPLATE_TOGGLED,
    AUDIT_NOTIFICATION_TEMPLATE_UPSERTED,
    NotificationTemplateAdminService,
    NotificationTemplateConflictError,
)

pytestmark = pytest.mark.integration

_NOW = datetime.now(UTC).replace(microsecond=0)
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_EVENT = NotificationEventType.ASSIGNMENT_DEADLINE_4H
_CHANNEL = NotificationChannel.SMS
_TITLE = "任务4小时后截止"
_BODY = "您领取的任务《{task_title}》将于{deadline_at}截止，请立即提交。【CampusQuest】"


async def _actor(db: AsyncSession, role: Role) -> Actor:
    user = User(
        username=f"tpl-admin-{uuid4().hex[:8]}",
        password_hash=(
            hash_password("correct-horse-battery")
            if role is Role.ADMIN
            else _PASSWORD_HASH
        ),
        nickname="模板管理员",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return Actor(user_id=user.id, role=role)


async def _audits(db: AsyncSession, template_id: object) -> list[AuditLog]:
    result = await db.scalars(
        select(AuditLog)
        .where(AuditLog.target_id == str(template_id))
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list(result)


async def _refreshed(db: AsyncSession, template_id: object) -> NotificationTemplate:
    row = await db.get(NotificationTemplate, template_id)
    assert row is not None
    await db.refresh(row)
    return row


# --- create -------------------------------------------------------------------------


async def test_admin_creates_template_with_version_1_and_audit(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, Role.ADMIN)

    template = await NotificationTemplateAdminService().create(
        db_session,
        actor=actor,
        event_type=_EVENT,
        channel=_CHANNEL,
        title=_TITLE,
        template_body=_BODY,
    )

    assert template.event_type == _EVENT.value
    assert template.channel == _CHANNEL.value
    assert template.title == _TITLE
    assert template.template_body == _BODY
    assert template.enabled is True
    assert template.version == 1

    audits = await _audits(db_session, template.id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.actor_user_id == actor.user_id
    assert audit.action == AUDIT_NOTIFICATION_TEMPLATE_UPSERTED
    assert audit.target_type == "notification_template"
    assert audit.before_snapshot is None
    assert audit.after_snapshot == {
        "title": _TITLE,
        "template_body": _BODY,
        "version": 1,
        "enabled": True,
    }


async def test_duplicate_pair_is_a_typed_409_with_zero_side_effects(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, Role.ADMIN)
    service = NotificationTemplateAdminService()
    await service.create(
        db_session,
        actor=actor,
        event_type=_EVENT,
        channel=_CHANNEL,
        title=_TITLE,
        template_body=_BODY,
    )

    with pytest.raises(NotificationTemplateConflictError) as exc_info:
        await service.create(
            db_session,
            actor=actor,
            event_type="ASSIGNMENT_DEADLINE_4H",  # the same pair, raw spelling
            channel="SMS",
            title="另一个标题",
            template_body="另一个正文",
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.details == {
        "event_type": _EVENT.value,
        "channel": _CHANNEL.value,
    }
    rows = (
        await db_session.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_type == _EVENT.value
            )
        )
    ).all()
    assert len(rows) == 1  # nothing new, nothing overwritten
    # A refused write is not a change: one audit row (the create),
    # none for the refusal.
    assert len(await _audits(db_session, rows[0].id)) == 1


async def test_unknown_event_type_or_channel_is_typed_422(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, Role.ADMIN)
    service = NotificationTemplateAdminService()

    with pytest.raises(BusinessError) as bad_event:
        await service.create(
            db_session,
            actor=actor,
            event_type="NOT_AN_EVENT",
            channel=_CHANNEL,
            title=_TITLE,
            template_body=_BODY,
        )
    assert bad_event.value.status_code == 422
    assert bad_event.value.details["field"] == "event_type"

    with pytest.raises(BusinessError) as bad_channel:
        await service.create(
            db_session,
            actor=actor,
            event_type=_EVENT,
            channel="PUSH",
            title=_TITLE,
            template_body=_BODY,
        )
    assert bad_channel.value.status_code == 422
    assert bad_channel.value.details["field"] == "channel"

    assert (await db_session.scalars(select(NotificationTemplate.id))).all() == []


# --- the unsafe-expression matrix (plan 08 step 1) -----------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "您领取的任务{{task_title}}即将截止",  # Jinja expression
        "{% if task_title %}紧急{% endif %}",  # Jinja block
        "任务${task_title}即将截止",  # injection styling
        "}}",  # stray close braces alone
        "请查看{x.y}的详情",  # attribute walk
        "第{0}次提醒",  # positional index
        "{Task_Title}即将截止",  # wrong grammar (uppercase)
        "{unknown_variable}提醒",  # not in the event type's whitelist
    ],
)
async def test_unsafe_or_invalid_template_bodies_are_typed_422(
    db_session: AsyncSession, body: str
) -> None:
    actor = await _actor(db_session, Role.ADMIN)

    with pytest.raises(BusinessError) as exc_info:
        await NotificationTemplateAdminService().create(
            db_session,
            actor=actor,
            event_type=_EVENT,
            channel=_CHANNEL,
            title=_TITLE,
            template_body=body,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.details["field"] == "template_body"
    assert (await db_session.scalars(select(NotificationTemplate.id))).all() == []
    assert (
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.action == AUDIT_NOTIFICATION_TEMPLATE_UPSERTED
            )
        )
    ).all() == []


async def test_unsafe_markup_in_the_title_is_rejected_too(
    db_session: AsyncSession,
) -> None:
    """The renderer substitutes the SAME grammar in title and body, so
    the write-time gate covers both fields — a title that would fail at
    render must fail at write."""
    actor = await _actor(db_session, Role.ADMIN)

    with pytest.raises(BusinessError) as exc_info:
        await NotificationTemplateAdminService().create(
            db_session,
            actor=actor,
            event_type=_EVENT,
            channel=_CHANNEL,
            title="{{task_title}}",
            template_body=_BODY,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.details["field"] == "title"
    assert exc_info.value.details["marker"] == "{{"


async def test_valid_placeholders_and_literal_braces_are_accepted(
    db_session: AsyncSession,
) -> None:
    """The grammar the renderer actually honors: whitelisted {name}
    pairs pass, and braces that do not FORM a complete group (an
    unpaired ``{``, a stray ``}``) are literal text, not markup."""
    actor = await _actor(db_session, Role.ADMIN)

    template = await NotificationTemplateAdminService().create(
        db_session,
        actor=actor,
        event_type=_EVENT,
        channel=NotificationChannel.IN_APP,
        title="提醒 {task_title}",
        template_body=(
            "} 与 { 不成对即为字面文本。《{task_title}》将于{deadline_at}截止。"
        ),
    )

    assert template.version == 1


@pytest.mark.parametrize(
    ("title", "body"),
    [("", _BODY), ("  ", _BODY), (_TITLE, ""), ("x" * 256, _BODY), (_TITLE, " \t ")],
)
async def test_blank_or_overlong_texts_are_typed_422(
    db_session: AsyncSession, title: str, body: str
) -> None:
    actor = await _actor(db_session, Role.ADMIN)

    with pytest.raises(BusinessError) as exc_info:
        await NotificationTemplateAdminService().create(
            db_session,
            actor=actor,
            event_type=_EVENT,
            channel=_CHANNEL,
            title=title,
            template_body=body,
        )

    assert exc_info.value.status_code == 422
    assert (await db_session.scalars(select(NotificationTemplate.id))).all() == []


# --- update: text edits bump version --------------------------------------------------


async def test_admin_edit_bumps_version_and_audits_the_migration(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, Role.ADMIN)
    service = NotificationTemplateAdminService()
    template = await service.create(
        db_session,
        actor=actor,
        event_type=_EVENT,
        channel=_CHANNEL,
        title=_TITLE,
        template_body=_BODY,
    )

    new_body = "紧急：《{task_title}》{deadline_at}截止，请立刻提交。【CampusQuest】"
    updated = await service.update(
        db_session,
        actor=actor,
        template_id=template.id,
        title="任务即将截止",
        template_body=new_body,
    )

    assert updated.title == "任务即将截止"
    assert updated.template_body == new_body
    assert updated.version == 2

    audits = await _audits(db_session, template.id)
    # Both writes land inside the rollback harness's ONE outer
    # transaction, so created_at ties and row order is unobservable —
    # identify the edit by its non-None before_snapshot.
    assert sorted(audit.action for audit in audits) == sorted(
        [AUDIT_NOTIFICATION_TEMPLATE_UPSERTED, AUDIT_NOTIFICATION_TEMPLATE_UPSERTED]
    )
    edit = next(audit for audit in audits if audit.before_snapshot is not None)
    assert edit.before_snapshot == {
        "title": _TITLE,
        "template_body": _BODY,
        "version": 1,
        "enabled": True,
    }
    assert edit.after_snapshot == {
        "title": "任务即将截止",
        "template_body": new_body,
        "version": 2,
        "enabled": True,
    }


async def test_update_unknown_template_is_404(db_session: AsyncSession) -> None:
    actor = await _actor(db_session, Role.ADMIN)
    missing_id = uuid4()

    with pytest.raises(BusinessError) as exc_info:
        await NotificationTemplateAdminService().update(
            db_session,
            actor=actor,
            template_id=missing_id,
            title=_TITLE,
            template_body=_BODY,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert exc_info.value.details == {"template_id": str(missing_id)}


async def test_long_body_is_truncated_in_the_audit_snapshot_only(
    db_session: AsyncSession,
) -> None:
    """The §30 snapshots carry a TRUNCATED summary; the row keeps the
    full text the renderer will use."""
    actor = await _actor(db_session, Role.ADMIN)
    long_body = "提醒：" + "很长的正文内容。" * 60 + "{task_title}"
    template = await NotificationTemplateAdminService().create(
        db_session,
        actor=actor,
        event_type=_EVENT,
        channel=_CHANNEL,
        title=_TITLE,
        template_body=long_body,
    )

    refreshed = await _refreshed(db_session, template.id)
    assert refreshed.template_body == long_body

    audit = (await _audits(db_session, template.id))[0]
    summary = audit.after_snapshot["template_body"]
    assert isinstance(summary, str)
    assert len(summary) == 120
    assert summary.endswith("…")
    assert summary != long_body


# --- enable / disable ----------------------------------------------------------------


async def test_toggle_writes_audit_and_keeps_the_content_version(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, Role.ADMIN)
    service = NotificationTemplateAdminService()
    template = await service.create(
        db_session,
        actor=actor,
        event_type=_EVENT,
        channel=_CHANNEL,
        title=_TITLE,
        template_body=_BODY,
    )

    disabled = await service.set_enabled(
        db_session, actor=actor, template_id=template.id, enabled=False
    )
    assert disabled.enabled is False
    # version tracks CONTENT: a toggle is not an edit (the wave's
    # registered seam decision).
    assert disabled.version == 1

    # The toggle's audit row is identified by its ACTION (created_at
    # ties inside the rollback harness make row order unobservable).
    toggle_audits = [
        audit
        for audit in await _audits(db_session, template.id)
        if audit.action == AUDIT_NOTIFICATION_TEMPLATE_TOGGLED
    ]
    assert len(toggle_audits) == 1
    toggle_audit = toggle_audits[0]
    assert toggle_audit.action == AUDIT_NOTIFICATION_TEMPLATE_TOGGLED
    assert toggle_audit.before_snapshot == {
        "title": _TITLE,
        "template_body": _BODY,
        "version": 1,
        "enabled": True,
    }
    assert toggle_audit.after_snapshot == {
        "title": _TITLE,
        "template_body": _BODY,
        "version": 1,
        "enabled": False,
    }

    # Idempotent replay: already disabled — nothing changes, nothing
    # is audited (the staff-invitation replay ruling).
    replayed = await service.set_enabled(
        db_session, actor=actor, template_id=template.id, enabled=False
    )
    assert replayed.enabled is False
    assert len(await _audits(db_session, template.id)) == 2


async def test_toggle_unknown_template_is_404(db_session: AsyncSession) -> None:
    actor = await _actor(db_session, Role.ADMIN)
    with pytest.raises(BusinessError) as exc_info:
        await NotificationTemplateAdminService().set_enabled(
            db_session, actor=actor, template_id=uuid4(), enabled=True
        )
    assert exc_info.value.status_code == 404


# --- the guard: Teacher cannot touch global templates ---------------------------------


async def test_teacher_is_forbidden_on_every_operation(
    db_session: AsyncSession,
) -> None:
    teacher = await _actor(db_session, Role.TEACHER)
    service = NotificationTemplateAdminService()
    admin = await _actor(db_session, Role.ADMIN)
    template = await service.create(
        db_session,
        actor=admin,
        event_type=_EVENT,
        channel=_CHANNEL,
        title=_TITLE,
        template_body=_BODY,
    )

    calls = [
        lambda: service.create(
            db_session,
            actor=teacher,
            event_type=_EVENT,
            channel=NotificationChannel.EMAIL,
            title=_TITLE,
            template_body=_BODY,
        ),
        lambda: service.update(
            db_session,
            actor=teacher,
            template_id=template.id,
            title=_TITLE,
            template_body=_BODY,
        ),
        lambda: service.set_enabled(
            db_session, actor=teacher, template_id=template.id, enabled=False
        ),
    ]
    for call in calls:
        with pytest.raises(BusinessError) as exc_info:
            await call()
        assert exc_info.value.status_code == 403
        assert exc_info.value.code == ErrorCode.PERMISSION_DENIED

    # The Teacher's refused operations left exactly the Admin's one
    # template and its one audit row.
    rows = (
        await db_session.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.event_type == _EVENT.value
            )
        )
    ).all()
    assert len(rows) == 1
    assert len(await _audits(db_session, template.id)) == 1


# --- the consumption seam: rows drive the record-time render --------------------------


async def test_registration_renders_an_enabled_stored_row(
    db_session: AsyncSession,
) -> None:
    """Pins the gfix-C wiring: an ENABLED stored (event, IN_APP) row IS
    the record-time render source (the ``template=`` seam,
    templates.py) — the notification snapshot carries the managed
    title/body, not the seed copy."""
    admin = await _actor(db_session, Role.ADMIN)
    service = NotificationTemplateAdminService()
    revision_event = NotificationEventType.REVISION_REQUIRED
    await service.create(
        db_session,
        actor=admin,
        event_type=revision_event,
        channel=NotificationChannel.IN_APP,
        title="被覆盖的标题",
        template_body="被覆盖的正文：{task_title}——{review_comment}",
    )
    student = User(
        username=f"tpl-student-{uuid4().hex[:8]}",
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.commit()  # release the seeding savepoint

    port = NotificationPort(clock=FrozenClock(_NOW))
    event_key = f"submission:{uuid4()}:revision_required"
    await port.record_event(
        db_session,
        event_key,
        revision_event,
        student.id,
        {
            "task_title": "校园咖啡店客流记录",
            "revision_deadline_at": _NOW,
            "review_comment": "第3行缺少时间戳。",
        },
    )
    await db_session.commit()

    notification = await db_session.scalar(
        select(Notification).where(
            Notification.event_key == event_key,
            Notification.user_id == student.id,
        )
    )
    assert notification is not None
    assert notification.title == "被覆盖的标题"
    assert notification.body == "被覆盖的正文：校园咖啡店客流记录——第3行缺少时间戳。"
