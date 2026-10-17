# backend/tests/integration/identity/test_staff_mobile_code_login_api.py
"""Staff mobile-code login (V1.1 login iteration, item 1b — the approved
/auth/staff/mobile-code contract).

The staff-panel third channel, mirroring the /auth/staff/email-code
family shape exactly: constant 202 on request (anti-enumeration — a
phone bound to a STUDENT is never sent a code, the SMS stays silent),
verify by role (TEACHER completes in one step; ADMIN gets the opaque
pending token + TOTP second leg, or the remembered-device cookie
replaces the second factor outright — the #87 craft shared with the
password re-login leg). The login_mobile_challenges table and the
keyed-digest craft are shared with the student channel.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
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
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import (
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from app.modules.identity.totp import encrypt_totp_secret, generate_totp_secret
from tests.fakes.integrations import FakeRateLimiter, FakeSmsSender

pytestmark = pytest.mark.integration

_SMC_REDIS_DB = 13
_T0 = datetime.now(UTC).replace(microsecond=0)
_TEACHER_EMAIL = "smc-teacher@campus.example.edu"
_TEACHER_PASSWORD = "correct-horse-battery"
_ADMIN_EMAIL = "smc-admin@campus.example.edu"
_ADMIN_PASSWORD = "correct-horse-battery"
_PHONE_TEACHER = "+8613800155551"
_PHONE_STUDENT = "+8613800155552"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_SMC_REDIS_DB}"))


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
def api_sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
    fake_limiter: FakeRateLimiter,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_identity_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_sms_sender] = lambda: api_sms
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_staff(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    role: Role,
    phone: str,
    with_totp: bool,
) -> tuple[User, str | None]:
    user = User(
        username=email,
        password_hash=hash_password(password),
        nickname=email.split("@")[0],
        email_normalized=email,
        email_verified_at=_T0,
        phone_e164=phone,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    secret: str | None = None
    if with_totp:
        secret = generate_totp_secret()
        fernet = Fernet(get_settings().totp_encryption_key.encode())
        db.add(
            TotpCredential(
                user_id=user.id,
                secret_encrypted=encrypt_totp_secret(fernet, secret),
                confirmed_at=_T0,
            )
        )
        await db.flush()
    return user, secret


async def _seed_student_with_phone(db: AsyncSession) -> User:
    user = User(
        username="20250015555",
        password_hash=None,
        nickname="占号学生",
        phone_e164=_PHONE_STUDENT,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _totp_now(secret: str) -> str:
    import pyotp

    return pyotp.TOTP(secret).at(_T0)


async def test_teacher_completes_in_one_step(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    await _seed_staff(
        db_session,
        email=_TEACHER_EMAIL,
        password=_TEACHER_PASSWORD,
        role=Role.TEACHER,
        phone=_PHONE_TEACHER,
        with_totp=False,
    )
    await db_session.commit()

    requested = await client.post(
        "/api/v1/auth/staff/mobile-code/request", json={"phone": _PHONE_TEACHER}
    )
    assert requested.status_code == 202, requested.text
    assert requested.json() == {}  # the family's constant-202 empty shape
    assert len(api_sms.messages) == 1
    code = api_sms.messages[0].variables["code"]

    verified = await client.post(
        "/api/v1/auth/staff/mobile-code/verify",
        json={"phone": _PHONE_TEACHER, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert set(verified.json()) == {"access_token", "csrf_token", "token_type"}


async def test_student_bound_phone_is_202_but_silent(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    await _seed_student_with_phone(db_session)
    await db_session.commit()

    requested = await client.post(
        "/api/v1/auth/staff/mobile-code/request", json={"phone": _PHONE_STUDENT}
    )
    unknown = await client.post(
        "/api/v1/auth/staff/mobile-code/request", json={"phone": "+8613800000000"}
    )

    assert requested.status_code == 202, requested.text
    assert unknown.status_code == 202, unknown.text
    assert len(api_sms.messages) == 0


async def test_admin_gets_pending_totp_leg_and_completes(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    _, secret = await _seed_staff(
        db_session,
        email=_ADMIN_EMAIL,
        password=_ADMIN_PASSWORD,
        role=Role.ADMIN,
        phone=_PHONE_TEACHER,
        with_totp=True,
    )
    await db_session.commit()
    await client.post(
        "/api/v1/auth/staff/mobile-code/request", json={"phone": _PHONE_TEACHER}
    )
    code = api_sms.messages[0].variables["code"]

    verified = await client.post(
        "/api/v1/auth/staff/mobile-code/verify",
        json={"phone": _PHONE_TEACHER, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert set(verified.json()) == {"status", "pending_token"}
    assert verified.json()["status"] == "totp_required"
    pending_token = verified.json()["pending_token"]

    finished = await client.post(
        "/api/v1/auth/staff/mobile-code/totp",
        json={"pending_token": pending_token, "code": _totp_now(secret or "")},
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["access_token"]


async def test_admin_with_remembered_device_skips_totp_leg(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, _ = await _seed_staff(
        db_session,
        email=_ADMIN_EMAIL,
        password=_ADMIN_PASSWORD,
        role=Role.ADMIN,
        phone=_PHONE_TEACHER,
        with_totp=True,
    )
    cookie = await issue_device(db_session, admin.id, clock=api_clock)
    await db_session.commit()

    verified = await client.post(
        "/api/v1/auth/staff/mobile-code/verify",
        json={},
        cookies={DEVICE_COOKIE_NAME: cookie},
    )

    assert verified.status_code == 200, verified.text
    assert verified.json()["access_token"]


async def test_wrong_code_is_the_uniform_invalid(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    await _seed_staff(
        db_session,
        email=_TEACHER_EMAIL,
        password=_TEACHER_PASSWORD,
        role=Role.TEACHER,
        phone=_PHONE_TEACHER,
        with_totp=False,
    )
    await db_session.commit()
    await client.post(
        "/api/v1/auth/staff/mobile-code/request", json={"phone": _PHONE_TEACHER}
    )

    verified = await client.post(
        "/api/v1/auth/staff/mobile-code/verify",
        json={"phone": _PHONE_TEACHER, "code": "000000"},
    )

    assert verified.status_code == 400, verified.text
    assert verified.json()["error"]["code"] == "MOBILE_CODE_INVALID"
