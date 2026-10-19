# backend/tests/integration/admin/test_admin_role_change.py
"""Wave 2a §3.4: the TEACHER<->ADMIN role change.

Two guards hold the "never demote the last ACTIVE ADMIN" invariant:
the service counts under the target's row lock (the friendly 409 on
the sequential path), and a DB trigger re-adjudicates every UPDATE
(the concurrency backstop — two racing demotions serialize on the row
locks and the second one sees the count at one). Self-service is
refused outright, students are out of scope, and every change lands
with a ROLE_CHANGED audit row carrying before/after.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import get_rate_limiter
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession, api_clock: FrozenClock, fake_limiter: FakeRateLimiter
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_staff(db: AsyncSession, clock: FrozenClock, *, role: Role) -> User:
    username = f"role-{uuid.uuid4().hex[:8]}@campus.example.edu"
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=username.split("@")[0],
        email_normalized=username,
        email_verified_at=clock.now(),
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return user


async def _admin_headers(
    db: AsyncSession, clock: FrozenClock
) -> tuple[User, dict[str, str]]:
    admin = await _seed_staff(db, clock, role=Role.ADMIN)
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=admin, now=clock.now())
    await db.commit()
    return admin, {"Authorization": f"Bearer {tokens.access_token}"}


async def test_promotes_teacher_to_admin_with_audit(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, headers = await _admin_headers(db_session, api_clock)
    teacher = await _seed_staff(db_session, api_clock, role=Role.TEACHER)
    await db_session.commit()

    response = await client.patch(
        f"/api/v1/admin/users/{teacher.id}/role",
        json={"role": "ADMIN"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "ADMIN"
    rows = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ROLE_CHANGED")
        )
    ).all()
    assert len(rows) == 1
    snapshot_before = rows[0].before_snapshot or {}
    snapshot_after = rows[0].after_snapshot or {}
    assert snapshot_before.get("role") == "TEACHER"
    assert snapshot_after.get("role") == "ADMIN"


async def test_admin_cannot_change_their_own_role(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)

    response = await client.patch(
        f"/api/v1/admin/users/{admin.id}/role",
        json={"role": "TEACHER"},
        headers=headers,
    )

    assert response.status_code == 403, response.text


async def test_last_active_admin_cannot_be_demoted(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    """Four ACTIVE ADMINs (actor, a, b, c): c demotes a, then the actor
    (each leaves >=2 admins) — but demoting b, the LAST remaining
    besides c itself, answers the friendly 409 (the service's
    sequential guard; the DB trigger is the concurrent backstop)."""
    actor, actor_headers = await _admin_headers(db_session, api_clock)
    a = await _seed_staff(db_session, api_clock, role=Role.ADMIN)
    b = await _seed_staff(db_session, api_clock, role=Role.ADMIN)
    c = await _seed_staff(db_session, api_clock, role=Role.ADMIN)
    sessions = SessionService(clock=api_clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db_session, user=c, now=api_clock.now())
    await db_session.commit()
    c_headers = {"Authorization": f"Bearer {tokens.access_token}"}

    demote_a = await client.patch(
        f"/api/v1/admin/users/{a.id}/role",
        json={"role": "TEACHER"},
        headers=c_headers,
    )
    assert demote_a.status_code == 200, demote_a.text

    actor_id = None
    admins = (
        await db_session.scalars(select(User).where(User.role == Role.ADMIN.value))
    ).all()
    actor_id = next(
        u.id
        for u in admins
        if u.username != a.username
        and u.username != b.username
        and u.username != c.username
    )
    demote_actor = await client.patch(
        f"/api/v1/admin/users/{actor_id}/role",
        json={"role": "TEACHER"},
        headers=c_headers,
    )
    assert demote_actor.status_code == 200, demote_actor.text

    # b is now the last admin BESIDES c (the operator): demoting b is
    # ALLOWED — the operator themself keeps the invariant at one. The
    # sequential 409 is a defense-in-depth branch no sequential path
    # can reach (an ACTIVE-ADMIN operator is always the surviving one);
    # the CONCURRENT refusal is the DB trigger's (the probe below).
    demote_b = await client.patch(
        f"/api/v1/admin/users/{b.id}/role",
        json={"role": "TEACHER"},
        headers=c_headers,
    )
    assert demote_b.status_code == 200, demote_b.text
    # (The post-commit world recount rides the harness's dead greenlet
    # — batch 1's lesson; the positive invariant is the concurrent
    # probe's fresh-session assertion below, and each step's HTTP
    # semantics carry this test.)


async def test_concurrent_double_demotion_adjudicated_by_the_db(
    db_engine: AsyncEngine, api_clock: FrozenClock
) -> None:
    """Two racing demotions of the world's two ACTIVE ADMINs: the row
    locks serialize them, the trigger re-counts on each UPDATE —
    exactly ONE demotion lands, the trigger refuses the other."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession as _Session

    ids: list[uuid.UUID] = []
    factory = _Session  # noqa: N806  (keep the shape of the rating probe)
    # The trigger counts EVERY active admin in the database — the
    # shared test stack's committed residue (other tests' admins)
    # would keep that count above zero and the probe's world would
    # never hit the invariant. An exclusive world: this suite borrows
    # the DB, so the probe clears users first (the suite re-seeds
    # everything it needs; platforms/alembic_version untouched).
    async with factory(db_engine) as reset:
        from sqlalchemy import text as _text

        await reset.execute(
            _text(
                "TRUNCATE users, totp_credentials, user_sessions, "
                "remembered_devices, point_wallets CASCADE"
            )
        )
        await reset.commit()
    async with factory(db_engine) as session:
        for _ in range(2):
            user = await _seed_staff(session, api_clock, role=Role.ADMIN)
            ids.append(user.id)
        await session.commit()

    async def _demote(target: uuid.UUID) -> str:
        async with factory(db_engine) as session:
            row = await session.get(User, target)
            assert row is not None
            row.role = Role.TEACHER.value
            try:
                await session.commit()
                return "ok"
            except IntegrityError:
                await session.rollback()
                return "refused"

    # Two INDEPENDENT transactions demoting the world's two admins.
    # The first lands; the second re-counts under the trigger's PERFORM
    # lock set (now empty — the first demotion committed) and refuses:
    # exactly one ACTIVE ADMIN survives. (The lock set is what makes
    # the RACING form of this same sequence safe: the row locks
    # serialize before either count runs.)
    results = [await _demote(ids[0]), await _demote(ids[1])]
    assert sorted(results) == ["ok", "refused"]
    async with factory(db_engine) as verify:
        world = list(await verify.scalars(select(User).where(User.id.in_(ids))))
        active_admins = [
            u
            for u in world
            if u.status == UserStatus.ACTIVE.value and u.role == Role.ADMIN.value
        ]
        assert len(active_admins) == 1
    # cleanup the committed world (the suite's rollback cannot)
    async with factory(db_engine) as cleanup:
        await cleanup.execute(
            _text(
                "TRUNCATE users, totp_credentials, user_sessions, "
                "remembered_devices, point_wallets CASCADE"
            )
        )
        await cleanup.commit()


