# backend/tests/integration/identity/test_own_devices_api.py
"""Self-service remembered-device management (V1.1 §2.2, Task 11 backend).

GET/DELETE /me/devices: the owner's own device list (never token
hashes) and one-shot self-revocation. Ruling: the routes mount the
active-STAFF guard — the credential is only ever issued on the staff
email-code channel, so a student token answers PERMISSION_DENIED rather
than an always-empty list that would imply the capability exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.device_credentials import DEVICE_COOKIE_NAME, issue
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import RememberedDevice, User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

pytestmark = pytest.mark.integration

_DEVICES_REDIS_DB = 10
_T0 = datetime.now(UTC).replace(microsecond=0)
_TEACHER_EMAIL = "devices-self@campus.example.edu"
_OTHER_EMAIL = "devices-other@campus.example.edu"
_STUDENT = "20250016666"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_DEVICES_REDIS_DB}"))


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
def api_email() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_email: FakeEmailSender,
    fake_limiter: FakeRateLimiter,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_identity_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_email_sender] = lambda: api_email
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_staff(db: AsyncSession, email: str) -> User:
    user = User(
        username=email,
        password_hash=None,
        nickname=email.split("@")[0],
        email_normalized=email,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _mint_bearer(db: AsyncSession, user: User) -> str:
    from app.core.security import AccessTokenCodec
    from app.modules.identity.session_service import SessionService

    sessions = SessionService(
        clock=FrozenClock(_T0),
        access_codec=AccessTokenCodec(
            secret=get_settings().token_secret,
            ttl_minutes=get_settings().access_token_ttl_minutes,
        ),
    )
    _, tokens = await sessions.issue_session(db, user=user, now=_T0)
    await db.commit()
    return tokens.access_token


async def test_list_returns_only_caller_devices_without_hashes(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    mine = await _seed_staff(db_session, _TEACHER_EMAIL)
    other = await _seed_staff(db_session, _OTHER_EMAIL)
    await issue(db_session, mine.id, clock=FrozenClock(_T0))
    await issue(db_session, mine.id, clock=FrozenClock(_T0))
    await issue(db_session, other.id, clock=FrozenClock(_T0))
    await db_session.commit()
    bearer = await _mint_bearer(db_session, mine)

    response = await client.get(
        "/api/v1/me/devices", headers={"Authorization": f"Bearer {bearer}"}
    )

    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 2
    for item in items:
        # The DTO enumerates fields explicitly: a token hash is not
        # representable in the response.
        assert set(item) == {"id", "created_at", "last_used_at", "expires_at"}
    assert {item["id"] for item in items} == {
        str(row.id)
        for row in await db_session.scalars(
            select(RememberedDevice).where(RememberedDevice.user_id == mine.id)
        )
    }


async def test_delete_revokes_every_device_and_kills_the_cookie(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    """The end-to-end Review Focus 3 shape: after DELETE, a login with
    the (stolen) cookie falls back to the TOTP leg."""
    admin = await _seed_staff(db_session, _TEACHER_EMAIL)
    bearer = await _mint_bearer(db_session, admin)
    cookie_value = await issue(db_session, admin.id, clock=FrozenClock(_T0))
    await db_session.commit()

    deleted = await client.request(
        "DELETE",
        "/api/v1/me/devices",
        headers={"Authorization": f"Bearer {bearer}"},
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["revoked"] == 1
    assert (
        await db_session.scalars(
            select(RememberedDevice).where(RememberedDevice.user_id == admin.id)
        )
    ).all() == []

    # The audit row: actor = the account that revoked its own devices.
    actions = list(
        await db_session.scalars(
            select(AuditLog.action).where(AuditLog.actor_user_id == admin.id)
        )
    )
    assert "OWN_DEVICES_REVOKED" in actions

    # The stolen-cookie fallback, end to end.
    client.cookies.set(DEVICE_COOKIE_NAME, cookie_value)
    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": _TEACHER_EMAIL}
    )
    assert requested.status_code == 202, requested.text
    from tests.e2e.email_code_probe import recover_email_code

    _, code = await recover_email_code(db_session, _TEACHER_EMAIL)
    verified = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": _TEACHER_EMAIL, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "totp_required"


async def test_student_token_is_denied(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student = User(
        username=_STUDENT,
        password_hash="x",
        nickname="学生",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.commit()
    bearer = await _mint_bearer(db_session, student)

    listed = await client.get(
        "/api/v1/me/devices", headers={"Authorization": f"Bearer {bearer}"}
    )
    deleted = await client.request(
        "DELETE",
        "/api/v1/me/devices",
        headers={"Authorization": f"Bearer {bearer}"},
    )

    assert listed.status_code == 403, listed.text
    assert listed.json()["error"]["code"] == "PERMISSION_DENIED"
    assert deleted.status_code == 403, deleted.text


async def test_unauthenticated_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/me/devices")
    assert response.status_code == 401, response.text


async def test_delete_with_no_devices_reports_zero(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    staff = await _seed_staff(db_session, _TEACHER_EMAIL)
    bearer = await _mint_bearer(db_session, staff)

    deleted = await client.request(
        "DELETE",
        "/api/v1/me/devices",
        headers={"Authorization": f"Bearer {bearer}"},
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["revoked"] == 0
