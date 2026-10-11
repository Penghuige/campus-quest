"""Repeated scans and command/expiry races on independent PG connections."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.modules.audit.service import AuditLogWriter
from app.modules.innovation.recruitment_scheduler import RecruitmentScheduler
from app.modules.innovation.recruitment_schemas import (
    RecruitmentDraftUpdate,
    RecruitmentFields,
    RecruitmentVersionCommand,
)
from app.modules.innovation.recruitment_service import RecruitmentService
from app.modules.notifications.models import Notification
from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.recruitment_support import fields
from tests.integration.innovation.test_recruitment_contact_races import (
    blocked,
    open_role,
)
from tests.integration.innovation.test_recruitment_races import CONTEXT
from tests.integration.innovation.test_recruitment_races import committed as committed

pytestmark = pytest.mark.integration


async def test_duplicate_scanners_record_once(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    scheduler = RecruitmentScheduler(clock=FrozenClock(row.next_remind_at))

    async def scan():
        async with sessions() as db:
            return await scheduler.remind_if_due(db, recruitment_id=row.id)

    assert sorted(
        await asyncio.wait_for(asyncio.gather(scan(), scan()), timeout=10)
    ) == [False, True]
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.user_id == args["actor"].user_id)
            )
            == 1
        )


@pytest.mark.parametrize("action", ["close", "confirm_active"])
async def test_reminder_and_owner_command_serialize(committed, clock, action):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    due_clock = FrozenClock(row.next_remind_at)
    service = RecruitmentService(clock=due_clock)

    async def remind():
        async with sessions() as db:
            return await RecruitmentScheduler(clock=due_clock).remind_if_due(
                db, recruitment_id=row.id
            )

    async def command():
        async with sessions() as db:
            try:
                return await getattr(service, action)(
                    db,
                    **args,
                    recruitment_id=row.id,
                    context=CONTEXT,
                    payload=RecruitmentVersionCommand(
                        version=row.version, request_id=uuid4()
                    ),
                )
            except BusinessError as error:
                return error

    reminder, result = await asyncio.wait_for(
        asyncio.gather(remind(), command()), timeout=10
    )
    if reminder:
        assert isinstance(result, BusinessError) and result.status_code == 409
    else:
        assert not isinstance(result, BusinessError)
    async with sessions() as db:
        current = await service.get_owned(db, **args, recruitment_id=row.id)
        assert current.version == row.version + 1


@pytest.mark.parametrize("first", ["extension", "expiry"])
async def test_deadline_extension_and_expiry_both_orders(committed, clock, first):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    deadline = clock.now() + timedelta(days=1)
    async with sessions() as db:
        row = await service.update(
            db,
            **args,
            recruitment_id=row.id,
            context=CONTEXT,
            payload=RecruitmentDraftUpdate(
                version=row.version,
                request_id=uuid4(),
                fields=RecruitmentFields(**(fields() | {"deadline_at": deadline})),
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
        row = await service.update(
            db,
            **args,
            recruitment_id=row.id,
            context=CONTEXT,
            payload=RecruitmentDraftUpdate(
                version=row.version,
                request_id=uuid4(),
                fields=RecruitmentFields(
                    **(fields() | {"deadline_at": deadline + timedelta(days=1)})
                ),
            ),
        )
    entered, release = asyncio.Event(), asyncio.Event()
    blocker = []

    class PausingAudit(AuditLogWriter):
        async def append(self, db, **kwargs):
            blocker.append(await db.scalar(select(func.pg_backend_pid())))
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return await super().append(db, **kwargs)

    async def extension():
        writer = RecruitmentService(
            clock=FrozenClock(deadline - timedelta(milliseconds=1)),
            audit=PausingAudit() if first == "extension" else None,
        )
        async with sessions() as db:
            try:
                return await writer.publish(
                    db,
                    **args,
                    recruitment_id=row.id,
                    context=CONTEXT,
                    payload=RecruitmentVersionCommand(
                        version=row.version, request_id=uuid4()
                    ),
                )
            except BusinessError as error:
                return error

    async def expiry():
        scheduler = RecruitmentScheduler(clock=FrozenClock(deadline))
        if first == "expiry":
            scheduler._commands = RecruitmentService(
                clock=FrozenClock(deadline), audit=PausingAudit()
            )
        async with sessions() as db:
            return await scheduler.expire_if_due(db, recruitment_id=row.id)

    operations = {"extension": extension, "expiry": expiry}
    leader = asyncio.create_task(operations[first]())
    await asyncio.wait_for(entered.wait(), timeout=5)
    follower = asyncio.create_task(
        operations["expiry" if first == "extension" else "extension"]()
    )
    try:
        await blocked(sessions, blocker[0], follower)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(leader, follower), timeout=10)
    if first == "extension":
        assert not isinstance(results[0], BusinessError) and results[1] is False
    else:
        assert results[0] is True
        assert isinstance(results[1], BusinessError) and results[1].status_code == 409
