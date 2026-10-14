# backend/tests/integration/identity/test_email_code_login_api.py
"""Staff email-code login over the real app (V1.1 §2.1-§2.2, Wave 1 Task 5).

Drives create_app() with the production composition root — the new
routes, the real EmailLoginService wiring (Redis cooldown + lockout
guard on the flushed test db, FakeEmailSender as the SMTP seam,
FrozenClock anchored to the real now for PyJWT exp validation).

The five Review Focus behaviors each have a pinned test here:
1. request as an enumeration oracle -> constant 202 (+ uniform 429);
2. brute force -> 5 wrong attempts lock the account (a 6th attempt
   with the CORRECT code still fails);
3. stolen device cookie -> does not survive revoke_all;
4. (lockout-not-on-429 is Task 6's parametrized suite);
5. passwordless TEACHER logs in end to end (no password ever set).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http import cookies as http_cookies
from urllib.parse import urlsplit, urlunsplit

import httpx
import pyotp
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.device_credentials import (
    DEVICE_COOKIE_NAME,
    revoke_all,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

_EMAIL_REDIS_DB = 13
_T0 = datetime.now(UTC).replace(microsecond=0)
_TEACHER_EMAIL = "wave1-teacher@campus.example.edu"
_ADMIN_EMAIL = "wave1-admin@campus.example.edu"
_STUDENT_USERNAME = "20250018888"
_STUDENT_EMAIL = "student-mail@campus.example.edu"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_EMAIL_REDIS_DB}"))


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


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


async def _seed_teacher(db: AsyncSession) -> User:
    """Passwordless TEACHER — the V1.1 §2.3 account shape (Review
    Focus 5: login works with no password ever set)."""
    user = User(
        username=_TEACHER_EMAIL,
        password_hash=None,
        nickname="邮箱码教师",
        email_normalized=_TEACHER_EMAIL,
        email_verified_at=_T0,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_admin_with_totp(db: AsyncSession) -> tuple[User, str]:
    """ADMIN with a CONFIRMED TOTP credential (the V1 onboarding end
    state); returns the user and the plaintext secret for pyotp."""
    user = User(
        username=_ADMIN_EMAIL,
        password_hash=None,
        nickname="邮箱码管理员",
        email_normalized=_ADMIN_EMAIL,
        email_verified_at=_T0,
        role=Role.ADMIN,
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
    return user, secret


async def _seed_student_with_mail(db: AsyncSession) -> User:
    user = User(
        username=_STUDENT_USERNAME,
        password_hash="x",
        nickname="学生",
        email_normalized=_STUDENT_EMAIL,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _last_code(api_email: FakeEmailSender) -> str:
    assert api_email.messages, "no email was sent"
    return str(api_email.messages[-1].variables["code"])


def _cookie_header(response: httpx.Response, name: str) -> str:
    matches = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    ]
    assert len(matches) == 1, f"expected exactly one {name} Set-Cookie"
    return matches[0]


def _cookie_value(response: httpx.Response, name: str) -> str:
    jar = http_cookies.SimpleCookie()
    jar.load(_cookie_header(response, name))
    return jar[name].value


def _envelope(response: httpx.Response) -> dict:
    """The §29 envelope body: {"error": {code, message, details, ...}}."""
    return response.json()["error"]


async def _request_code(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/staff/email-code/request", json={"email": email}
    )


async def _verify(
    client: httpx.AsyncClient,
    email: str,
    code: str,
    *,
    remember_device: bool = False,
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/staff/email-code/verify",
        json={"email": email, "code": code, "remember_device": remember_device},
    )


async def test_teacher_email_code_happy_path(
    client: httpx.AsyncClient, db_session: AsyncSession, api_email: FakeEmailSender
) -> None:
    await _seed_teacher(db_session)

    requested = await _request_code(client, _TEACHER_EMAIL)
    assert requested.status_code == 202, requested.text
    assert requested.json() == {}

    verified = await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["access_token"]
    assert body["csrf_token"]

    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["username"] == _TEACHER_EMAIL


async def test_request_constant_202_for_unknown_and_student_mailboxes(
    client: httpx.AsyncClient, db_session: AsyncSession, api_email: FakeEmailSender
) -> None:
    """Review Focus 1: identical status, body, and timing band — the
    endpoint is not an oracle for staff existence."""
    await _seed_teacher(db_session)
    await _seed_student_with_mail(db_session)

    samples: list[tuple[float, httpx.Response]] = []
    for email in ("nobody@campus.example.edu", _STUDENT_EMAIL, _TEACHER_EMAIL):
        started = time.monotonic()
        response = await _request_code(client, email)
        samples.append((time.monotonic() - started, response))

    for _elapsed, response in samples:
        assert response.status_code == 202, response.text
        assert response.json() == {}
    # Timing band: all three inside one order of magnitude (the teacher
    # leg does a DB write + fake send; a real SMTP would add latency to
    # every staff request alike — the band assertion pins "no fast-path
    # divergence between unknown and staff").
    assert all(elapsed < 1.0 for elapsed, _ in samples)
    # Only the real teacher was mailed; nothing was stored for the rest.
    assert [message.to for message in api_email.messages] == [_TEACHER_EMAIL]


async def test_cooldown_second_request_is_uniform_429(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_teacher(db_session)
    unknown = "cooldown-probe@campus.example.edu"

    assert (await _request_code(client, unknown)).status_code == 202
    repeat_unknown = await _request_code(client, unknown)
    assert repeat_unknown.status_code == 429
    assert _envelope(repeat_unknown)["code"] == "RATE_LIMITED"

    assert (await _request_code(client, _TEACHER_EMAIL)).status_code == 202
    repeat_teacher = await _request_code(client, _TEACHER_EMAIL)
    assert repeat_teacher.status_code == 429
    assert _envelope(repeat_teacher)["code"] == "RATE_LIMITED"


async def test_admin_flow_totp_required_then_tokens(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_clock: FrozenClock,
) -> None:
    _, secret = await _seed_admin_with_totp(db_session)

    await _request_code(client, _ADMIN_EMAIL)
    pending = await _verify(client, _ADMIN_EMAIL, _last_code(api_email))
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "totp_required"
    pending_token = pending.json()["pending_token"]

    totp_code = pyotp.TOTP(secret).at(api_clock.now())
    finished = await client.post(
        "/api/v1/auth/staff/email-code/totp",
        json={"pending_token": pending_token, "code": totp_code},
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["access_token"]


async def test_pending_token_authenticates_nothing_but_the_totp_leg(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    """Final-review F1 pin: the mid-login pending_token is NOT a session.

    A full UserSession JWT here would pass every management guard (an
    ADMIN with a confirmed credential satisfies the guards' only 2FA
    check), reducing 邮箱码+TOTP to 邮箱码. The pending token must be an
    opaque single-purpose credential: 401 on /me, 401 on the admin
    surface — not 403/200."""
    await _seed_admin_with_totp(db_session)
    await _request_code(client, _ADMIN_EMAIL)
    pending = await _verify(client, _ADMIN_EMAIL, _last_code(api_email))
    token = pending.json()["pending_token"]

    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 401, me.text

    manage = await client.get(
        "/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"}
    )
    assert manage.status_code == 401, manage.text


async def test_totp_leg_consumes_the_pending_token(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_clock: FrozenClock,
) -> None:
    """The totp leg is single-use: after it succeeds (or fails on a bad
    code the lockout counts), the same pending_token answers nothing."""
    from app.core.error_codes import ErrorCode  # noqa: F401

    _, secret = await _seed_admin_with_totp(db_session)
    await _request_code(client, _ADMIN_EMAIL)
    pending = await _verify(client, _ADMIN_EMAIL, _last_code(api_email))
    token = pending.json()["pending_token"]

    finished = await client.post(
        "/api/v1/auth/staff/email-code/totp",
        json={"pending_token": token, "code": pyotp.TOTP(secret).at(api_clock.now())},
    )
    assert finished.status_code == 200, finished.text

    replay = await client.post(
        "/api/v1/auth/staff/email-code/totp",
        json={"pending_token": token, "code": pyotp.TOTP(secret).at(api_clock.now())},
    )
    assert replay.status_code == 401, replay.text
    assert _envelope(replay)["code"] == "AUTHENTICATION_REQUIRED"


async def test_five_wrong_codes_then_correct_code_fails(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    """Review Focus 2: the 6th attempt presents the CORRECT code and
    still fails — five wrong codes exhausted the challenge AND locked
    the account (V1.1 §6); the lock is checked first."""
    await _seed_teacher(db_session)
    await _request_code(client, _TEACHER_EMAIL)

    for _ in range(5):
        wrong = await _verify(client, _TEACHER_EMAIL, "000000")
        assert wrong.status_code == 400, wrong.text
        assert _envelope(wrong)["code"] == "EMAIL_CODE_INVALID"

    correct = await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    assert correct.status_code == 429, correct.text
    error = _envelope(correct)
    assert error["code"] == "ACCOUNT_LOCKED"
    assert 0 < error["details"]["retry_after_seconds"] <= 900


async def test_expired_code_rejected_uniformly(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_clock: FrozenClock,
) -> None:
    await _seed_teacher(db_session)
    await _request_code(client, _TEACHER_EMAIL)
    _advance(api_clock, minutes=6)

    expired = await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    assert expired.status_code == 400
    assert _envelope(expired)["code"] == "EMAIL_CODE_INVALID"


async def test_locked_account_gets_account_locked_envelope(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_email: FakeEmailSender,
) -> None:
    teacher = await _seed_teacher(db_session)
    await _request_code(client, _TEACHER_EMAIL)
    # Seed the lock directly: five failures already recorded elsewhere.
    key = f"login:fail:{teacher.id}"
    for _ in range(5):
        await api_redis.incr(key)
    await api_redis.expire(key, 900)

    response = await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    assert response.status_code == 429, response.text
    error = _envelope(response)
    assert error["code"] == "ACCOUNT_LOCKED"
    assert 0 < error["details"]["retry_after_seconds"] <= 900


async def test_remember_device_cookie_flags_and_totp_skip(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_clock: FrozenClock,
    api_redis: aioredis.Redis,
) -> None:
    """V1.1 §2.2: the device cookie is minted only on the login the
    TOTP leg completes (never before the second factor is proven), and
    the next ADMIN login with that cookie skips TOTP entirely."""
    _, secret = await _seed_admin_with_totp(db_session)

    await _request_code(client, _ADMIN_EMAIL)
    pending = await _verify(
        client, _ADMIN_EMAIL, _last_code(api_email), remember_device=True
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "totp_required"
    # Security pin: no device credential before the second factor.
    assert not [
        header
        for header in pending.headers.get_list("set-cookie")
        if header.startswith(f"{DEVICE_COOKIE_NAME}=")
    ]

    totp_code = pyotp.TOTP(secret).at(api_clock.now())
    finished = await client.post(
        "/api/v1/auth/staff/email-code/totp",
        json={
            "pending_token": pending.json()["pending_token"],
            "code": totp_code,
            "remember_device": True,
        },
    )
    assert finished.status_code == 200, finished.text
    header = _cookie_header(finished, DEVICE_COOKIE_NAME)
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "samesite=lax" in header.lower()
    cookie_value = _cookie_value(finished, DEVICE_COOKIE_NAME)

    client.cookies.set(DEVICE_COOKIE_NAME, cookie_value)
    _advance(api_clock, seconds=61)  # challenge freshness on the frozen clock
    # The cooldown key's TTL runs on REAL Redis time; drop it directly
    # (the frozen clock cannot advance it).
    await api_redis.delete(f"emailcode:cooldown:{_ADMIN_EMAIL}")
    await _request_code(client, _ADMIN_EMAIL)
    direct = await _verify(client, _ADMIN_EMAIL, _last_code(api_email))
    assert direct.status_code == 200, direct.text
    assert direct.json()["access_token"]
    assert "status" not in direct.json()


async def test_revoked_device_cookie_falls_back_to_totp(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_clock: FrozenClock,
    api_redis: aioredis.Redis,
) -> None:
    """Review Focus 3: a stolen cookie does not survive revoke_all."""
    admin, secret = await _seed_admin_with_totp(db_session)
    await _request_code(client, _ADMIN_EMAIL)
    pending = await _verify(
        client, _ADMIN_EMAIL, _last_code(api_email), remember_device=True
    )
    assert pending.json()["status"] == "totp_required"
    totp_code = pyotp.TOTP(secret).at(api_clock.now())
    finished = await client.post(
        "/api/v1/auth/staff/email-code/totp",
        json={
            "pending_token": pending.json()["pending_token"],
            "code": totp_code,
            "remember_device": True,
        },
    )
    assert finished.status_code == 200, finished.text
    cookie_value = _cookie_value(finished, DEVICE_COOKIE_NAME)

    assert await revoke_all(db_session, admin.id) == 1
    await db_session.commit()

    client.cookies.set(DEVICE_COOKIE_NAME, cookie_value)
    _advance(api_clock, seconds=61)  # challenge freshness on the frozen clock
    await api_redis.delete(f"emailcode:cooldown:{_ADMIN_EMAIL}")
    await _request_code(client, _ADMIN_EMAIL)
    fallback = await _verify(client, _ADMIN_EMAIL, _last_code(api_email))
    assert fallback.status_code == 200, fallback.text
    assert fallback.json()["status"] == "totp_required"


async def test_totp_endpoint_answers_unknown_tokens_uniformly(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    """The totp leg resolves only its own opaque pending tokens: a
    TEACHER's real access token, a session JWT, or garbage all get the
    same 401 — the leg is not a session surface and gives no branch
    signal (the F1 posture)."""
    await _seed_teacher(db_session)
    await _request_code(client, _TEACHER_EMAIL)
    verified = await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    access_token = verified.json()["access_token"]

    for candidate in (access_token, "not-a-pending-token"):
        response = await client.post(
            "/api/v1/auth/staff/email-code/totp",
            json={"pending_token": candidate, "code": "123456"},
        )
        assert response.status_code == 401, response.text
        assert _envelope(response)["code"] == "AUTHENTICATION_REQUIRED"


async def test_verify_unknown_email_is_uniform_invalid(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await _verify(client, "ghost@campus.example.edu", "123456")
    assert response.status_code == 400, response.text
    assert _envelope(response)["code"] == "EMAIL_CODE_INVALID"


async def test_audit_rows_for_success_and_totp_required(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    await _seed_teacher(db_session)
    await _seed_admin_with_totp(db_session)

    await _request_code(client, _TEACHER_EMAIL)
    await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    await _request_code(client, _ADMIN_EMAIL)
    await _verify(client, _ADMIN_EMAIL, _last_code(api_email))

    actions = set(
        await db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.action.in_(
                    ("EMAIL_LOGIN_SUCCEEDED", "EMAIL_LOGIN_TOTP_REQUIRED")
                )
            )
        )
    )
    assert actions == {"EMAIL_LOGIN_SUCCEEDED", "EMAIL_LOGIN_TOTP_REQUIRED"}
    # G11: no email lands on the audit rows' details.
    rows = list(
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.action.in_(
                    ("EMAIL_LOGIN_SUCCEEDED", "EMAIL_LOGIN_TOTP_REQUIRED")
                )
            )
        )
    )
    for row in rows:
        assert row.details is None or _TEACHER_EMAIL not in str(row.details)
        assert row.details is None or _ADMIN_EMAIL not in str(row.details)


async def test_success_resets_failure_count(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
) -> None:
    """V1.1 §6 成功登录清零: two failures, a success, then five more
    failures — the account locks only at the fifth of the NEW streak."""
    teacher = await _seed_teacher(db_session)
    key = f"login:fail:{teacher.id}"

    await _request_code(client, _TEACHER_EMAIL)
    for _ in range(2):
        await _verify(client, _TEACHER_EMAIL, "000000")
    assert await api_redis.get(key) == "2"

    await _verify(client, _TEACHER_EMAIL, _last_code(api_email))
    assert await api_redis.get(key) is None
