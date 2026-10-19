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
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.errors import BusinessError
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



# --- the durability legs (the e2e-caught defect's regression pins) ---------


async def test_request_leaves_the_challenge_durable_across_sessions(
    db_engine: AsyncEngine,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
) -> None:
    """The 202 must leave the challenge durable (#85 lesson, the student
    leg's commit boundary): the savepoint-wrapped suite sessions CANNOT
    see this defect (an uncommitted row is visible inside the same
    transaction), so this pin runs on a REAL commit — a fresh session
    must find the row after request_staff_code returns."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.identity.mobile_login_service import MobileLoginService
    from app.modules.identity.models import LoginMobileChallenge, User

    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username="durable-smc@campus.example.edu",
                password_hash=None,
                nickname="durable",
                email_normalized=_TEACHER_EMAIL,
                email_verified_at=_T0,
                phone_e164=_PHONE_TEACHER,
                role=Role.TEACHER,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()

    service = MobileLoginService(
        sms_sender=api_sms,
        redis=api_redis,
        clock=api_clock,
        hmac_secret=get_settings().otp_hmac_secret,
    )
    async with AsyncSession(db_engine) as session:
        await service.request_staff_code(session, _PHONE_TEACHER)

    async with AsyncSession(db_engine) as verify:
        rows = (
            await verify.scalars(
                select(LoginMobileChallenge).where(
                    LoginMobileChallenge.phone_e164 == _PHONE_TEACHER
                )
            )
        ).all()
        assert len(rows) == 1, "the challenge row died with the transaction"
    # cleanup the committed rows (the suite's outer rollback cannot)
    async with AsyncSession(db_engine) as cleanup:
        await cleanup.execute(
            delete(LoginMobileChallenge).where(
                LoginMobileChallenge.phone_e164 == _PHONE_TEACHER
            )
        )
        await cleanup.execute(
            delete(User).where(User.username == "durable-smc@campus.example.edu")
        )
        await cleanup.commit()


async def test_failed_send_releases_the_cooldown(
    db_engine: AsyncEngine,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
) -> None:
    """A carrier failure burns no cooldown: the arm is released so the
    retry is not 429-blocked for 60s (the student leg's discipline)."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.integrations.errors import ProviderError
    from app.modules.identity.mobile_login_service import MobileLoginService
    from app.modules.identity.models import LoginMobileChallenge, User

    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username="cooldown-smc@campus.example.edu",
                password_hash=None,
                nickname="cooldown",
                email_normalized=_TEACHER_EMAIL,
                email_verified_at=_T0,
                phone_e164=_PHONE_TEACHER,
                role=Role.TEACHER,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()

    service = MobileLoginService(
        sms_sender=api_sms,
        redis=api_redis,
        clock=api_clock,
        hmac_secret=get_settings().otp_hmac_secret,
    )
    api_sms.failures.append(ProviderError("carrier down"))
    async with AsyncSession(db_engine) as session:
        with pytest.raises(BusinessError):
            await service.request_staff_code(session, _PHONE_TEACHER)

    async with AsyncSession(db_engine) as session:
        # The retry goes through — the failed attempt armed no cooldown.
        await service.request_staff_code(session, _PHONE_TEACHER)
    assert len(api_sms.messages) == 1
    async with AsyncSession(db_engine) as cleanup:
        await cleanup.execute(
            delete(LoginMobileChallenge).where(
                LoginMobileChallenge.phone_e164 == _PHONE_TEACHER
            )
        )
        await cleanup.execute(
            delete(User).where(User.username == "cooldown-smc@campus.example.edu")
        )
        await cleanup.commit()
