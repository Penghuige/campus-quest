"""A close committed before visibility must suppress even an already claimed send."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.modules.audit.service import AuditLogWriter
from app.modules.innovation.recruitment_schemas import RecruitmentVersionCommand
from app.modules.innovation.recruitment_service import RecruitmentService
from app.modules.notifications.delivery_service import SendOutcome
from app.modules.notifications.inbox_service import InboxService
from app.workers.jobs.send_notification import build_delivery_service
from tests.integration.innovation.talent_support import clock as clock
from tests.integration.innovation.test_recruitment_composition_smoke import seed_due
from tests.integration.innovation.test_recruitment_contact_races import blocked
from tests.integration.innovation.test_recruitment_races import CONTEXT
from tests.integration.innovation.test_recruitment_races import committed as committed

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("action", ["close", "confirm_active"])
async def test_owner_change_before_visibility_suppresses_claimed_delivery(
    committed, clock, monkeypatch, action
):
    sessions, service, args = committed
    row, delivery_id = await seed_due(committed, clock)
    sender = build_delivery_service(session_maker=sessions)
    finalize = sender._finalize
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_finalize(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await finalize(*args, **kwargs)

    monkeypatch.setattr(sender, "_finalize", paused_finalize)
    sending = asyncio.create_task(sender.send(delivery_id, "r3-finalize-gap"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with sessions() as db:
            row = await service.get_owned(db, **args, recruitment_id=row.id)
            await getattr(service, action)(
                db,
                **args,
                recruitment_id=row.id,
                context=CONTEXT,
                payload=RecruitmentVersionCommand(
                    version=row.version, request_id=uuid4()
                ),
            )
    finally:
        release.set()
    assert (await asyncio.wait_for(sending, timeout=10)).outcome == SendOutcome.SKIPPED
    async with sessions() as db:
        assert (await InboxService(clock=clock).list_inbox(db, args["actor"].user_id))[
            1
        ] == 0


@pytest.mark.parametrize("first", ["send", "close"])
async def test_visibility_and_close_hold_scope_locks_until_commit(
    committed, clock, first
):
    sessions, service, args = committed
    row, delivery_id = await seed_due(committed, clock)
    async with sessions() as db:
        row = await service.get_owned(db, **args, recruitment_id=row.id)
    entered, release = asyncio.Event(), asyncio.Event()
    blocker = []
    sender = build_delivery_service(session_maker=sessions)
    resolver = sender._recruitment_reminder_resolver

    async def held_resolver(db, recruitment_id, cycle_id):
        result = await resolver(db, recruitment_id, cycle_id)
        if first == "send":
            blocker.append(await db.scalar(select(func.pg_backend_pid())))
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return result

    sender._recruitment_reminder_resolver = held_resolver

    class HeldAudit(AuditLogWriter):
        async def append(self, db, **kwargs):
            blocker.append(await db.scalar(select(func.pg_backend_pid())))
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return await super().append(db, **kwargs)

    async def close():
        writer = RecruitmentService(
            clock=clock, audit=HeldAudit() if first == "close" else None
        )
        async with sessions() as db:
            await writer.close(
                db,
                **args,
                recruitment_id=row.id,
                context=CONTEXT,
                payload=RecruitmentVersionCommand(
                    version=row.version, request_id=uuid4()
                ),
            )

    async def send():
        return await sender.send(delivery_id, "r3-visibility-lock")

    operations = {"send": send, "close": close}
    leader = asyncio.create_task(operations[first]())
    await asyncio.wait_for(entered.wait(), timeout=5)
    follower = asyncio.create_task(operations["close" if first == "send" else "send"]())
    try:
        await blocked(sessions, blocker[0], follower)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(leader, follower), timeout=10)
    sent = results[0 if first == "send" else 1]
    assert sent.outcome == (
        SendOutcome.SENT if first == "send" else SendOutcome.SKIPPED
    )
