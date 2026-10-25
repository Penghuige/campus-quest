"""Independent committed PostgreSQL connections verify serialization and receipts."""

import asyncio
from time import monotonic
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import BusinessError
from app.modules.audit.context import AuditContext
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from app.modules.innovation.recruitment_models import (
    RecruitmentActionReceipt,
    RecruitmentContactAuthorization,
    RecruitmentDraft,
    RecruitmentRevision,
)
from app.modules.innovation.recruitment_schemas import (
    RecruitmentDraftCreate,
    RecruitmentFields,
    RecruitmentVersionCommand,
    RevokeRecruitmentContactCommand,
)
from app.modules.innovation.recruitment_service import RecruitmentService
from app.modules.notifications.models import Notification, NotificationDelivery
from tests.integration.innovation.recruitment_support import fields, world
from tests.integration.innovation.talent_support import clock as clock

pytestmark = pytest.mark.integration
CONTEXT = AuditContext(None, None)


@pytest.fixture
async def committed(db_engine, clock):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as db:
        user, project, _, _ = await world(db, clock)
        user_id, project_id = user.id, project.id
        await db.commit()
    service = RecruitmentService(clock=clock)
    args = {
        "actor": Actor(user_id=user_id, role=Role.STUDENT),
        "project_id": project_id,
    }
    try:
        yield sessions, service, args
    finally:
        async with sessions() as db:
            ids = select(RecruitmentDraft.id).where(
                RecruitmentDraft.project_id == project_id
            )
            await db.execute(
                update(RecruitmentDraft)
                .where(RecruitmentDraft.project_id == project_id)
                .values(status="DRAFT", public_revision_id=None)
            )
            for model in (
                RecruitmentContactAuthorization,
                RecruitmentRevision,
                RecruitmentActionReceipt,
            ):
                await db.execute(delete(model).where(model.recruitment_id.in_(ids)))
            await db.execute(
                delete(RecruitmentDraft).where(
                    RecruitmentDraft.project_id == project_id
                )
            )
            await db.execute(delete(ProjectDraft).where(ProjectDraft.id == project_id))
            await db.execute(
                delete(OwnerQualification).where(OwnerQualification.user_id == user_id)
            )
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.user_id == user_id
                )
            )
            await db.execute(
                delete(Notification).where(Notification.user_id == user_id)
            )
            await db.execute(delete(AuditLog).where(AuditLog.actor_user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def draft(sessions, service, args):
    async with sessions() as db:
        result, _ = await service.create(
            db,
            **args,
            payload=RecruitmentDraftCreate(
                request_id=uuid4(), fields=RecruitmentFields(**fields())
            ),
            context=CONTEXT,
        )
        return result


async def test_concurrent_create_same_key_is_one_row(committed):
    sessions, service, args = committed
    payload = RecruitmentDraftCreate(
        request_id=uuid4(), fields=RecruitmentFields(**fields())
    )

    async def create():
        async with sessions() as db:
            return await service.create(db, **args, payload=payload, context=CONTEXT)

    first, second = await asyncio.wait_for(
        asyncio.gather(create(), create()), timeout=10
    )
    assert first[0].id == second[0].id
    assert sorted([first[1], second[1]]) == [False, True]
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RecruitmentDraft)
                .where(RecruitmentDraft.project_id == args["project_id"])
            )
            == 1
        )


@pytest.mark.parametrize(
    "actions",
    [("publish", "publish"), ("close", "reopen"), ("revoke_contact", "publish")],
)
async def test_concurrent_version_commands_have_one_winner(committed, actions):
    sessions, service, args = committed
    row = await draft(sessions, service, args)
    args = args | {"recruitment_id": row.id}
    if actions != ("publish", "publish"):
        async with sessions() as db:
            row = (
                await service.publish(
                    db,
                    **args,
                    payload=RecruitmentVersionCommand(
                        version=row.version, request_id=uuid4()
                    ),
                    context=CONTEXT,
                )
            ).current

    async def run(action):
        payload = RecruitmentVersionCommand(version=row.version, request_id=uuid4())
        if action == "revoke_contact":
            payload = RevokeRecruitmentContactCommand(
                version=row.version,
                request_id=uuid4(),
                channel="WECHAT",
                close_if_last=True,
            )
        async with sessions() as db:
            try:
                return await getattr(service, action)(
                    db, **args, payload=payload, context=CONTEXT
                )
            except BusinessError as exc:
                return exc

    results = await asyncio.wait_for(
        asyncio.gather(*(run(a) for a in actions)), timeout=10
    )
    errors = [r for r in results if isinstance(r, BusinessError)]
    assert len(errors) == 1
    assert errors[0].status_code == 409
    async with sessions() as db:
        current = await service.get_owned(db, **args)
        assert current.version == row.version + 1


async def test_suspended_account_cannot_publish_after_waiting_for_user_lock(committed):
    sessions, service, args = committed
    row = await draft(sessions, service, args)
    async with sessions() as suspender:
        await suspender.execute(
            update(User)
            .where(User.id == args["actor"].user_id)
            .values(status="SUSPENDED")
        )

        async def publish():
            async with sessions() as db:
                with pytest.raises(BusinessError) as error:
                    await service.publish(
                        db,
                        **args,
                        recruitment_id=row.id,
                        payload=RecruitmentVersionCommand(
                            version=row.version, request_id=uuid4()
                        ),
                        context=CONTEXT,
                    )
                assert error.value.status_code == 403

        task = asyncio.create_task(publish())
        blocker_pid = await suspender.scalar(select(func.pg_backend_pid()))
        async with sessions() as observer:
            stop_at = monotonic() + 5
            while monotonic() < stop_at:
                waiting = await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :blocker = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"blocker": blocker_pid},
                )
                if waiting:
                    break
                assert not task.done(), "publisher bypassed the current user lock"
                await asyncio.sleep(0.01)
            else:
                pytest.fail("publisher did not wait on the suspension transaction")
        await suspender.commit()
        await asyncio.wait_for(task, timeout=10)
    async with sessions() as db:
        stored = await db.get(RecruitmentDraft, UUID(str(row.id)))
        assert stored.status == "DRAFT"
        assert stored.public_revision_id is None
