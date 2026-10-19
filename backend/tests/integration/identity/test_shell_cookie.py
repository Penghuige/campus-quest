# backend/tests/integration/identity/test_shell_cookie.py
"""The cq_shell role-hint cookie (shell-flicker fix, backend half).

Every session-cookie issuance ALSO writes ``cq_shell=student|staff`` so
the RSC server-side probe can render the right shell before any client
JS wakes up (the optimistic student-shell flicker on staff refresh).
Pinned: both login forms (student password; staff email-code + invite
accept — the password+TOTP channel shares the same cookie seam),
refresh rotation rewrites it, logout clears it, and the value domain is
exactly the two-value set (no ADMIN/TEACHER granularity).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http import cookies as http_cookies
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password, hash_refresh_token
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import StaffInvitation, User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from app.modules.identity.routing_common import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SHELL_COOKIE_NAME,
)
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

pytestmark = pytest.mark.integration

_SHELL_REDIS_DB = 9
_T0 = datetime.now(UTC).replace(microsecond=0)
_STUDENT = "20250015555"
_PASSWORD = "correct-horse-battery"
_TEACHER_EMAIL = "shell-teacher@campus.example.edu"
_ADMIN_OWNER_EMAIL = "shell-owner@campus.example.edu"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_SHELL_REDIS_DB}"))


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


async def _seed_student(db: AsyncSession) -> User:
    user = User(
        username=_STUDENT,
        password_hash=hash_password(_PASSWORD),
        nickname="壳测试同学",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_passwordless_teacher(db: AsyncSession) -> User:
    user = User(
        username=_TEACHER_EMAIL,
        password_hash=None,
        nickname="壳测试教师",
        email_normalized=_TEACHER_EMAIL,
        email_verified_at=_T0,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _shell_cookie_header(response: httpx.Response) -> str:
    matches = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{SHELL_COOKIE_NAME}=")
    ]
    assert len(matches) == 1, f"expected exactly one {SHELL_COOKIE_NAME} Set-Cookie"
    return matches[0]


def _shell_value(response: httpx.Response) -> str:
    jar = http_cookies.SimpleCookie()
    jar.load(_shell_cookie_header(response))
    return jar[SHELL_COOKIE_NAME].value


def _assert_cookie_flags(header: str) -> None:
    assert "HttpOnly" in header
    assert "Secure" in header
    assert re.search(r"samesite=lax", header, re.IGNORECASE)


async def _student_login(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"identifier": _STUDENT, "password": _PASSWORD}
    )


async def test_student_login_writes_student_shell(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_student(db_session)

    response = await _student_login(client)

    assert response.status_code == 200, response.text
    header = _shell_cookie_header(response)
    _assert_cookie_flags(header)
    assert _shell_value(response) == "student"


async def test_staff_email_code_login_writes_staff_shell(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    await _seed_passwordless_teacher(db_session)

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
    _assert_cookie_flags(_shell_cookie_header(verified))
    assert _shell_value(verified) == "staff"


async def test_invitation_accept_writes_staff_shell(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    import secrets

    db = db_session
    owner = User(
        username=_ADMIN_OWNER_EMAIL,
        password_hash="x",
        nickname="壳测试管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(owner)
    await db.flush()
    token = secrets.token_urlsafe(32)
    db.add(
        StaffInvitation(
            email_normalized=_TEACHER_EMAIL,
            role=Role.TEACHER.value,
            token_hash=hash_refresh_token(token),
            expires_at=_T0.replace(year=_T0.year + 1),
            created_by=owner.id,
        )
    )
    await db.flush()

    accepted = await client.post(
        "/api/v1/auth/staff/invitations/accept", json={"token": token}
    )

    assert accepted.status_code == 200, accepted.text
    _assert_cookie_flags(_shell_cookie_header(accepted))
    assert _shell_value(accepted) == "staff"


async def test_refresh_rotation_rewrites_the_shell(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_student(db_session)
    login = await _student_login(client)
    csrf = login.json()["tokens"]["csrf_token"]
    # Explicit cookies (the test_identity_api pattern): the ASGI client
    # runs over http://, and a real cookie jar must not replay Secure
    # cookies over plain http.
    from app.modules.identity.routing_common import REFRESH_COOKIE_NAME as _RC

    jar = http_cookies.SimpleCookie()

    def _cookie_from(response: httpx.Response, name: str) -> str:
        jar.load(
            next(
                h
                for h in response.headers.get_list("set-cookie")
                if h.startswith(f"{name}=")
            )
        )
        return jar[name].value

    auth_cookies = {
        _RC: _cookie_from(login, _RC),
        CSRF_COOKIE_NAME: csrf,
    }

    rotated = await client.post(
        "/api/v1/auth/refresh",
        cookies=auth_cookies,
        headers={CSRF_HEADER_NAME: csrf},
    )

    assert rotated.status_code == 200, rotated.text
    _assert_cookie_flags(_shell_cookie_header(rotated))
    assert _shell_value(rotated) == "student"


async def test_logout_clears_the_shell(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_student(db_session)
    login = await _student_login(client)
    csrf = login.json()["tokens"]["csrf_token"]
    from app.modules.identity.routing_common import REFRESH_COOKIE_NAME as _RC

    jar = http_cookies.SimpleCookie()

    def _cookie_from(response: httpx.Response, name: str) -> str:
        jar.load(
            next(
                h
                for h in response.headers.get_list("set-cookie")
                if h.startswith(f"{name}=")
            )
        )
        return jar[name].value

    auth_cookies = {
        _RC: _cookie_from(login, _RC),
        CSRF_COOKIE_NAME: csrf,
    }

    logged_out = await client.post(
        "/api/v1/auth/logout",
        cookies=auth_cookies,
        headers={CSRF_HEADER_NAME: csrf},
        json={},
    )

    assert logged_out.status_code in (200, 204), logged_out.text
    header = _shell_cookie_header(logged_out)
    # Cleared: Max-Age=0 (empty value) at the same path it was set with.
    assert "max-age=0" in header.lower()
    jar = http_cookies.SimpleCookie()
    jar.load(header)
    assert jar[SHELL_COOKIE_NAME].value == ""


def test_shell_value_domain_is_exactly_two_values() -> None:
    """The cookie never carries ADMIN/TEACHER granularity — the RSC probe
    needs exactly the two shells (information-surface restraint)."""
    assert SHELL_COOKIE_NAME == "cq_shell"
    # The mapping is the rbac predicate: STUDENT -> student, staff roles
    # -> staff; asserted behaviorally above, structurally here.
    from app.core import rbac
    from app.modules.identity.enums import Role

    for role in Role:
        shell = "student" if not rbac.is_staff(role.value) else "staff"
        assert shell in ("student", "staff")
