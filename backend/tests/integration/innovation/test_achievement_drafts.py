"""Private child resources on real sessions and independent PostgreSQL connections."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.dependencies import get_access_token_codec, get_business_clock
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.identity.routing_common import REFRESH_COOKIE_NAME
from app.modules.identity.session_service import SessionService
from app.modules.innovation.achievement_schemas import (
    AchievementDraftCreate,
    AchievementDraftUpdate,
)
from app.modules.innovation.achievement_service import AchievementDraftService
from app.modules.innovation.models import (
    AchievementDraft,
    OperationsGrant,
    ProjectDraft,
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/ie/me/project-drafts"
CONTENT = {
    "title": " 原型一 ",
    "description": " <script>仅作为文字</script> ",
    "work_url": " https://example.com/work ",
    "award_text": " 尚未核实的立项说明 ",
}


@pytest.fixture
def clock():
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


async def user(db, role=Role.STUDENT):
    person = User(
        username=f"achievement-{uuid4().hex}",
        password_hash="unused",
        nickname="成果同学",
        role=role.value,
        status="ACTIVE",
    )
    db.add(person)
    await db.flush()
    return person


async def project(db, owner):
    row = ProjectDraft(
        owner_user_id=owner.id,
        creation_request_id=uuid4(),
        creation_payload_fingerprint="0" * 64,
        title="私有父项目",
    )
    db.add(row)
    await db.flush()
    return row


def actor(person):
    return Actor(user_id=person.id, role=Role(person.role))


def creation(**changes):
    return {**CONTENT, "request_id": str(uuid4()), **changes}


def edit(row, **changes):
    return {key: changes.get(key, row[key]) for key in (*CONTENT, "version")}


@pytest_asyncio.fixture
async def client(db_session, clock) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def tokens(db, clock, person):
    return (
        await SessionService(
            clock=clock, access_codec=get_access_token_codec()
        ).issue_session(db, user=person, now=clock.now())
    )[1]


async def test_round_trip_idempotence_and_safe_audit(client, db_session, clock):
    owner = await user(db_session)
    parent = await project(db_session, owner)
    auth = await tokens(db_session, clock, owner)
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "X-Request-ID": "achievement-request-1",
    }
    path = f"{BASE}/{parent.id}/achievements"
    body = creation()
    saved = await client.post(path, json=body, headers=headers)
    assert saved.status_code == 201
    assert saved.headers["cache-control"] == "private, no-store"
    row = saved.json()
    assert set(row) == {"id", *CONTENT, "version", "created_at", "updated_at"}
    assert {key: row[key] for key in CONTENT} == {
        key: value.strip() for key, value in CONTENT.items()
    }
    assert (await client.get(f"{path}/{row['id']}", headers=headers)).json() == row
    for title in ("第二份", "第三份"):
        assert (
            await client.post(path, json=creation(title=title), headers=headers)
        ).status_code == 201
    page = (
        await client.get(path, params={"limit": 1, "offset": 1}, headers=headers)
    ).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    changed = await client.patch(
        f"{path}/{row['id']}",
        headers=headers,
        json=edit(row, description="修改后的作品记录"),
    )
    assert changed.status_code == 200 and changed.json()["version"] == 2
    retry = await client.post(path, json=body, headers=headers)
    assert retry.status_code == 200 and retry.json() == changed.json()
    assert (
        await client.post(path, json={**body, "title": "换了内容"}, headers=headers)
    ).status_code == 409
    assert (
        await client.patch(f"{path}/{row['id']}", json=edit(row), headers=headers)
    ).status_code == 409
    logs = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.actor_user_id == owner.id)
        )
    )
    assert {
        "IE_ACHIEVEMENT_DRAFT_CREATE",
        "IE_ACHIEVEMENT_DRAFT_UPDATE",
        "IE_ACHIEVEMENT_DRAFT_READ",
    } <= {log.action for log in logs}
    for log in logs:
        assert log.request_id == "achievement-request-1"
        for content in CONTENT.values():
            assert content.strip() not in repr(
                (log.details, log.before_snapshot, log.after_snapshot)
            )


async def test_parent_child_and_account_boundaries(client, db_session, clock):
    a, b = await user(db_session), await user(db_session)
    pa, pb = await project(db_session, a), await project(db_session, b)
    other_project = await project(db_session, a)
    ta, tb = await tokens(db_session, clock, a), await tokens(db_session, clock, b)
    ha, hb = (
        {"Authorization": f"Bearer {ta.access_token}"},
        {"Authorization": f"Bearer {tb.access_token}"},
    )
    path = f"{BASE}/{pa.id}/achievements"
    row = (await client.post(path, headers=ha, json=creation())).json()
    # An operations grant is not a private-content read grant.
    db_session.add(
        OperationsGrant(
            user_id=b.id,
            enabled=True,
            version=1,
            changed_by=a.id,
            updated_at=clock.now(),
        )
    )
    await db_session.flush()
    for target, auth in [(pa.id, hb), (uuid4(), ha)]:
        prefix = f"{BASE}/{target}/achievements"
        assert (await client.get(prefix, headers=auth)).status_code == 404
        assert (
            await client.post(prefix, headers=auth, json=creation())
        ).status_code == 404
        assert (
            await client.get(f"{prefix}/{row['id']}", headers=auth)
        ).status_code == 404
        assert (
            await client.patch(f"{prefix}/{row['id']}", headers=auth, json=edit(row))
        ).status_code == 404
    for target, auth in [(pb.id, hb), (other_project.id, ha)]:
        prefix = f"{BASE}/{target}/achievements"
        assert (
            await client.get(f"{prefix}/{row['id']}", headers=auth)
        ).status_code == 404
        assert (
            await client.patch(f"{prefix}/{row['id']}", headers=auth, json=edit(row))
        ).status_code == 404
        assert (await client.get(prefix, headers=auth)).json()["items"] == []
    for bad in (
        {"approved": True},
        {"owner_id": str(b.id)},
        {"project_id": str(pb.id)},
    ):
        assert (
            await client.post(path, headers=ha, json=creation(**bad))
        ).status_code == 422
    client.cookies.set(REFRESH_COOKIE_NAME, ta.refresh_token)
    for url in (path, f"{path}/{row['id']}"):
        assert (await client.get(url)).status_code == 401
    assert (await client.post(path, json=creation())).status_code == 401
    assert (
        await client.patch(f"{path}/{row['id']}", json=edit(row))
    ).status_code == 401
    client.cookies.clear()
    for role in (Role.TEACHER, Role.ADMIN, Role.STUDENT):
        person = await user(db_session, role)
        token = await tokens(db_session, clock, person)
        if role == Role.STUDENT:
            person.status = "SUSPENDED"
            await db_session.flush()
        auth = {"Authorization": f"Bearer {token.access_token}"}
        assert (await client.get(path, headers=auth)).status_code == 403
        assert (
            await client.post(path, headers=auth, json=creation())
        ).status_code == 403
        assert (
            await client.patch(f"{path}/{row['id']}", headers=auth, json=edit(row))
        ).status_code == 403


async def test_audit_failure_and_fresh_service_state(db_session, clock):
    person = await user(db_session)
    parent = await project(db_session, person)
    who = actor(person)

    class BrokenAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    service = AchievementDraftService(clock=clock, audit=BrokenAudit())
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with db_session.begin_nested():
            await service.create(
                db_session,
                actor=who,
                project_id=parent.id,
                payload=AchievementDraftCreate(**creation()),
            )
    assert not await db_session.scalar(
        select(func.count())
        .select_from(AchievementDraft)
        .where(AchievementDraft.project_id == parent.id)
    )
    for role, status in [("STUDENT", "BANNED"), ("ADMIN", "ACTIVE")]:
        person.role, person.status = role, status
        await db_session.flush()
        with pytest.raises(BusinessError):
            await service.list_owned(
                db_session, actor=who, project_id=parent.id, limit=20, offset=0
            )


async def test_independent_creation_and_version_races(db_engine, clock):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        person = await user(seed)
        parent = await project(seed, person)
        uid, pid, who = person.id, parent.id, actor(person)
        await seed.commit()
    service = AchievementDraftService(clock=clock)
    command = AchievementDraftCreate(**creation())
    try:

        async def create():
            async with sessions() as db:
                return await service.create(
                    db, actor=who, project_id=pid, payload=command
                )

        results = await asyncio.gather(create(), create())
        assert sorted(created for _, created in results) == [False, True]
        record = results[0][0]
        assert record.id == results[1][0].id

        async def change(text):
            async with sessions() as db:
                try:
                    return await service.update(
                        db,
                        actor=who,
                        project_id=pid,
                        achievement_id=record.id,
                        payload=AchievementDraftUpdate(
                            **edit(record.model_dump(), description=text)
                        ),
                    )
                except BusinessError as exc:
                    return exc

        outcomes = await asyncio.gather(change("第一处"), change("第二处"))
        assert sum(isinstance(result, BusinessError) for result in outcomes) == 1
        async with sessions() as check:
            row = await check.get(AchievementDraft, record.id)
            assert row.version == 2
            assert row.description in ("第一处", "第二处")
            await check.execute(
                update(User).where(User.id == uid).values(status="SUSPENDED")
            )
            await check.commit()
        async with sessions() as check:
            with pytest.raises(BusinessError):
                await service.get_owned(
                    check, actor=who, project_id=pid, achievement_id=record.id
                )
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AchievementDraft).where(AchievementDraft.project_id == pid)
            )
            await cleanup.execute(delete(AuditLog).where(AuditLog.actor_user_id == uid))
            await cleanup.execute(delete(ProjectDraft).where(ProjectDraft.id == pid))
            await cleanup.execute(delete(User).where(User.id == uid))
            await cleanup.commit()


async def test_waiting_create_observes_committed_suspension(db_engine, clock):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        person = await user(seed)
        parent = await project(seed, person)
        uid, pid, who = person.id, parent.id, actor(person)
        await seed.commit()
    started = asyncio.get_running_loop().create_future()
    task = None

    async def create():
        async with sessions() as db:
            started.set_result(await db.scalar(text("select pg_backend_pid()")))
            try:
                return await AchievementDraftService(clock=clock).create(
                    db,
                    actor=who,
                    project_id=pid,
                    payload=AchievementDraftCreate(**creation()),
                )
            except BusinessError as exc:
                return exc

    try:
        async with sessions() as suspension:
            await suspension.execute(
                update(User).where(User.id == uid).values(status="SUSPENDED")
            )
            task = asyncio.create_task(create())
            backend_pid = await asyncio.wait_for(started, 5)
            async with sessions() as monitor:
                async with asyncio.timeout(10):
                    while not await monitor.scalar(
                        text(
                            "select exists(select 1 from pg_stat_activity "
                            "where pid=:pid and wait_event_type='Lock')"
                        ),
                        {"pid": backend_pid},
                    ):
                        await asyncio.sleep(0.02)
            assert not task.done()
            await suspension.commit()
        result = await asyncio.wait_for(task, 5)
        assert isinstance(result, BusinessError) and result.code == "ACCOUNT_NOT_ACTIVE"
        async with sessions() as check:
            assert not await check.scalar(
                select(func.count())
                .select_from(AchievementDraft)
                .where(AchievementDraft.project_id == pid)
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(AchievementDraft).where(AchievementDraft.project_id == pid)
            )
            await cleanup.execute(delete(AuditLog).where(AuditLog.actor_user_id == uid))
            await cleanup.execute(delete(ProjectDraft).where(ProjectDraft.id == pid))
            await cleanup.execute(delete(User).where(User.id == uid))
            await cleanup.commit()


async def test_read_and_update_cannot_succeed_without_audit(db_session, clock):
    person = await user(db_session)
    parent = await project(db_session, person)
    pid, who = parent.id, actor(person)
    record, _ = await AchievementDraftService(clock=clock).create(
        db_session,
        actor=who,
        project_id=pid,
        payload=AchievementDraftCreate(**creation()),
    )

    class BrokenAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    service = AchievementDraftService(clock=clock, audit=BrokenAudit())
    for operation in (
        lambda: service.list_owned(
            db_session, actor=who, project_id=pid, limit=20, offset=0
        ),
        lambda: service.get_owned(
            db_session, actor=who, project_id=pid, achievement_id=record.id
        ),
        lambda: service.update(
            db_session,
            actor=who,
            project_id=pid,
            achievement_id=record.id,
            payload=AchievementDraftUpdate(
                **edit(record.model_dump(), description="不得保存的更新")
            ),
        ),
    ):
        with pytest.raises(RuntimeError, match="audit unavailable"):
            async with db_session.begin_nested():
                await operation()
    row = await db_session.get(AchievementDraft, record.id, populate_existing=True)
    assert row.version == 1 and row.description == record.description


@pytest.mark.parametrize(
    "change,sqlstate",
    [
        ({"title": ""}, "23514"),
        ({"version": 0}, "23514"),
        ({"project_id": uuid4()}, "23503"),
        ({"work_url": "javascript:evil()"}, "23514"),
        ({"award_text": "a" * 1001}, "23514"),
    ],
)
async def test_database_constraints(db_session, clock, change, sqlstate):
    person = await user(db_session)
    parent = await project(db_session, person)
    with pytest.raises(DBAPIError) as refused:
        async with db_session.begin_nested():
            db_session.add(
                AchievementDraft(
                    project_id=parent.id,
                    creation_request_id=uuid4(),
                    creation_payload_fingerprint="0" * 64,
                    title="成果",
                    version=1,
                    created_at=clock.now(),
                    updated_at=clock.now(),
                    **{
                        k: v
                        for k, v in change.items()
                        if k not in ("title", "version", "project_id")
                    },
                )
            )
            # Apply fields that share constructor defaults without duplicate kwargs.
            row = list(db_session.new)[0]
            for key, value in change.items():
                setattr(row, key, value)
            await db_session.flush()
    assert refused.value.orig.sqlstate == sqlstate
