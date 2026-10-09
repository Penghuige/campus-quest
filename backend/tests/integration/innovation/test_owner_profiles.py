"""Private PII persistence, fresh authorization and real PG race checks."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.dependencies import get_access_token_codec, get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User, UserSession
from app.modules.identity.routing_common import REFRESH_COOKIE_NAME
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import OwnerProfile
from app.modules.innovation.owner_schemas import OwnerProfileSave
from app.modules.innovation.owner_service import OwnerProfileService

pytestmark = pytest.mark.integration
PATH = "/api/v1/ie/me/owner-profile"
FIELDS = {
    "name": " 小叶 ",
    "student_no": " 001234 ",
    "major": " 计算机 ",
    "grade": " 2026级 ",
}
NORMALIZED = {k: v.strip() for k, v in FIELDS.items()}


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def app(db_session, clock) -> FastAPI:
    result = create_app()

    async def db() -> AsyncIterator[AsyncSession]:
        yield db_session

    result.dependency_overrides[get_db_session] = db
    result.dependency_overrides[get_business_clock] = lambda: clock
    return result


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def user(db, role=Role.STUDENT) -> User:
    result = User(
        username=f"op-{uuid4().hex}",
        password_hash="unused",
        nickname="同学",
        role=role.value,
        status="ACTIVE",
    )
    db.add(result)
    await db.flush()
    return result


async def tokens(db, clock, person):
    _, result = await SessionService(
        clock=clock, access_codec=get_access_token_codec()
    ).issue_session(db, user=person, now=clock.now())
    return result


async def test_private_round_trip_and_safe_audit(client, db_session, clock) -> None:
    person = await user(db_session)
    auth = await tokens(db_session, clock, person)
    headers = {"Authorization": f"Bearer {auth.access_token}"}
    assert (await client.get(PATH, headers=headers)).json() == {"profile": None}
    saved = await client.put(PATH, headers=headers, json={**FIELDS, "version": 0})
    assert saved.status_code == 200
    assert saved.headers["cache-control"] == "private, no-store"
    body = saved.json()
    assert set(body) == {*FIELDS, "version", "created_at", "updated_at"}
    assert {k: body[k] for k in FIELDS} == NORMALIZED
    assert body["version"] == 1
    assert (await client.get(PATH, headers=headers)).json() == {"profile": body}
    edited = await client.put(
        PATH, headers=headers, json={**NORMALIZED, "grade": "大一", "version": 1}
    )
    assert edited.status_code == 200 and edited.json()["version"] == 2
    for version in (0, 1):
        stale = await client.put(
            PATH, headers=headers, json={**NORMALIZED, "version": version}
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "OWNER_PROFILE_VERSION_CONFLICT"
    assert (
        await client.put(
            PATH, headers=headers, json={**NORMALIZED, "version": 2, "qualified": True}
        )
    ).status_code == 422
    logs = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.actor_user_id == person.id)
        )
    )
    assert {log.action for log in logs} == {
        "IE_OWNER_PROFILE_READ",
        "IE_OWNER_PROFILE_SAVE",
    }
    assert len(logs) == 4
    for log in logs:
        assert log.target_id == str(person.id) and log.reason
        payload = repr(
            (log.details, log.before_snapshot, log.after_snapshot, log.reason)
        )
        for private in NORMALIZED.values():
            assert private not in payload


async def test_identity_cookie_and_cross_account_boundaries(
    client, db_session, clock
) -> None:
    a, b = await user(db_session), await user(db_session)
    a_tokens, b_tokens = (
        await tokens(db_session, clock, a),
        await tokens(db_session, clock, b),
    )
    ha, hb = (
        {"Authorization": f"Bearer {a_tokens.access_token}"},
        {"Authorization": f"Bearer {b_tokens.access_token}"},
    )
    assert (
        await client.put(PATH, headers=ha, json={**FIELDS, "version": 0})
    ).status_code == 200
    assert (await client.get(PATH, headers=hb)).json() == {"profile": None}
    assert (await client.get(f"{PATH}/{a.id}", headers=hb)).status_code == 404
    assert (
        await client.put(
            PATH, headers=hb, json={**FIELDS, "version": 0, "user_id": str(a.id)}
        )
    ).status_code == 422
    for cookies in ({}, {REFRESH_COOKIE_NAME: a_tokens.refresh_token}):
        client.cookies.update(cookies)
        assert (await client.get(PATH)).status_code == 401
        assert (
            await client.put(PATH, json={**FIELDS, "version": 0})
        ).status_code == 401
    client.cookies.clear()
    for role in (Role.TEACHER, Role.ADMIN, Role.STUDENT):
        person = await user(db_session, role)
        auth = await tokens(db_session, clock, person)
        headers = {"Authorization": f"Bearer {auth.access_token}"}
        if role == Role.STUDENT:
            person.status = "SUSPENDED"
            await db_session.flush()
        assert (await client.get(PATH, headers=headers)).status_code == 403
        assert (
            await client.put(PATH, headers=headers, json={**FIELDS, "version": 0})
        ).status_code == 403


async def test_service_rechecks_account_and_audit_is_atomic(db_session, clock) -> None:
    person = await user(db_session)
    actor = Actor(user_id=person.id, role=Role.STUDENT)
    service = OwnerProfileService(clock=clock)
    payload = OwnerProfileSave(**FIELDS, version=0)
    person.status = UserStatus.SUSPENDED.value
    await db_session.flush()
    with pytest.raises(BusinessError) as inactive:
        await service.save(db_session, actor=actor, payload=payload)
    assert inactive.value.code == "ACCOUNT_NOT_ACTIVE"
    person.status = UserStatus.ACTIVE.value
    await db_session.flush()

    class BrokenAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    service = OwnerProfileService(clock=clock, audit=BrokenAudit())
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with db_session.begin_nested():
            await service.save(db_session, actor=actor, payload=payload)
    assert await db_session.get(OwnerProfile, person.id) is None
    normal = OwnerProfileService(clock=clock)
    await normal.save(db_session, actor=actor, payload=payload)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        async with db_session.begin_nested():
            await service.get_owned(db_session, actor=actor)


async def test_independent_connections_create_and_update_conflict(
    db_engine, clock
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as seed:
        person = await user(seed)
        person_id = person.id
        await seed.commit()
    actor = Actor(user_id=person_id, role=Role.STUDENT)
    service = OwnerProfileService(clock=clock)
    try:

        async def save(version, grade):
            async with sessions() as db:
                try:
                    return await service.save(
                        db,
                        actor=actor,
                        payload=OwnerProfileSave(
                            **{**FIELDS, "grade": grade}, version=version
                        ),
                    )
                except BusinessError as exc:
                    return exc

        for version in (0, 1):
            results = await asyncio.gather(
                save(version, "大一"), save(version, "2026级")
            )
            failures = [
                result for result in results if isinstance(result, BusinessError)
            ]
            assert (
                len(failures) == 1
                and failures[0].code == "OWNER_PROFILE_VERSION_CONFLICT"
            )
        async with sessions() as check:
            stored = await check.get(OwnerProfile, person_id)
            assert stored is not None and stored.version == 2
            assert stored.created_at == stored.updated_at == clock.now()
            # Core check is fresh even if an Actor was created before suspension.
            await check.execute(
                update(User).where(User.id == person_id).values(status="SUSPENDED")
            )
            await check.commit()
        async with sessions() as check:
            with pytest.raises(BusinessError):
                await service.get_owned(check, actor=actor)
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(OwnerProfile).where(OwnerProfile.user_id == person_id)
            )
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.actor_user_id == person_id)
            )
            await cleanup.execute(
                delete(UserSession).where(UserSession.user_id == person_id)
            )
            await cleanup.execute(delete(User).where(User.id == person_id))
            await cleanup.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"student_no": " "},
        {"major": "a" * 121},
        {"grade": "a" * 41},
        {"version": 0},
    ],
)
async def test_database_constraints(db_session, clock, changes) -> None:
    person = await user(db_session)
    with pytest.raises(DBAPIError) as refused:
        async with db_session.begin_nested():
            db_session.add(
                OwnerProfile(
                    user_id=person.id,
                    **{**NORMALIZED, "version": 1, **changes},
                    created_at=clock.now(),
                    updated_at=clock.now(),
                )
            )
            await db_session.flush()
    expected = (
        "22001"
        if any(isinstance(v, str) and len(v) > 40 for v in changes.values())
        else "23514"
    )
    assert refused.value.orig.sqlstate == expected
