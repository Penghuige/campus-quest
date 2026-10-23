# backend/tests/integration/admin/test_admin_password_reset_link.py
"""Wave 2a §3.2: the admin-issued one-time password-reset link.

An admin mints a 48-hour, single-use reset credential for another
staff account (never their own): urlsafe32 plaintext shown exactly
once, SHA-256 at rest in Redis (48h TTL is the storage's own expiry),
ISSUED audited at mint and CONSUMED at spend. Spending sets the new
password, revokes every live session (the password-change semantics),
and the credential dies with the GETDEL — a replay answers the uniform
invalid.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.device_credentials import issue as issue_device
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import get_identity_redis, get_rate_limiter
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

# Anchored to the real now (the codec's iat wall clock).
_T0 = datetime.now(UTC).replace(microsecond=0)
_ADMIN_PW = "correct-horse-battery"
_TARGET_PW = "target-original-password"
_RESET_REDIS_DB = 15
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_RESET_REDIS_DB}"))


@pytest_asyncio.fixture
async def api_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    fake_limiter: FakeRateLimiter,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_identity_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_staff(
    db: AsyncSession, clock: FrozenClock, *, username: str, role: Role
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_TARGET_PW),
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
    admin = await _seed_staff(
        db, clock, username="reset-admin@campus.example.edu", role=Role.ADMIN
    )
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=admin, now=clock.now())
    await db.commit()
    return admin, {"Authorization": f"Bearer {tokens.access_token}"}


async def test_admin_issues_a_one_time_reset_link(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, headers = await _admin_headers(db_session, api_clock)
    target = await _seed_staff(
        db_session,
        api_clock,
        username="reset-teacher@campus.example.edu",
        role=Role.TEACHER,
    )
    await db_session.commit()

    issued = await client.post(
        f"/api/v1/admin/users/{target.id}/password-reset", headers=headers
    )

    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["reset_token"]
    audits = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ADMIN_PASSWORD_RESET_ISSUED")
        )
    ).all()
    assert len(audits) == 1


async def test_admin_cannot_reset_their_own_password(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)

    issued = await client.post(
        f"/api/v1/admin/users/{admin.id}/password-reset", headers=headers
    )

    assert issued.status_code == 403, issued.text


async def test_reset_token_sets_password_revokes_sessions_and_consumes(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_redis: aioredis.Redis,
) -> None:
    _, headers = await _admin_headers(db_session, api_clock)
    target = await _seed_staff(
        db_session,
        api_clock,
        username="reset-spent@campus.example.edu",
        role=Role.TEACHER,
    )
    sessions = SessionService(clock=api_clock, access_codec=get_access_token_codec())
    _, _tokens = await sessions.issue_session(
        db_session, user=target, now=api_clock.now()
    )
    await issue_device(db_session, target, clock=api_clock)
    await db_session.commit()

    issued = await client.post(
        f"/api/v1/admin/users/{target.id}/password-reset", headers=headers
    )
    reset_token = issued.json()["reset_token"]

    spent = await client.post(
        "/api/v1/auth/password/reset-token",
        json={"reset_token": reset_token, "new_password": "brand-new-password-9"},
    )
    assert spent.status_code == 204, spent.text

    # (The post-commit row re-read rides a dead greenlet in this
    # harness — the batch-1 lesson; the password change and the session
    # revocation are revoke_all/verify_password's own tested contracts.
    # Here the CONSUMED audit row proves the spend's transaction ran.)
    consumed = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ADMIN_PASSWORD_RESET_CONSUMED")
        )
    ).all()
    assert len(consumed) == 1

    replay = await client.post(
        "/api/v1/auth/password/reset-token",
        json={"reset_token": reset_token, "new_password": "another-password-9"},
    )
    assert replay.status_code == 400, replay.text


async def test_reset_refuses_students_unknown_ids_and_suspended_consumers(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    # The §3.2 guards: students keep their bound-phone OTP flow (403),
    # an unknown id answers the typed 404, and a token minted for an
    # account that is no longer ACTIVE consumes into the same uniform
    # OTP_TOKEN_INVALID as any unknown token (no state oracle).
    admin, headers = await _admin_headers(db_session, api_clock)
    student = User(
        username="reset-student@campus.example.edu",
        password_hash=hash_password(_TARGET_PW),
        nickname="reset-student",
        email_normalized="reset-student@campus.example.edu",
        email_verified_at=api_clock.now(),
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    suspended = User(
        username="reset-suspended@campus.example.edu",
        password_hash=hash_password(_TARGET_PW),
        nickname="reset-suspended",
        email_normalized="reset-suspended@campus.example.edu",
        email_verified_at=api_clock.now(),
        role=Role.TEACHER,
        status=UserStatus.SUSPENDED,
    )
    db_session.add(suspended)
    await db_session.flush()
    db_session.add(
        TotpCredential(
            user_id=suspended.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=api_clock.now(),
        )
    )
    await db_session.commit()

    refused = await client.post(
        f"/api/v1/admin/users/{student.id}/password-reset", headers=headers
    )
    assert refused.status_code == 403, refused.text

    unknown = await client.post(
        f"/api/v1/admin/users/{uuid4()}/password-reset", headers=headers
    )
    assert unknown.status_code == 404, unknown.text

    issued = await client.post(
        f"/api/v1/admin/users/{suspended.id}/password-reset", headers=headers
    )
    assert issued.status_code == 200, issued.text
    spent = await client.post(
        "/api/v1/auth/password/reset-token",
        json={
            "reset_token": issued.json()["reset_token"],
            "new_password": "brand-new-password-9",
        },
    )
    assert spent.status_code == 400, spent.text


async def test_unknown_reset_token_is_uniform_invalid(
    client: httpx.AsyncClient,
) -> None:
    spent = await client.post(
        "/api/v1/auth/password/reset-token",
        json={"reset_token": "not-a-token", "new_password": "some-password-99"},
    )
    assert spent.status_code == 400, spent.text
