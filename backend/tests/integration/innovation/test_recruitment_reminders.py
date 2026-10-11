"""Persist reminders and advance the cycle in the same real PG transaction."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.core.clock import FrozenClock
from app.modules.innovation.models import OwnerQualification
from app.modules.innovation.recruitment_scheduler import RecruitmentScheduler
from app.modules.innovation.recruitment_schemas import (
    RecruitmentDraftUpdate,
    RecruitmentFields,
    RecruitmentVersionCommand,
)
from app.modules.innovation.recruitment_service import RecruitmentService
from app.modules.notifications.models import Notification, NotificationDelivery
from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.recruitment_support import fields
from tests.integration.innovation.test_recruitment_contact_races import open_role
from tests.integration.innovation.test_recruitment_races import CONTEXT
from tests.integration.innovation.test_recruitment_races import committed as committed

pytestmark = pytest.mark.integration


async def test_boundary_catchup_and_duplicate_do_not_close(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    due = row.next_remind_at
    assert due == clock.now() + timedelta(days=30)
    for instant, expected in [
        (due - timedelta(milliseconds=1), False),
        (due, True),
        (due, False),
        (due + timedelta(days=95), True),
    ]:
        scheduler = RecruitmentScheduler(clock=FrozenClock(instant))
        async with sessions() as db:
            assert await scheduler.remind_if_due(db, recruitment_id=row.id) == expected
    async with sessions() as db:
        current = await service.get_owned(db, **args, recruitment_id=row.id)
        assert current.status == "OPEN"
        assert current.next_remind_at == due + timedelta(days=120)
        notes = list(
            await db.scalars(
                select(Notification).where(
                    Notification.user_id == args["actor"].user_id
                )
            )
        )
        assert len(notes) == 2
        assert all(
            note.event_type == "IE_RECRUITMENT_CONFIRMATION_DUE" for note in notes
        )
        assert all("demo_partner" not in note.body for note in notes)
        channels = list(
            await db.scalars(
                select(NotificationDelivery.channel).where(
                    NotificationDelivery.user_id == args["actor"].user_id
                )
            )
        )
        assert channels == ["IN_APP", "IN_APP"]


async def test_notification_failure_rolls_back_due_and_version(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)

    class BrokenPort:
        async def record_event(self, *args, **kwargs):
            raise RuntimeError("storage unavailable")

    async with sessions() as db:
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await RecruitmentScheduler(
                clock=FrozenClock(row.next_remind_at), notifications=BrokenPort()
            ).remind_if_due(db, recruitment_id=row.id)
    async with sessions() as db:
        current = await service.get_owned(db, **args, recruitment_id=row.id)
        assert current.version == row.version
        assert current.next_remind_at == row.next_remind_at
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == args["actor"].user_id)
            )
            == 0
        )


async def test_confirmation_fences_old_cycle_and_resets_due(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    clock = FrozenClock(clock.now() + timedelta(days=31))
    service = RecruitmentService(clock=clock)
    async with sessions() as db:
        current = (
            await service.confirm_active(
                db,
                **args,
                recruitment_id=row.id,
                context=CONTEXT,
                payload=RecruitmentVersionCommand(
                    version=row.version, request_id=uuid4()
                ),
            )
        ).current
    assert current.cycle_id != row.cycle_id
    scheduler = RecruitmentScheduler(clock=clock)
    async with sessions() as db:
        assert not await scheduler.remind_if_due(db, recruitment_id=row.id)
        assert not await scheduler.is_current_reminder(
            db, recruitment_id=row.id, cycle_id=row.cycle_id
        )
        assert await scheduler.is_current_reminder(
            db, recruitment_id=row.id, cycle_id=current.cycle_id
        )


async def test_scan_does_not_rediscover_invalid_owner(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    async with sessions() as db:
        await db.execute(
            update(OwnerQualification)
            .where(OwnerQualification.user_id == args["actor"].user_id)
            .values(status="PENDING", approved_at=None, approved_by=None)
        )
        await db.commit()
    async with sessions() as db:
        result = await RecruitmentScheduler(
            clock=FrozenClock(row.next_remind_at)
        ).scan_due(db, limit=1, request_id="invalid-owner")
        assert result.expired == result.reminded == result.skipped == 0


async def test_ordinary_updates_preserve_due_and_date_switch_creates_cycle(
    committed, clock
):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    original_due, original_cycle = row.next_remind_at, row.cycle_id
    current_clock = FrozenClock(clock.now() + timedelta(days=12))
    service = RecruitmentService(clock=current_clock)

    async def revise(payload):
        nonlocal row
        async with sessions() as db:
            row = await service.update(
                db,
                **args,
                recruitment_id=row.id,
                context=CONTEXT,
                payload=RecruitmentDraftUpdate(
                    version=row.version,
                    request_id=uuid4(),
                    fields=RecruitmentFields(**payload),
                ),
            )
            row = (
                await service.publish(
                    db,
                    **args,
                    recruitment_id=row.id,
                    context=CONTEXT,
                    payload=RecruitmentVersionCommand(
                        version=row.version, request_id=uuid4()
                    ),
                )
            ).current

    await revise(fields() | {"introduction": "更新介绍不代表确认继续"})
    assert row.next_remind_at == original_due and row.cycle_id == original_cycle
    await revise(fields() | {"deadline_at": current_clock.now() + timedelta(days=2)})
    assert row.next_remind_at is None
    dated_cycle = row.cycle_id
    await revise(fields())
    assert row.next_remind_at == current_clock.now() + timedelta(days=30)
    assert row.cycle_id != dated_cycle
