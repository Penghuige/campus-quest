"""Sensitive reads serialize with revocation and current publisher suspension."""

import asyncio
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.innovation.public_recruitment_service import PublicRecruitmentService
from app.modules.innovation.recruitment_schemas import (
    RecruitmentVersionCommand,
    RevokeRecruitmentContactCommand,
)
from app.modules.innovation.recruitment_service import RecruitmentService
from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.test_achievement_review_operations import account
from tests.integration.innovation.test_recruitment_races import CONTEXT, draft
from tests.integration.innovation.test_recruitment_races import committed as committed

pytestmark = pytest.mark.integration


async def open_role(sessions, service, args):
    row = await draft(sessions, service, args)
    async with sessions() as db:
        return (
            await service.publish(
                db,
                **args,
                recruitment_id=row.id,
                payload=RecruitmentVersionCommand(
                    version=row.version, request_id=uuid4()
                ),
                context=CONTEXT,
            )
        ).current


async def blocked(sessions, blocker, task):
    async with sessions() as observer:
        stop_at = monotonic() + 5
        while monotonic() < stop_at:
            if await observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE :blocker = ANY(pg_blocking_pids(pid)))"
                ),
                {"blocker": blocker},
            ):
                return
            assert not task.done(), "sensitive operation bypassed scope locks"
            await asyncio.sleep(0.01)
        pytest.fail("no actual PostgreSQL lock wait observed")


@pytest.mark.parametrize("first", ["read", "revoke"])
async def test_read_and_revoke_both_orders_are_serialized(committed, clock, first):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    entered, release = asyncio.Event(), asyncio.Event()
    blocker = []

    class PausingAudit(AuditLogWriter):
        async def append(self, db, **kwargs):
            blocker.append(await db.scalar(select(func.pg_backend_pid())))
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return await super().append(db, **kwargs)

    async def read():
        reader = PublicRecruitmentService(
            clock=clock, audit=PausingAudit() if first == "read" else None
        )
        async with sessions() as db:
            try:
                return await reader.get_contacts(
                    db, actor=args["actor"], recruitment_id=row.id, context=CONTEXT
                )
            except BusinessError as error:
                return error

    async def revoke():
        writer = RecruitmentService(
            clock=clock, audit=PausingAudit() if first == "revoke" else None
        )
        async with sessions() as db:
            return await writer.revoke_contact(
                db,
                **args,
                recruitment_id=row.id,
                context=CONTEXT,
                payload=RevokeRecruitmentContactCommand(
                    version=row.version,
                    request_id=uuid4(),
                    channel="WECHAT",
                    close_if_last=True,
                ),
            )

    operations = {"read": read, "revoke": revoke}
    leading = asyncio.create_task(operations[first]())
    await asyncio.wait_for(entered.wait(), timeout=5)
    trailing = asyncio.create_task(
        operations["revoke" if first == "read" else "read"]()
    )
    try:
        await blocked(sessions, blocker[0], trailing)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(leading, trailing), timeout=10)
    result = results[0 if first == "read" else 1]
    if first == "read":
        assert result.contacts[0].value == "demo_partner"
    else:
        assert isinstance(result, BusinessError) and result.status_code == 404
    assert isinstance(await read(), BusinessError)


async def test_read_rechecks_publisher_after_suspension_lock(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    async with sessions() as db:
        viewer, _ = await account(db, clock)
        viewer_id = viewer.id
        await db.commit()
    try:
        async with sessions() as suspender:
            await suspender.execute(
                update(User)
                .where(User.id == args["actor"].user_id)
                .values(status="SUSPENDED")
            )
            blocker = await suspender.scalar(select(func.pg_backend_pid()))

            async def read():
                async with sessions() as db:
                    with pytest.raises(BusinessError) as error:
                        await PublicRecruitmentService(clock=clock).get_contacts(
                            db,
                            actor=Actor(user_id=viewer_id, role=Role.STUDENT),
                            recruitment_id=row.id,
                            context=CONTEXT,
                        )
                    assert error.value.status_code == 404

            task = asyncio.create_task(read())
            await blocked(sessions, blocker, task)
            await suspender.commit()
            await asyncio.wait_for(task, timeout=10)
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.actor_user_id == viewer_id,
                        AuditLog.action == "ie.recruitment.contacts_read",
                    )
                )
                == 0
            )
    finally:
        async with sessions() as db:
            await db.execute(
                delete(UserSession).where(UserSession.user_id == viewer_id)
            )
            await db.execute(delete(User).where(User.id == viewer_id))
            await db.commit()


async def test_suspension_waits_for_already_authorized_read(committed, clock):
    sessions, service, args = committed
    row = await open_role(sessions, service, args)
    entered, release = asyncio.Event(), asyncio.Event()
    blocker = []

    class PausingAudit(AuditLogWriter):
        async def append(self, db, **kwargs):
            blocker.append(await db.scalar(select(func.pg_backend_pid())))
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return await super().append(db, **kwargs)

    async def read():
        async with sessions() as db:
            return await PublicRecruitmentService(
                clock=clock, audit=PausingAudit()
            ).get_contacts(
                db, actor=args["actor"], recruitment_id=row.id, context=CONTEXT
            )

    async def suspend():
        async with sessions() as db:
            await db.execute(
                update(User)
                .where(User.id == args["actor"].user_id)
                .values(status="SUSPENDED")
            )
            await db.commit()

    reader = asyncio.create_task(read())
    await asyncio.wait_for(entered.wait(), timeout=5)
    suspender = asyncio.create_task(suspend())
    try:
        await blocked(sessions, blocker[0], suspender)
    finally:
        release.set()
    authorized, _ = await asyncio.wait_for(
        asyncio.gather(reader, suspender), timeout=10
    )
    assert authorized.contacts[0].value == "demo_partner"
