# backend/tests/integration/identity/test_staff_login_remembered_api.py
"""Staff password login grows the remembered-device second factor
(V1.1 login iteration, item 2).

The checkbox on the staff panel: ``remember_device`` on a full login
mints the 30-day cq_staff_device cookie (#87 craft). Within its life a
re-login needs only email+password — an EMPTY ``totp_code`` plus the
cookie completes the login (the cookie replaces the second factor,
same as the email-code ADMIN leg). Without a valid cookie an empty
code is the uniform AUTHENTICATION_REQUIRED, byte-identical to a wrong
password — no existence or state signal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
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
from app.modules.identity.totp import generate_totp_secret
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

_STAFF_REDIS_DB = 10
_T0 = datetime.now(UTC).replace(microsecond=0)
_ADMIN_EMAIL = "remember-admin@campus.example.edu"
_ADMIN_PASSWORD = "correct-horse-battery"
_FERNET_KEY_MISSING = object()
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_STAFF_REDIS_DB}"))


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


def _totp_now_code(secret: str, clock: Clock) -> str:
    import pyotp

    return pyotp.TOTP(secret).at(clock.now())


async def _seed_confirmed_admin(db: AsyncSession, clock: Clock) -> tuple[User, str]:
    """One ACTIVE ADMIN with a CONFIRMED TOTP credential; returns the
    user and the plaintext secret (the totp.confirm flow's stand-in)."""
    from cryptography.fernet import Fernet

    from app.modules.identity.models import TotpCredential
    from app.modules.identity.totp import encrypt_totp_secret

    user = User(
        username=_ADMIN_EMAIL,
        password_hash=hash_password(_ADMIN_PASSWORD),
        nickname="记住设备管理员",
        email_normalized=_ADMIN_EMAIL,
        email_verified_at=clock.now(),
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    secret = generate_totp_secret()
    fernet = Fernet(get_settings().totp_encryption_key.encode())
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=encrypt_totp_secret(fernet, secret),
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return user, secret


def _login_body(*, code: str, remember: bool = False) -> dict[str, object]:
    return {
        "email": _ADMIN_EMAIL,
        "password": _ADMIN_PASSWORD,
        "totp_code": code,
        "remember_device": remember,
    }


async def test_full_login_with_remember_mints_device_cookie(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, secret = await _seed_confirmed_admin(db_session, api_clock)

    logged = await client.post(
        "/api/v1/auth/staff/login",
        json=_login_body(code=_totp_now_code(secret, api_clock), remember=True),
    )

    assert logged.status_code == 200, logged.text
    cookie_header = next(
        (
            header
            for header in logged.headers.get_list("set-cookie")
            if header.startswith(f"{DEVICE_COOKIE_NAME}=")
        ),
        None,
    )
    assert cookie_header is not None, logged.headers.get_list("set-cookie")


async def test_empty_code_with_valid_cookie_completes_login(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    user, secret = await _seed_confirmed_admin(db_session, api_clock)
    # A remembered device from a PREVIOUS full login.
    cookie_value = await issue_device(db_session, user.id, clock=api_clock)
    await db_session.commit()

    logged = await client.post(
        "/api/v1/auth/staff/login",
        json=_login_body(code=""),
        cookies={DEVICE_COOKIE_NAME: cookie_value},
    )

    assert logged.status_code == 200, logged.text
    assert logged.json()["access_token"]


async def test_empty_code_without_cookie_is_uniform_authentication_failure(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    await _seed_confirmed_admin(db_session, api_clock)

    empty_code = await client.post(
        "/api/v1/auth/staff/login", json=_login_body(code="")
    )
    wrong_password = await client.post(
        "/api/v1/auth/staff/login",
        json={
            "email": _ADMIN_EMAIL,
            "password": "not-the-password",
            "totp_code": "123456",
        },
    )

    assert empty_code.status_code == 401, empty_code.text

    # Identical error envelope (request_id is per-request by design):
    # empty code + no cookie tells an attacker nothing a wrong password
    # wouldn't.
    def _envelope(response: httpx.Response) -> dict[str, object]:
        error = dict(response.json()["error"])
        error.pop("request_id")
        return error

    assert _envelope(empty_code) == _envelope(wrong_password)


async def test_empty_code_with_expired_cookie_is_uniform_failure(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user, _ = await _seed_confirmed_admin(db_session, FrozenClock(_T0))
    stale_clock = FrozenClock(_T0 - timedelta(days=31))
    cookie_value = await issue_device(db_session, user.id, clock=stale_clock)
    await db_session.commit()

    logged = await client.post(
        "/api/v1/auth/staff/login",
        json=_login_body(code=""),
        cookies={DEVICE_COOKIE_NAME: cookie_value},
    )

    assert logged.status_code == 401, logged.text


async def test_nonempty_code_without_cookie_still_logs_in(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    """The pre-iteration path is untouched: a current TOTP code needs
    no cookie and no checkbox."""
    _, secret = await _seed_confirmed_admin(db_session, api_clock)

    logged = await client.post(
        "/api/v1/auth/staff/login",
        json=_login_body(code=_totp_now_code(secret, api_clock)),
    )

    assert logged.status_code == 200, logged.text
