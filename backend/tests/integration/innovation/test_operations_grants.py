"""Scoped grants, actual HTTP guards and independent PostgreSQL transactions."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
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
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import OperationsGrant
from app.modules.innovation.operations_schemas import OperationsGrantSave
from app.modules.innovation.operations_service import OperationsGrantService
from app.modules.system.service import SystemSettingService

pytestmark = pytest.mark.integration
ADMIN_PATH = "/api/v1/admin/ie/operations-grants"
SELF_PATH = "/api/v1/ie/me/capabilities"


@pytest.fixture
def clock():
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


async def user(db, role=Role.STUDENT, *, totp=True):
    person = User(
        username=f"ops-{uuid4().hex}",
        password_hash="unused",
        nickname="运营测试",
        role=role.value,
        status="ACTIVE",
    )
    db.add(person)
    await db.flush()
    if role == Role.ADMIN and totp:
        db.add(
            TotpCredential(
                user_id=person.id,
                secret_encrypted=b"unused",
                confirmed_at=datetime.now(UTC),
            )
        )
        await db.flush()
    return person


def actor(person):
    return Actor(user_id=person.id, role=Role(person.role))


def payload(enabled=True, version=0):
    return OperationsGrantSave(enabled=enabled, version=version, reason="指定测试运营")


@pytest_asyncio.fixture
async def client(db_session, clock) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as result:
        yield result


async def headers(db, clock, person):
    _, token = await SessionService(
        clock=clock, access_codec=get_access_token_codec()
    ).issue_session(db, user=person, now=clock.now())
    return {"Authorization": f"Bearer {token.access_token}"}


async def test_http_lifecycle_and_scope(client, db_session, clock):
    admin, student = await user(db_session, Role.ADMIN), await user(db_session)
    ah, sh = (
        await headers(db_session, clock, admin),
        await headers(db_session, clock, student),
    )
    path = f"{ADMIN_PATH}/{student.id}"
    assert (await client.get(SELF_PATH, headers=sh)).json() == {
        "operations_enabled": False
    }
    assert (await client.get(path, headers=ah)).json() == {
        "user_id": str(student.id),
        "enabled": False,
        "version": 0,
    }
    for enabled, version in [(True, 0), (False, 1), (True, 2)]:
        saved = await client.put(
            path, headers=ah, json=payload(enabled, version).model_dump()
        )
        assert saved.status_code == 200 and saved.json()["version"] == version + 1
        assert saved.headers["cache-control"] == "private, no-store"
        assert (await client.get(SELF_PATH, headers=sh)).json() == {
            "operations_enabled": enabled
        }
    stale = await client.put(path, headers=ah, json=payload(False, 1).model_dump())
    assert stale.status_code == 409
    assert (await client.get(SELF_PATH, headers=sh)).json()[
        "operations_enabled"
    ] is True
    duplicate = await client.put(path, headers=ah, json=payload(True, 3).model_dump())
    assert duplicate.status_code == 409
    # This grant does not alter the global role or bypass existing staff guards.
    assert (await client.get("/api/v1/me", headers=sh)).json()["role"] == "STUDENT"
    for protected in ("/api/v1/admin/users", "/api/v1/admin/rewards"):
        assert (await client.get(protected, headers=sh)).status_code == 403
    assert (
        await client.post("/api/v1/teacher/tasks", headers=sh, json={})
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/ie/me/owner-profile/{admin.id}", headers=sh)
    ).status_code == 404
    assert (
        await client.put(path, headers=sh, json=payload().model_dump())
    ).status_code == 403
    logs = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.target_type == "ie_operations_grant")
        )
    )
    assert (
        len([log for log in logs if log.action == "IE_OPERATIONS_GRANT_CHANGED"]) == 3
    )
    for log in logs:
        assert student.username not in repr(
            (log.details, log.before_snapshot, log.after_snapshot)
        )


async def test_http_guards(client, db_session, clock):
    student = await user(db_session)
    path = f"{ADMIN_PATH}/{student.id}"
    assert (await client.get(path)).status_code == 401
    assert (await client.put(path, json=payload().model_dump())).status_code == 401
    for role, status, confirmed in [
        (Role.STUDENT, "ACTIVE", True),
        (Role.TEACHER, "ACTIVE", True),
        (Role.ADMIN, "SUSPENDED", True),
        (Role.ADMIN, "ACTIVE", False),
    ]:
        person = await user(db_session, role, totp=confirmed)
        auth = await headers(db_session, clock, person)
        person.status = status
        await db_session.flush()
        assert (await client.get(path, headers=auth)).status_code == 403
        assert (
            await client.put(path, headers=auth, json=payload().model_dump())
        ).status_code == 403


async def test_actual_store_network_policy_and_validation(client, db_session, clock):
    admin, student = await user(db_session, Role.ADMIN), await user(db_session)
    auth = await headers(db_session, clock, admin)
    path = f"{ADMIN_PATH}/{student.id}"
    for body in (
        {**payload().model_dump(), "role": "ADMIN"},
        {**payload().model_dump(), "reason": " "},
    ):
        assert (await client.put(path, headers=auth, json=body)).status_code == 422
    # The HTTP test peer is 127.0.0.1; real persisted policy excludes it.
    settings = SystemSettingService()
    await settings.set_management_network_policy(
        db_session, actor=actor(admin), cidrs=["192.0.2.0/24"], reason="测试网络限制"
    )
    await settings.set_management_network_policy(
        db_session, actor=actor(admin), enabled=True, reason="测试网络限制"
    )
    assert (await client.get(path, headers=auth)).status_code == 403
    assert (
        await client.put(path, headers=auth, json=payload().model_dump())
    ).status_code == 403
    assert await db_session.get(OperationsGrant, student.id) is None


async def test_waiting_grant_observes_committed_suspension(db_engine, clock):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        admin, student = await user(seed, Role.ADMIN), await user(seed)
        ids = [admin.id, student.id]
        aa = actor(admin)
        await seed.commit()
    service = OperationsGrantService(clock=clock)
    started = asyncio.get_running_loop().create_future()
    task = None

    async def waiting_grant():
        async with sessions() as db:
            started.set_result(await db.scalar(text("select pg_backend_pid()")))
            try:
                return await service.save(
                    db, actor=aa, user_id=ids[1], payload=payload()
                )
            except BusinessError as exc:
                return exc

    try:
        async with sessions() as suspension:
            await suspension.execute(
                update(User).where(User.id == ids[1]).values(status="SUSPENDED")
            )
            task = asyncio.create_task(waiting_grant())
            pid = await asyncio.wait_for(started, 5)
            async with sessions() as monitor:
                async with asyncio.timeout(10):
                    while not await monitor.scalar(
                        text(
                            "select exists(select 1 from pg_stat_activity "
                            "where pid=:pid and wait_event_type='Lock')"
                        ),
                        {"pid": pid},
                    ):
                        await asyncio.sleep(0.02)
            assert not task.done()
            await suspension.commit()
        refused = await asyncio.wait_for(task, 5)
        assert (
            isinstance(refused, BusinessError) and refused.code == "PERMISSION_DENIED"
        )
        async with sessions() as check:
            assert await check.get(OperationsGrant, ids[1]) is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with sessions() as db:
            await db.execute(
                delete(OperationsGrant).where(OperationsGrant.user_id.in_(ids))
            )
            await db.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(ids)))
            await db.execute(
                delete(TotpCredential).where(TotpCredential.user_id.in_(ids))
            )
            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.commit()


async def test_grant_database_constraints(db_session, clock):
    admin, student = await user(db_session, Role.ADMIN), await user(db_session)
    for target, version, state in [(student.id, 0, "23514"), (uuid4(), 1, "23503")]:
        with pytest.raises(DBAPIError) as refused:
            async with db_session.begin_nested():
                db_session.add(
                    OperationsGrant(
                        user_id=target,
                        enabled=True,
                        version=version,
                        changed_by=admin.id,
                        updated_at=clock.now(),
                    )
                )
                await db_session.flush()
        assert refused.value.orig.sqlstate == state


async def test_target_rules_fresh_admin_and_revocation(db_session, clock):
    admin, target = await user(db_session, Role.ADMIN), await user(db_session)
    service = OperationsGrantService(clock=clock)
    aa = actor(admin)
    for role, status in [("TEACHER", "ACTIVE"), ("STUDENT", "SUSPENDED")]:
        target.role, target.status = role, status
        await db_session.flush()
        with pytest.raises(BusinessError):
            await service.save(
                db_session, actor=aa, user_id=target.id, payload=payload()
            )
    target.role, target.status = "STUDENT", "ACTIVE"
    await db_session.flush()
    await service.save(db_session, actor=aa, user_id=target.id, payload=payload())
    target.status = "SUSPENDED"
    await db_session.flush()
    with pytest.raises(BusinessError):
        await service.capabilities(db_session, actor=actor(target))
    target.role = "TEACHER"
    await db_session.flush()
    assert not (
        await service.save(
            db_session, actor=aa, user_id=target.id, payload=payload(False, 1)
        )
    ).enabled
    for role, status in [("STUDENT", "ACTIVE"), ("ADMIN", "BANNED")]:
        admin.role, admin.status = role, status
        await db_session.flush()
        with pytest.raises(BusinessError):
            await service.read(db_session, actor=aa, user_id=target.id)
    admin.role, admin.status = "ADMIN", "ACTIVE"
    await db_session.flush()
    await db_session.execute(
        delete(TotpCredential).where(TotpCredential.user_id == admin.id)
    )
    with pytest.raises(BusinessError) as refused:
        await service.read(db_session, actor=aa, user_id=target.id)
    assert refused.value.status_code == 403


async def test_audit_failure_rolls_back_grant(db_session, clock):
    admin, student = await user(db_session, Role.ADMIN), await user(db_session)

    class BrokenAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    service = OperationsGrantService(clock=clock, audit=BrokenAudit())
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with db_session.begin_nested():
            await service.save(
                db_session, actor=actor(admin), user_id=student.id, payload=payload()
            )
    assert await db_session.get(OperationsGrant, student.id) is None


async def test_independent_grants_and_stale_revoke(db_engine, clock):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        admin, student = await user(seed, Role.ADMIN), await user(seed)
        ids = [admin.id, student.id]
        aa = actor(admin)
        await seed.commit()
    service = OperationsGrantService(clock=clock)
    try:

        async def save(value):
            async with sessions() as db:
                try:
                    return await service.save(
                        db, actor=aa, user_id=ids[1], payload=value
                    )
                except BusinessError as exc:
                    return exc

        outcomes = await asyncio.gather(save(payload()), save(payload()))
        assert sum(isinstance(result, BusinessError) for result in outcomes) == 1
        assert [
            result.code for result in outcomes if isinstance(result, BusinessError)
        ] == ["CONFLICT"]
        assert (await save(payload(False, 1))).version == 2
        assert (await save(payload(True, 2))).version == 3
        assert isinstance(await save(payload(False, 1)), BusinessError)
        async with sessions() as check:
            row = await check.get(OperationsGrant, ids[1])
            assert row.enabled and row.version == 3
            logs = list(
                await check.scalars(
                    select(AuditLog).where(AuditLog.actor_user_id == ids[0])
                )
            )
            assert len(logs) == 3
    finally:
        async with sessions() as db:
            await db.execute(
                delete(OperationsGrant).where(OperationsGrant.user_id.in_(ids))
            )
            await db.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(ids)))
            await db.execute(
                delete(TotpCredential).where(TotpCredential.user_id.in_(ids))
            )
            await db.execute(delete(User).where(User.id.in_(ids)))
            await db.commit()
