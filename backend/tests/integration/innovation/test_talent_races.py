"""Real independent PostgreSQL transactions test talent command serialization."""

import asyncio
from time import monotonic
from uuid import uuid4

import pytest
from db_guard import require_test_database
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.innovation.talent_models import (
    TalentActionReceipt,
    TalentProfile,
    TalentRevision,
    TalentRevisionFile,
)
from app.modules.innovation.talent_schemas import (
    TalentFields,
    TalentRevokeContactCommand,
    TalentSaveCommand,
    TalentVersionCommand,
)
from app.modules.innovation.talent_service import TalentService
from tests.integration.innovation.talent_support import clock as clock
from tests.integration.innovation.talent_support import fields
from tests.integration.innovation.test_achievement_review_operations import account

pytestmark = pytest.mark.integration
CONTEXT = AuditContext(None, None)
TAG_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
async def committed(db_engine, clock):
    require_test_database(get_settings().database_url)
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as db:
        user, _ = await account(db, clock)
        user_id = user.id
        await db.commit()
    try:
        yield sessions, TalentService(clock=clock), Actor(user_id, Role.STUDENT)
    finally:
        # Test-only cleanup: production immutable history has no cleanup bypass.
        # Disable only these user triggers, transactionally, in a guarded test DB.
        async with sessions() as db:
            profile_ids = select(TalentProfile.id).where(
                TalentProfile.owner_user_id == user_id
            )
            await db.execute(
                update(TalentProfile)
                .where(TalentProfile.owner_user_id == user_id)
                .values(visibility="HIDDEN", public_revision_id=None)
            )
            for model in (TalentRevisionFile, TalentRevision, TalentActionReceipt):
                table = model.__tablename__
                await db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
                condition = (
                    model.actor_user_id == user_id
                    if model is TalentActionReceipt
                    else model.profile_id.in_(profile_ids)
                )
                await db.execute(delete(model).where(condition))
                await db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))
            await db.execute(
                delete(TalentProfile).where(TalentProfile.owner_user_id == user_id)
            )
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(AuditLog).where(AuditLog.actor_user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def saved(sessions, service, actor):
    async with sessions() as db:
        return (
            await service.save(
                db,
                actor=actor,
                payload=TalentSaveCommand(
                    request_id=uuid4(),
                    version=0,
                    fields=TalentFields(
                        **fields(TAG_ID),
                        contacts=[{"channel": "WECHAT", "value": "demo_talent"}],
                    ),
                ),
                context=CONTEXT,
            )
        ).current


async def test_one_profile_creation_race(committed):
    sessions, service, actor = committed
    barrier = asyncio.Barrier(2)
    payload = TalentSaveCommand(
        request_id=uuid4(), version=0, fields=TalentFields(**fields(TAG_ID))
    )

    async def run():
        async with sessions() as db:
            await barrier.wait()
            return await service.save(db, actor=actor, payload=payload, context=CONTEXT)

    results = await asyncio.wait_for(asyncio.gather(run(), run()), timeout=10)
    assert results[0].current.id == results[1].current.id
    assert sorted(r.replayed for r in results) == [False, True]
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(TalentProfile)
                .where(TalentProfile.owner_user_id == actor.user_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(TalentActionReceipt)
                .where(TalentActionReceipt.actor_user_id == actor.user_id)
            )
            == 1
        )


async def test_publish_hide_version_race(committed):
    await version_race(committed, ("publish", "hide"))


async def test_revoke_publish_race(committed):
    await version_race(committed, ("revoke_contact", "publish"))


async def version_race(committed, actions):
    sessions, service, actor = committed
    row = await saved(sessions, service, actor)
    async with sessions() as db:
        row = (
            await service.publish(
                db,
                actor=actor,
                payload=TalentVersionCommand(request_id=uuid4(), version=row.version),
                context=CONTEXT,
            )
        ).current
    barrier = asyncio.Barrier(2)

    async def run(action):
        payload = TalentVersionCommand(request_id=uuid4(), version=row.version)
        if action == "revoke_contact":
            payload = TalentRevokeContactCommand(
                request_id=uuid4(), version=row.version, channel="WECHAT"
            )
        async with sessions() as db:
            await barrier.wait()
            try:
                return await getattr(service, action)(
                    db, actor=actor, payload=payload, context=CONTEXT
                )
            except BusinessError as exc:
                return exc

    results = await asyncio.wait_for(
        asyncio.gather(*(run(a) for a in actions)), timeout=10
    )
    errors = [r for r in results if isinstance(r, BusinessError)]
    assert len(errors) == 1 and errors[0].status_code == 409
    async with sessions() as db:
        current = await service.get_owned(db, actor=actor, context=CONTEXT)
        assert current.profile.version == row.version + 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(TalentActionReceipt)
                .where(TalentActionReceipt.profile_id == row.id)
            )
            == 3
        )
        revisions = await db.scalar(
            select(func.count())
            .select_from(TalentRevision)
            .where(TalentRevision.profile_id == row.id)
        )
        assert revisions == (1 if current.profile.visibility == "HIDDEN" else 2)


async def test_account_disabled_before_publication(committed):
    sessions, service, actor = committed
    row = await saved(sessions, service, actor)
    async with sessions() as blocker:
        await blocker.execute(
            update(User).where(User.id == actor.user_id).values(status="SUSPENDED")
        )
        pid = await blocker.scalar(select(func.pg_backend_pid()))

        async def publish():
            async with sessions() as db:
                with pytest.raises(BusinessError) as error:
                    await service.publish(
                        db,
                        actor=actor,
                        payload=TalentVersionCommand(
                            request_id=uuid4(), version=row.version
                        ),
                        context=CONTEXT,
                    )
                assert error.value.status_code == 403

        task = asyncio.create_task(publish())
        try:
            async with sessions() as observer:
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    waiting = await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE :pid = ANY(pg_blocking_pids(pid)))"
                        ),
                        {"pid": pid},
                    )
                    if waiting:
                        break
                    assert not task.done(), "publisher bypassed identity lock"
                    await asyncio.sleep(0)
                else:
                    pytest.fail("publisher never blocked on account transaction")
            await blocker.commit()
            await asyncio.wait_for(task, timeout=10)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        profile = await db.get(TalentProfile, row.id)
        assert profile.visibility == "HIDDEN" and profile.public_revision_id is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(TalentActionReceipt)
                .where(TalentActionReceipt.profile_id == row.id)
            )
            == 1
        )
