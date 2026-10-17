# backend/tests/integration/identity/test_mobile_remembered_probe_api.py
"""The remembered-device probe + cookie-only mobile verify leg
(V1.1 login iteration, item 3).

The login page banner needs to know whether a remembered device is
alive, but cq_staff_device is HttpOnly — the browser cannot look. The
probe answers ONLY for the banner's audience: a cookie bound to a
STUDENT (ACTIVE or PENDING_APPROVAL) with a bound phone yields 200
{phone_masked}; EVERY other shape (no cookie, unknown/expired value,
a staff binding) answers the same empty 204 — existence is never an
answer. The click leg is cookie-only: verify with an EMPTY body — no
phone, no code — the device row resolves the user directly.
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.device_credentials import (
    DEVICE_COOKIE_NAME,
)
from app.modules.identity.device_credentials import (
    issue as issue_device,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.providers import (
    get_identity_redis,
    get_rate_limiter,
)
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

_PROBE_REDIS_DB = 11
_T0 = datetime.now(UTC).replace(microsecond=0)
_PHONE = "+8613800123456"
_MASKED = "+86****3456"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_PROBE_REDIS_DB}"))


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


async def _seed_user(
    db: AsyncSession,
    *,
    username: str,
    role: Role,
    status: UserStatus,
    phone: str | None,
) -> User:
    user = User(
        username=username,
        password_hash=None,
        nickname=username,
        phone_e164=phone,
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


async def test_probe_answers_masked_phone_for_active_student(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    student = await _seed_user(
        db_session,
        username="20250018888",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
        phone=_PHONE,
    )
    cookie = await issue_device(db_session, student.id, clock=api_clock)
    await db_session.commit()

    probed = await client.get(
        "/api/v1/auth/mobile-code/remembered",
        cookies={DEVICE_COOKIE_NAME: cookie},
    )

    assert probed.status_code == 200, probed.text
    assert probed.json() == {"phone_masked": _MASKED}


async def test_probe_answers_pending_student_too(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    student = await _seed_user(
        db_session,
        username="20250018889",
        role=Role.STUDENT,
        status=UserStatus.PENDING_APPROVAL,
        phone=_PHONE,
    )
    cookie = await issue_device(db_session, student.id, clock=api_clock)
    await db_session.commit()

    probed = await client.get(
        "/api/v1/auth/mobile-code/remembered",
        cookies={DEVICE_COOKIE_NAME: cookie},
    )

    assert probed.status_code == 200, probed.text


async def test_probe_is_empty_204_for_every_non_banner_shape(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    staff = await _seed_user(
        db_session,
        username="probe-teacher@campus.example.edu",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
        phone=_PHONE,
    )
    staff_cookie = await issue_device(db_session, staff.id, clock=api_clock)
    await db_session.commit()

    no_cookie = await client.get("/api/v1/auth/mobile-code/remembered")
    bad_value = await client.get(
        "/api/v1/auth/mobile-code/remembered",
        cookies={DEVICE_COOKIE_NAME: "not-a-real-cookie"},
    )
    staff_bound = await client.get(
        "/api/v1/auth/mobile-code/remembered",
        cookies={DEVICE_COOKIE_NAME: staff_cookie},
    )

    for response in (no_cookie, bad_value, staff_bound):
        assert response.status_code == 204, response.text
        assert response.content == b""


async def test_cookie_only_verify_opens_session_and_rotates_cookie(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    student = await _seed_user(
        db_session,
        username="20250018890",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
        phone=_PHONE,
    )
    cookie = await issue_device(db_session, student.id, clock=api_clock)
    await db_session.commit()

    verified = await client.post(
        "/api/v1/auth/mobile-code/verify",
        json={},
        cookies={DEVICE_COOKIE_NAME: cookie},
    )

    assert verified.status_code == 200, verified.text
    assert verified.json()["access_token"]
    # The #87 discipline: every successful cookie login re-issues a
    # fresh 30-day value (rotation), so a lifted old value dies here.
    set_values = [
        header.split("=", 1)[1].split(";", 1)[0]
        for header in verified.headers.get_list("set-cookie")
        if header.startswith(f"{DEVICE_COOKIE_NAME}=")
    ]
    assert set_values and set_values[0] != cookie


async def test_cookie_only_verify_without_cookie_is_uniform_400(
    client: httpx.AsyncClient,
) -> None:
    verified = await client.post("/api/v1/auth/mobile-code/verify", json={})

    assert verified.status_code == 400, verified.text
    assert verified.json()["error"]["code"] == "MOBILE_CODE_INVALID"
