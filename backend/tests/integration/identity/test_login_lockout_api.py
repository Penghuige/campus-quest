# backend/tests/integration/identity/test_login_lockout_api.py
"""Per-account lockout across ALL login paths (V1.1 §6, Wave 1 Task 6).

The uniform counter (LoginFailureGuard) is wired into the student
password path, the staff password+TOTP path, and (Task 5) the email-code
path. Pinned behaviors (Review Focus 4):

- 5 consecutive credential failures -> ACCOUNT_LOCKED envelope with
  retry_after_seconds, even for the CORRECT credential on attempt 6;
- a success between failures resets the streak;
- a rate-limit 429 (traffic shaping) never counts as a failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pyotp
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
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

_LOCKOUT_REDIS_DB = 12
_T0 = datetime.now(UTC).replace(microsecond=0)
_STUDENT = "20250017777"
_PASSWORD = "correct-horse-battery"
_STAFF_EMAIL = "lockout-staff@campus.example.edu"
_STAFF_PASSWORD = "staff-correct-horse"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_LOCKOUT_REDIS_DB}"))


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


def _envelope(response: httpx.Response) -> dict:
    return response.json()["error"]


async def _seed_student(db: AsyncSession) -> User:
    user = User(
        username=_STUDENT,
        password_hash=hash_password(_PASSWORD),
        nickname="锁定测试同学",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_staff_with_totp(db: AsyncSession) -> str:
    """ACTIVE TEACHER with password + confirmed TOTP (the V1 staff
    channel); returns the TOTP secret for pyotp."""
    user = User(
        username=_STAFF_EMAIL,
        password_hash=hash_password(_STAFF_PASSWORD),
        nickname="锁定测试员工",
        email_normalized=_STAFF_EMAIL,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    secret = pyotp.random_base32()
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=Fernet(
                get_settings().totp_encryption_key.encode()
            ).encrypt(secret.encode()),
            confirmed_at=_T0,
        )
    )
    await db.flush()
    return secret


async def _student_login(client: httpx.AsyncClient, password: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"username": _STUDENT, "password": password}
    )


async def _staff_login(
    client: httpx.AsyncClient, totp_code: str, *, password: str = _STAFF_PASSWORD
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/staff/login",
        json={"email": _STAFF_EMAIL, "password": password, "totp_code": totp_code},
    )


async def test_student_five_wrong_passwords_then_correct_is_locked(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_student(db_session)

    for _ in range(5):
        wrong = await _student_login(client, "wrong-password")
        assert wrong.status_code == 401, wrong.text
        assert _envelope(wrong)["code"] == "AUTHENTICATION_REQUIRED"

    correct = await _student_login(client, _PASSWORD)
    assert correct.status_code == 429, correct.text
    error = _envelope(correct)
    assert error["code"] == "ACCOUNT_LOCKED"
    assert 0 < error["details"]["retry_after_seconds"] <= 900


async def test_staff_wrong_totp_counts_and_locks(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    secret = await _seed_staff_with_totp(db_session)
    valid_code = pyotp.TOTP(secret).at(api_clock.now())

    for _ in range(5):
        # Right password, wrong second factor — a credential failure.
        wrong = await _staff_login(client, "000000")
        assert wrong.status_code == 401, wrong.text

    correct = await _staff_login(client, valid_code)
    assert correct.status_code == 429, correct.text
    assert _envelope(correct)["code"] == "ACCOUNT_LOCKED"


async def test_staff_wrong_password_counts_before_second_factor(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    secret = await _seed_staff_with_totp(db_session)
    valid_code = pyotp.TOTP(secret).at(api_clock.now())

    for _ in range(4):
        wrong = await _staff_login(client, valid_code, password="wrong-password")
        assert wrong.status_code == 401, wrong.text

    # A success between failures resets the streak (V1.1 §6).
    ok = await _staff_login(client, valid_code)
    assert ok.status_code == 200, ok.text

    for _ in range(4):
        wrong = await _staff_login(client, "000000")
        assert wrong.status_code == 401, wrong.text

    # 4 new failures after the reset: still below the threshold.
    fifth = await _staff_login(client, valid_code)
    assert fifth.status_code == 200, fifth.text


async def test_rate_limit_429_never_counts_as_failure(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
) -> None:
    """Review Focus 4: the email-code request path's 429s (cooldown) are
    traffic shaping — the account failure counter stays untouched, and a
    later correct login succeeds without ever being locked."""
    from tests.integration.identity.test_email_code_login_api import (
        _TEACHER_EMAIL,  # noqa: PLC0415 — cross-file seed helper reuse
        _seed_teacher,
    )

    teacher = await _seed_teacher(db_session)
    # Two requests inside the cooldown window: the second is a 429 that
    # must NOT reach the per-account counter.
    assert (
        await client.post(
            "/api/v1/auth/staff/email-code/request", json={"email": _TEACHER_EMAIL}
        )
    ).status_code == 202
    throttled = await client.post(
        "/api/v1/auth/staff/email-code/request", json={"email": _TEACHER_EMAIL}
    )
    assert throttled.status_code == 429
    assert await api_redis.get(f"login:fail:{teacher.id}") is None

    # The mailed code still verifies cleanly — nothing was counted.
    code = str(api_email.messages[-1].variables["code"])
    verified = await client.post(
        "/api/v1/auth/staff/email-code/verify",
        json={"email": _TEACHER_EMAIL, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["access_token"]