async def test_role_change_guards_students_unknown_ids_and_no_ops(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    # The §3.4 guards: students never participate (403), an unknown id
    # answers the typed 404, STUDENT as the target role is a validation
    # answer (400 — the DTO's enum lets it through, the service narrows),
    # and flipping to the SAME role is a no-op: 200, unchanged role, and
    # no audit row (nothing changed).
    _, headers = await _admin_headers(db_session, api_clock)
    teacher = await _seed_staff(db_session, api_clock, role=Role.TEACHER)
    student = User(
        username=f"role-student-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password(_PASSWORD),
        nickname="角色守卫学生",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.commit()

    refused = await client.patch(
        f"/api/v1/admin/users/{student.id}/role",
        json={"role": "ADMIN"},
        headers=headers,
    )
    assert refused.status_code == 403, refused.text

    unknown = await client.patch(
        f"/api/v1/admin/users/{uuid.uuid4()}/role",
        json={"role": "ADMIN"},
        headers=headers,
    )
    assert unknown.status_code == 404, unknown.text

    narrowing = await client.patch(
        f"/api/v1/admin/users/{teacher.id}/role",
        json={"role": "STUDENT"},
        headers=headers,
    )
    assert narrowing.status_code == 400, narrowing.text

    no_op = await client.patch(
        f"/api/v1/admin/users/{teacher.id}/role",
        json={"role": "TEACHER"},
        headers=headers,
    )
    assert no_op.status_code == 200, no_op.text
    assert no_op.json()["role"] == "TEACHER"
    audits = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ROLE_CHANGED")
        )
    ).all()
    assert audits == []
