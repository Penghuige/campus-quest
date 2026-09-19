# backend/tests/integration/identity/test_password_recovery.py
"""Password recovery and password change against real PostgreSQL/Redis (Task 8).

Spec §5.6: every student carries a verified phone, so V1 password recovery
uses the bound phone as the mandatory factor (staff accounts are rejected —
they have no student phone-reset path); a successful reset rotates the
Argon2id verifier and revokes every refresh session; replaying a consumed
reset challenge must fail; and the request response is uniform whether or
not the username exists (no account enumeration).

`change_password` (the Task-5 deferred stub) lives here too: current-password
re-auth, the 10-128 band, and revocation of every refresh session EXCEPT
the caller's current one.

Redis runs on the dedicated test database 15, flushed around every test;
business time is FrozenClock-driven (backend-engineering §11).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import (
    AccessTokenCodec,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User, UserSession
from app.modules.identity.otp import (
    ChallengeAlreadyConsumedError,
    ChallengePublic,
    InvalidTokenError,
    OtpChallengeService,
    OtpPolicy,
    OtpPurpose,
    UnknownChallengeError,
    WrongCodeError,
)
from app.modules.identity.profile_service import (
    PasswordResetNotAllowedError,
    ProfileService,
)
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import FakeSmsSender

_OTP_TEST_REDIS_DB = 15
_T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_USERNAME = "20250010001"
_STAFF_USERNAME = "teacher@pku.edu.cn"
_PASSWORD = "correct-horse-battery"
_NEW_PASSWORD = "staple-battery-camel"
_PHONE = "+8613700137001"
_CLIENT_IP = "203.0.113.7"
_CHALLENGE_TTL = timedelta(minutes=5)
_ACCESS_SECRET = "integration-test-access-token-secret-0123456789"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_OTP_TEST_REDIS_DB}"))


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


@pytest_asyncio.fixture
async def recovery_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


def _otp_service(
    redis: aioredis.Redis, frozen: FrozenClock, sender: FakeSmsSender
) -> OtpChallengeService:
    return OtpChallengeService(
        redis=redis,
        clock=frozen,
        sms_sender=sender,
        policy=OtpPolicy(
            ttl_seconds=300,
            max_verify_attempts=5,
            resend_cooldown_seconds=60,
            verified_token_ttl_seconds=600,
            phone_hourly_request_limit=5,
            phone_daily_request_limit=20,
            ip_hourly_request_limit=50,
            ip_daily_request_limit=200,
            hmac_secret="integration-test-otp-hmac-secret",
            default_region="CN",
        ),
    )


def _make_services(
    redis: aioredis.Redis, frozen: FrozenClock, sender: FakeSmsSender
) -> tuple[ProfileService, SessionService]:
    otp = _otp_service(redis, frozen, sender)
    sessions = SessionService(
        clock=frozen,
        access_codec=AccessTokenCodec(secret=_ACCESS_SECRET, ttl_minutes=15),
        refresh_token_ttl_days=30,
    )
    return ProfileService(clock=frozen, otp=otp, sessions=sessions), sessions


async def _seed_student(
    db: AsyncSession,
    *,
    username: str = _USERNAME,
    phone: str | None = _PHONE,
    role: Role = Role.STUDENT,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="找回密码同学",
        phone_e164=phone,
        role=role.value,
        status=status.value,
    )
    db.add(user)
    await db.flush()
    return user


def _sms_code(sender: FakeSmsSender) -> str:
    code = sender.messages[-1].variables["code"]
    assert isinstance(code, str)
    return code


async def _count_unrevoked_sessions(db: AsyncSession, user_id: UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.replaced_by.is_(None),
            )
        )
        or 0
    )


async def _session_row(db: AsyncSession, refresh_token: str) -> UserSession | None:
    return await db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == hash_refresh_token(refresh_token)
        )
    )


async def _reload_user(db: AsyncSession, user_id: UUID) -> User:
    db.expunge_all()
    found = await db.scalar(select(User).where(User.id == user_id))
    assert found is not None
    return found


# --- reset request (spec §5.6 recovery via the bound phone) -------------------


@pytest.mark.integration
async def test_reset_request_sends_otp_to_bound_phone(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)

    challenge = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )

    assert isinstance(challenge, ChallengePublic)
    assert challenge.expires_at == _T0 + _CHALLENGE_TTL
    assert len(sms.messages) == 1
    assert sms.messages[0].to == _PHONE
    assert user.phone_e164 == _PHONE


@pytest.mark.integration
async def test_reset_request_unknown_username_is_uniform(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # No account enumeration: an unknown username answers with the same
    # challenge-shaped response (a decoy) and sends nothing. Confirming
    # against the decoy fails exactly like any other unknown challenge.
    await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)

    known = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )
    unknown = await service.request_password_reset(
        db_session, "20990099999", client_ip=_CLIENT_IP
    )

    assert isinstance(unknown, ChallengePublic)
    assert unknown.challenge_id != known.challenge_id
    assert unknown.expires_at == known.expires_at == _T0 + _CHALLENGE_TTL
    # Only the real account's phone received a code.
    assert len(sms.messages) == 1
    with pytest.raises(UnknownChallengeError):
        await service.confirm_password_reset(
            db_session, unknown.challenge_id, "123456", _NEW_PASSWORD
        )


@pytest.mark.integration
async def test_reset_request_rejects_staff_accounts(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # Staff do not use the student phone-reset path: a typed error the
    # router maps (staff recovery is an admin flow, out of V1 scope).
    await _seed_student(db_session, username=_STAFF_USERNAME, role=Role.TEACHER)
    service, _ = _make_services(recovery_redis, clock, sms)

    with pytest.raises(PasswordResetNotAllowedError):
        await service.request_password_reset(
            db_session, _STAFF_USERNAME, client_ip=_CLIENT_IP
        )
    assert sms.messages == []


@pytest.mark.integration
async def test_reset_request_phoneless_student_is_uniform(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # A student row without a bound phone cannot receive a recovery code;
    # the response stays uniform instead of revealing the account's state.
    await _seed_student(db_session, phone=None, status=UserStatus.PENDING_PHONE)
    service, _ = _make_services(recovery_redis, clock, sms)

    challenge = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )

    assert isinstance(challenge, ChallengePublic)
    assert challenge.expires_at == _T0 + _CHALLENGE_TTL
    assert sms.messages == []


# --- reset confirmation --------------------------------------------------------


@pytest.mark.integration
async def test_reset_confirm_rotates_password_and_revokes_all_sessions(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_student(db_session)
    service, sessions = _make_services(recovery_redis, clock, sms)
    first = await sessions.login_student(db_session, _USERNAME, _PASSWORD)
    second = await sessions.login_student(db_session, _USERNAME, _PASSWORD)
    assert await _count_unrevoked_sessions(db_session, user.id) == 2

    challenge = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )
    await service.confirm_password_reset(
        db_session, challenge.challenge_id, _sms_code(sms), _NEW_PASSWORD
    )

    # Argon2id verifier rotated: the new password verifies, the old one does not.
    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_NEW_PASSWORD, persisted.password_hash)
    assert not verify_password(_PASSWORD, persisted.password_hash)

    # Spec §5.6: every pre-reset refresh session is revoked.
    assert await _count_unrevoked_sessions(db_session, user.id) == 0
    for dead_token in (first.refresh_token, second.refresh_token):
        with pytest.raises(BusinessError) as exc_info:
            await sessions.rotate_refresh(db_session, dead_token)
        assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED

    # The account logs in immediately with the new password.
    fresh = await sessions.login_student(db_session, _USERNAME, _NEW_PASSWORD)
    assert fresh.refresh_token
    assert await _count_unrevoked_sessions(db_session, user.id) == 1


@pytest.mark.integration
async def test_reset_challenge_replay_fails(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)
    challenge = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )
    code = _sms_code(sms)
    await service.confirm_password_reset(
        db_session, challenge.challenge_id, code, _NEW_PASSWORD
    )

    # Replaying the consumed challenge must fail without changing anything.
    with pytest.raises(ChallengeAlreadyConsumedError):
        await service.confirm_password_reset(
            db_session, challenge.challenge_id, code, _NEW_PASSWORD
        )


@pytest.mark.integration
async def test_reset_confirm_wrong_code_keeps_password(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)
    challenge = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )

    with pytest.raises(WrongCodeError):
        await service.confirm_password_reset(
            db_session, challenge.challenge_id, "000000", _NEW_PASSWORD
        )

    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_PASSWORD, persisted.password_hash)


@pytest.mark.integration
async def test_reset_confirm_out_of_band_password_does_not_burn_challenge(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # Registration ordering principle: a malformed request never consumes a
    # single-use proof. The band check runs before the OTP is touched, so
    # the same code still completes the reset afterwards.
    user = await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)
    challenge = await service.request_password_reset(
        db_session, _USERNAME, client_ip=_CLIENT_IP
    )

    for bad_password in ("short", "a" * 129):
        with pytest.raises(BusinessError) as exc_info:
            await service.confirm_password_reset(
                db_session, challenge.challenge_id, _sms_code(sms), bad_password
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert exc_info.value.status_code == 400

    await service.confirm_password_reset(
        db_session, challenge.challenge_id, _sms_code(sms), _NEW_PASSWORD
    )
    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_NEW_PASSWORD, persisted.password_hash)


@pytest.mark.integration
async def test_reset_confirm_rejects_wrong_purpose_proof(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # An OTP minted for registration to the bound phone must not authorize
    # a password reset (OTP purpose confusion; spec §33.2 single-purpose).
    user = await _seed_student(db_session)
    service, sessions = _make_services(recovery_redis, clock, sms)
    otp = _otp_service(recovery_redis, clock, sms)
    challenge = await otp.request_phone_challenge(
        _PHONE, OtpPurpose.REGISTER, client_ip=_CLIENT_IP
    )

    with pytest.raises(InvalidTokenError):
        await service.confirm_password_reset(
            db_session, challenge.challenge_id, _sms_code(sms), _NEW_PASSWORD
        )

    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_PASSWORD, persisted.password_hash)
    fresh = await sessions.login_student(db_session, _USERNAME, _PASSWORD)
    assert fresh.refresh_token


# --- change_password (Task-5 deferred stub, implemented here) -----------------


@pytest.mark.integration
async def test_change_password_requires_current_password(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)

    with pytest.raises(BusinessError) as exc_info:
        await service.change_password(
            db_session, user.id, "wrong-horse-battery", _NEW_PASSWORD
        )

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_PASSWORD, persisted.password_hash)


@pytest.mark.integration
async def test_change_password_auth_precedes_band_validation(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # Re-auth is checked FIRST: a wrong current password answers with the
    # auth failure even when the new password is also invalid.
    user = await _seed_student(db_session)
    service, _ = _make_services(recovery_redis, clock, sms)

    with pytest.raises(BusinessError) as auth_exc:
        await service.change_password(db_session, user.id, "wrong-horse", "short")
    assert auth_exc.value.code == ErrorCode.AUTHENTICATION_REQUIRED

    with pytest.raises(BusinessError) as band_exc:
        await service.change_password(db_session, user.id, _PASSWORD, "short")
    assert band_exc.value.code == ErrorCode.VALIDATION_ERROR
    assert band_exc.value.status_code == 400
    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_PASSWORD, persisted.password_hash)


@pytest.mark.integration
async def test_change_password_revokes_other_sessions_keeps_current(
    db_session: AsyncSession,
    recovery_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_student(db_session)
    service, sessions = _make_services(recovery_redis, clock, sms)
    current = await sessions.login_student(db_session, _USERNAME, _PASSWORD)
    other = await sessions.login_student(db_session, _USERNAME, _PASSWORD)
    current_row = await _session_row(db_session, current.refresh_token)
    assert current_row is not None
    assert await _count_unrevoked_sessions(db_session, user.id) == 2

    await service.change_password(
        db_session,
        user.id,
        _PASSWORD,
        _NEW_PASSWORD,
        current_session_id=current_row.id,
    )

    # Hash rotated; the caller's session survives, every other one dies.
    persisted = await _reload_user(db_session, user.id)
    assert verify_password(_NEW_PASSWORD, persisted.password_hash)
    assert await _count_unrevoked_sessions(db_session, user.id) == 1
    rotated = await sessions.rotate_refresh(db_session, current.refresh_token)
    assert rotated.refresh_token
    with pytest.raises(BusinessError) as exc_info:
        await sessions.rotate_refresh(db_session, other.refresh_token)
    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED

    # Old password no longer logs in; the new one does.
    with pytest.raises(BusinessError):
        await sessions.login_student(db_session, _USERNAME, _PASSWORD)
    fresh = await sessions.login_student(db_session, _USERNAME, _NEW_PASSWORD)
    assert fresh.refresh_token
