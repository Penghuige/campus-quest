# backend/tests/integration/identity/test_profile_contacts.py
"""Student profile contacts against real PostgreSQL and Redis (Task 8).

Covers spec §5.3 (nickname change reuses the grapheme validator), §5.4
(logged-in + re-auth + new-phone OTP phone change; old phone preserved until
confirmation; global phone uniqueness adjudicated by the partial unique
index), and §5.5 (optional email: normalized storage, V1 global uniqueness,
verified only after a short-lived single-use email token consumed from
Redis, unbind behind re-auth that never touches the phone).

Redis runs on the dedicated test database 15, flushed around every test
(same philosophy as `test_otp_flow`); business time is FrozenClock-driven,
so the 24h email-token TTL is a clock advance, never a sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import AccessTokenCodec, hash_password
from app.modules.identity.email_verification import (
    EmailAlreadyBoundError,
    EmailChallenge,
    EmailVerificationService,
    InvalidEmailTokenError,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.otp import (
    ChallengePublic,
    InvalidTokenError,
    OtpChallengeService,
    OtpPolicy,
    OtpPurpose,
    WrongCodeError,
)
from app.modules.identity.profile_service import ProfileService
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import FakeEmailSender, FakeSmsSender

_OTP_TEST_REDIS_DB = 15
_T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_USERNAME_A = "20250010001"
_USERNAME_B = "20250010002"
_PASSWORD = "correct-horse-battery"
_PHONE_A = "+8613700137001"
_PHONE_B = "+8613800138000"
_PHONE_NEW = "+8613900139000"
_CLIENT_IP = "203.0.113.7"
_EMAIL = "user@pku.edu.cn"
_EMAIL_OTHER = "other@pku.edu.cn"
_EMAIL_TOKEN_TTL = timedelta(hours=24)
# >= 32 bytes so PyJWT-class codecs never warn; mirrors test_sessions.
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
async def contacts_redis() -> AsyncIterator[aioredis.Redis]:
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


@pytest.fixture
def email_sender() -> FakeEmailSender:
    return FakeEmailSender()


def _otp_service(
    redis: aioredis.Redis, frozen: FrozenClock, sender: FakeSmsSender
) -> OtpChallengeService:
    return OtpChallengeService(
        redis=redis,
        clock=frozen,
        sms_sender=sender,
        policy=_otp_policy(),
    )


def _otp_policy() -> OtpPolicy:
    return OtpPolicy(
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
    )


def _session_service(frozen: FrozenClock) -> SessionService:
    return SessionService(
        clock=frozen,
        access_codec=AccessTokenCodec(secret=_ACCESS_SECRET, ttl_minutes=15),
        refresh_token_ttl_days=30,
    )


def _profile_service(
    redis: aioredis.Redis, frozen: FrozenClock, sender: FakeSmsSender
) -> ProfileService:
    return ProfileService(
        clock=frozen,
        otp=_otp_service(redis, frozen, sender),
        otp_policy=_otp_policy(),
        sessions=_session_service(frozen),
    )


def _email_service(
    redis: aioredis.Redis, frozen: FrozenClock, sender: FakeEmailSender
) -> EmailVerificationService:
    return EmailVerificationService(
        clock=frozen, email_sender=sender, redis=redis, token_ttl_hours=24
    )


async def _seed_user(
    db: AsyncSession,
    *,
    username: str,
    phone: str | None = None,
    email: str | None = None,
    email_verified: bool = False,
    role: Role = Role.STUDENT,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"同学{username[-2:]}",
        phone_e164=phone,
        email_normalized=email,
        email_verified_at=_T0 if email_verified else None,
        role=role.value,
        status=status.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _reload_user(db: AsyncSession, user_id: UUID) -> User:
    db.expunge_all()
    found = await db.scalar(select(User).where(User.id == user_id))
    assert found is not None
    return found


def _sms_code(sender: FakeSmsSender) -> str:
    code = sender.messages[-1].variables["code"]
    assert isinstance(code, str)
    return code


# --- nickname (spec §5.3) -----------------------------------------------------


@pytest.mark.integration
async def test_change_nickname_normalizes_and_persists(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _profile_service(contacts_redis, clock, sms)

    updated = await service.change_nickname(db_session, user.id, "  昵称😀同学  ")

    assert updated.nickname == "昵称😀同学"
    persisted = await _reload_user(db_session, user.id)
    assert persisted.nickname == "昵称😀同学"


@pytest.mark.integration
async def test_change_nickname_enforces_16_grapheme_boundary(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    original = user.nickname
    service = _profile_service(contacts_redis, clock, sms)

    # 16 grapheme clusters (a ZWJ family emoji counts as one): accepted.
    sixteen = "👨‍👩‍👧‍👦" * 16
    updated = await service.change_nickname(db_session, user.id, sixteen)
    assert updated.nickname == sixteen

    # 17 clusters: rejected with the registry validation code, nothing persisted.
    with pytest.raises(BusinessError) as exc_info:
        await service.change_nickname(db_session, user.id, "👨‍👩‍👧‍👦" * 17)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.status_code == 400
    persisted = await _reload_user(db_session, user.id)
    assert persisted.nickname == sixteen
    assert persisted.nickname != original


@pytest.mark.integration
async def test_change_nickname_rejects_blank_after_cleaning(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _profile_service(contacts_redis, clock, sms)

    with pytest.raises(BusinessError) as exc_info:
        await service.change_nickname(db_session, user.id, "  ")
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    persisted = await _reload_user(db_session, user.id)
    assert persisted.nickname == f"同学{_USERNAME_A[-2:]}"


# --- phone change (spec §5.4) -------------------------------------------------


@pytest.mark.integration
async def test_phone_change_request_requires_current_password(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _profile_service(contacts_redis, clock, sms)

    with pytest.raises(BusinessError) as exc_info:
        await service.request_phone_change(
            db_session,
            user.id,
            "wrong-horse-battery",
            "139 0013 9000",
            client_ip=_CLIENT_IP,
        )

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    # Re-auth runs before anything else: no SMS, no challenge side effects.
    assert sms.messages == []
    persisted = await _reload_user(db_session, user.id)
    assert persisted.phone_e164 == _PHONE_A


@pytest.mark.integration
async def test_phone_change_request_sends_otp_to_new_phone_and_keeps_old(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _profile_service(contacts_redis, clock, sms)

    # Domestic formatting of the new number: the challenge normalizes to E.164.
    challenge = await service.request_phone_change(
        db_session, user.id, _PASSWORD, "139 0013 9000", client_ip=_CLIENT_IP
    )

    assert isinstance(challenge, ChallengePublic)
    assert challenge.expires_at == _T0 + timedelta(minutes=5)
    assert len(sms.messages) == 1
    assert sms.messages[0].to == _PHONE_NEW
    # The old phone is preserved until confirmation completes.
    persisted = await _reload_user(db_session, user.id)
    assert persisted.phone_e164 == _PHONE_A


@pytest.mark.integration
async def test_phone_change_request_rejects_phone_bound_elsewhere_or_self(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    await _seed_user(db_session, username=_USERNAME_B, phone=_PHONE_NEW)
    service = _profile_service(contacts_redis, clock, sms)

    with pytest.raises(BusinessError) as bound_exc:
        await service.request_phone_change(
            db_session, user.id, _PASSWORD, _PHONE_NEW, client_ip=_CLIENT_IP
        )
    assert bound_exc.value.code == ErrorCode.PHONE_ALREADY_BOUND
    assert bound_exc.value.status_code == 409

    # Binding one's own current phone again is the same no-op conflict.
    with pytest.raises(BusinessError) as self_exc:
        await service.request_phone_change(
            db_session, user.id, _PASSWORD, _PHONE_A, client_ip=_CLIENT_IP
        )
    assert self_exc.value.code == ErrorCode.PHONE_ALREADY_BOUND
    assert sms.messages == []


@pytest.mark.integration
async def test_phone_change_confirm_swaps_phone_and_frees_old(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _profile_service(contacts_redis, clock, sms)
    challenge = await service.request_phone_change(
        db_session, user.id, _PASSWORD, _PHONE_NEW, client_ip=_CLIENT_IP
    )

    # A wrong code leaves the challenge alive and the phone untouched.
    with pytest.raises(WrongCodeError):
        await service.confirm_phone_change(
            db_session, user.id, challenge.challenge_id, "000000"
        )
    persisted = await _reload_user(db_session, user.id)
    assert persisted.phone_e164 == _PHONE_A

    updated = await service.confirm_phone_change(
        db_session, user.id, challenge.challenge_id, _sms_code(sms)
    )

    assert updated.phone_e164 == _PHONE_NEW
    persisted = await _reload_user(db_session, user.id)
    assert persisted.phone_e164 == _PHONE_NEW
    # The old number is freed: no account holds it anymore.
    old_holder = await db_session.scalar(
        select(User).where(User.phone_e164 == _PHONE_A)
    )
    assert old_holder is None


@pytest.mark.integration
async def test_phone_change_confirm_rejects_wrong_purpose_proof(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    otp = _otp_service(contacts_redis, clock, sms)
    service = _profile_service(contacts_redis, clock, sms)

    # A REGISTER-purpose challenge to the new phone: possession is proven,
    # but the proof was minted for a different operation and must not
    # authorize a phone swap (OTP purpose confusion).
    challenge = await otp.request_phone_challenge(
        _PHONE_NEW, OtpPurpose.REGISTER, client_ip=_CLIENT_IP
    )

    with pytest.raises(InvalidTokenError):
        await service.confirm_phone_change(
            db_session, user.id, challenge.challenge_id, _sms_code(sms)
        )
    persisted = await _reload_user(db_session, user.id)
    assert persisted.phone_e164 == _PHONE_A


@pytest.mark.integration
async def test_phone_change_confirm_loses_unique_race(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    other = await _seed_user(db_session, username=_USERNAME_B, phone=_PHONE_B)
    # Land the seed in its own savepoint: the service's rollback on the
    # lost race must revert only the phone writes, not the test data.
    await db_session.commit()
    service = _profile_service(contacts_redis, clock, sms)
    challenge = await service.request_phone_change(
        db_session, user.id, _PASSWORD, _PHONE_NEW, client_ip=_CLIENT_IP
    )

    # The request-time pre-check passed, but the target phone gets bound
    # before confirmation: the partial unique index must close the race.
    user_id = user.id
    other.phone_e164 = _PHONE_NEW
    await db_session.flush()

    with pytest.raises(BusinessError) as exc_info:
        await service.confirm_phone_change(
            db_session, user_id, challenge.challenge_id, _sms_code(sms)
        )
    assert exc_info.value.code == ErrorCode.PHONE_ALREADY_BOUND
    assert exc_info.value.status_code == 409

    # The failed swap rolled back; the account keeps its old phone.
    persisted = await _reload_user(db_session, user_id)
    assert persisted.phone_e164 == _PHONE_A


async def _confirm_on_own_session(
    engine: AsyncEngine,
    service: ProfileService,
    user_id: UUID,
    challenge: ChallengePublic,
    code: str,
) -> User | BusinessError:
    """Run one phone-change confirmation on an independent session with real
    commits, so `asyncio.gather` exercises true concurrent index contention."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            return await service.confirm_phone_change(
                session, user_id, challenge.challenge_id, code
            )
        except BusinessError as exc:
            return exc


@pytest.mark.integration
async def test_concurrent_phone_change_to_same_phone_exactly_one_wins(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
) -> None:
    # Two accounts, two separately-issued challenges for one new phone: the
    # request-time pre-checks both passed, so only the database constraint
    # can keep the phone globally unique (spec §5.4).
    usernames = {_USERNAME_A, _USERNAME_B}
    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username=_USERNAME_A,
                password_hash=hash_password(_PASSWORD),
                nickname="并发甲",
                phone_e164=_PHONE_A,
                role=Role.STUDENT.value,
                status=UserStatus.ACTIVE.value,
            )
        )
        session.add(
            User(
                username=_USERNAME_B,
                password_hash=hash_password(_PASSWORD),
                nickname="并发乙",
                phone_e164=_PHONE_B,
                role=Role.STUDENT.value,
                status=UserStatus.ACTIVE.value,
            )
        )
        await session.commit()
    try:
        service = _profile_service(contacts_redis, clock, sms)
        async with AsyncSession(db_engine) as session:
            user_a = await session.scalar(
                select(User).where(User.username == _USERNAME_A)
            )
            user_b = await session.scalar(
                select(User).where(User.username == _USERNAME_B)
            )
            assert user_a is not None and user_b is not None

            challenge_a = await service.request_phone_change(
                session, user_a.id, _PASSWORD, _PHONE_NEW, client_ip=_CLIENT_IP
            )
            # Advance past the resend cooldown so the second challenge for
            # the same number can be issued (still inside both TTLs).
            _advance(clock, seconds=61)
            challenge_b = await service.request_phone_change(
                session, user_b.id, _PASSWORD, _PHONE_NEW, client_ip=_CLIENT_IP
            )
            code_a = sms.messages[0].variables["code"]
            code_b = sms.messages[1].variables["code"]
            assert isinstance(code_a, str) and isinstance(code_b, str)
            user_a_id, user_b_id = user_a.id, user_b.id

        results = await asyncio.gather(
            _confirm_on_own_session(db_engine, service, user_a_id, challenge_a, code_a),
            _confirm_on_own_session(db_engine, service, user_b_id, challenge_b, code_b),
        )

        successes = [result for result in results if isinstance(result, User)]
        failures = [result for result in results if isinstance(result, BusinessError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == ErrorCode.PHONE_ALREADY_BOUND
        assert failures[0].status_code == 409

        async with AsyncSession(db_engine) as verifier:
            holders = list(
                await verifier.scalars(
                    select(User).where(User.phone_e164 == _PHONE_NEW)
                )
            )
            assert len(holders) == 1
            winner = successes[0]
            assert holders[0].id == winner.id
    finally:
        async with AsyncSession(db_engine) as session:
            await session.execute(delete(User).where(User.username.in_(usernames)))
            await session.commit()


# --- email (spec §5.5) --------------------------------------------------------


@pytest.mark.integration
async def test_email_request_normalizes_stores_unverified_and_sends(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _email_service(contacts_redis, clock, email_sender)

    challenge = await service.request_email_verification(
        db_session, user.id, "  User@PKU.edu.cn  "
    )

    assert isinstance(challenge, EmailChallenge)
    assert challenge.user_id == user.id
    assert challenge.expires_at == _T0 + _EMAIL_TOKEN_TTL
    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_normalized == _EMAIL
    # Stored but NOT verified: verification happens only at token consumption.
    assert persisted.email_verified_at is None
    assert len(email_sender.messages) == 1
    sent = email_sender.messages[0]
    assert sent.to == _EMAIL
    assert sent.template == "email_verification"
    assert isinstance(sent.variables["token"], str) and sent.variables["token"]


@pytest.mark.integration
async def test_email_request_rejects_email_bound_elsewhere_allows_self_resend(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    await _seed_user(db_session, username=_USERNAME_B, phone=_PHONE_B, email=_EMAIL)
    service = _email_service(contacts_redis, clock, email_sender)

    with pytest.raises(EmailAlreadyBoundError):
        await service.request_email_verification(db_session, user.id, _EMAIL)
    assert email_sender.messages == []
    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_normalized is None

    # Re-requesting one's own (even verified) address restarts the cycle.
    own = await _reload_user(db_session, user.id)
    own.email_normalized = _EMAIL_OTHER
    own.email_verified_at = _T0
    await db_session.flush()
    await service.request_email_verification(db_session, user.id, _EMAIL_OTHER)
    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_normalized == _EMAIL_OTHER
    assert persisted.email_verified_at is None


@pytest.mark.integration
async def test_email_verified_only_after_token_consumption(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _email_service(contacts_redis, clock, email_sender)
    await service.request_email_verification(db_session, user.id, _EMAIL)
    token = email_sender.messages[-1].variables["token"]
    assert isinstance(token, str)

    verified = await service.confirm_email_verification(db_session, user.id, token)

    assert verified.email_verified_at == _T0
    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_verified_at == _T0
    assert persisted.email_normalized == _EMAIL


@pytest.mark.integration
async def test_email_token_is_single_use(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _email_service(contacts_redis, clock, email_sender)
    await service.request_email_verification(db_session, user.id, _EMAIL)
    token = email_sender.messages[-1].variables["token"]
    assert isinstance(token, str)
    await service.confirm_email_verification(db_session, user.id, token)

    # Replaying the same token must fail (single-use consume).
    with pytest.raises(InvalidEmailTokenError):
        await service.confirm_email_verification(db_session, user.id, token)


@pytest.mark.integration
async def test_email_token_is_short_lived(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    service = _email_service(contacts_redis, clock, email_sender)
    await service.request_email_verification(db_session, user.id, _EMAIL)
    token = email_sender.messages[-1].variables["token"]
    assert isinstance(token, str)

    # Business time moves past the 24h TTL: FrozenClock, no sleep.
    _advance(clock, hours=24, seconds=1)

    with pytest.raises(InvalidEmailTokenError):
        await service.confirm_email_verification(db_session, user.id, token)
    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_verified_at is None


@pytest.mark.integration
async def test_email_token_rejects_other_user_and_stale_address(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    other = await _seed_user(db_session, username=_USERNAME_B, phone=_PHONE_B)
    service = _email_service(contacts_redis, clock, email_sender)

    # A token minted for user A cannot verify user B's account.
    await service.request_email_verification(db_session, user.id, _EMAIL)
    foreign_token = email_sender.messages[-1].variables["token"]
    assert isinstance(foreign_token, str)
    with pytest.raises(InvalidEmailTokenError):
        await service.confirm_email_verification(db_session, other.id, foreign_token)

    # A token for a superseded address must not verify the newest one.
    await service.request_email_verification(db_session, user.id, _EMAIL)
    stale_token = email_sender.messages[-1].variables["token"]
    assert isinstance(stale_token, str)
    await service.request_email_verification(db_session, user.id, _EMAIL_OTHER)
    fresh_token = email_sender.messages[-1].variables["token"]
    assert isinstance(fresh_token, str)

    with pytest.raises(InvalidEmailTokenError):
        await service.confirm_email_verification(db_session, user.id, stale_token)
    verified = await service.confirm_email_verification(
        db_session, user.id, fresh_token
    )
    assert verified.email_normalized == _EMAIL_OTHER
    assert verified.email_verified_at == _T0


@pytest.mark.integration
async def test_unbind_email_requires_password_and_clears_address(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    user = await _seed_user(
        db_session,
        username=_USERNAME_A,
        phone=_PHONE_A,
        email=_EMAIL,
        email_verified=True,
    )
    service = _email_service(contacts_redis, clock, email_sender)

    with pytest.raises(BusinessError) as exc_info:
        await service.unbind_email(db_session, user.id, "wrong-horse-battery")
    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_normalized == _EMAIL

    await service.unbind_email(db_session, user.id, _PASSWORD)

    persisted = await _reload_user(db_session, user.id)
    assert persisted.email_normalized is None
    assert persisted.email_verified_at is None
    # The phone binding is untouched by the unbind.
    assert persisted.phone_e164 == _PHONE_A


@pytest.mark.integration
async def test_unbind_email_keeps_login_capability(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
) -> None:
    # Spec §5.5: unbinding the email must not affect the phone-bound
    # account's ability to log in (student login = student number + password).
    user = await _seed_user(
        db_session,
        username=_USERNAME_A,
        phone=_PHONE_A,
        email=_EMAIL,
        email_verified=True,
    )
    email_service = _email_service(contacts_redis, clock, email_sender)
    await email_service.unbind_email(db_session, user.id, _PASSWORD)

    tokens = await _session_service(clock).login_student(
        db_session, _USERNAME_A, _PASSWORD
    )

    assert tokens.refresh_token


async def _bind_on_own_session(
    engine: AsyncEngine,
    service: EmailVerificationService,
    user_id: UUID,
    email: str,
) -> EmailChallenge | EmailAlreadyBoundError:
    """Run one email bind on an independent session with real commits, so
    `asyncio.gather` exercises true unique-index contention."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            return await service.request_email_verification(session, user_id, email)
        except EmailAlreadyBoundError as exc:
            return exc


@pytest.mark.integration
async def test_concurrent_email_bind_same_address_exactly_one_wins(
    db_engine: AsyncEngine,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    email_sender: FakeEmailSender,
) -> None:
    # T8 review carry-forward: two accounts concurrently bind the SAME
    # email. Both passed the friendly pre-check (the slot was free), so only
    # the partial unique index `uq_users_email_normalized` can keep V1's
    # global email uniqueness — exactly one bind lands, the loser gets the
    # typed EmailAlreadyBoundError.
    usernames = {_USERNAME_A, _USERNAME_B}
    async with AsyncSession(db_engine) as session:
        for username, phone in (
            (_USERNAME_A, _PHONE_A),
            (_USERNAME_B, _PHONE_B),
        ):
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(_PASSWORD),
                    nickname=f"并发邮箱{username[-1]}",
                    phone_e164=phone,
                    role=Role.STUDENT.value,
                    status=UserStatus.ACTIVE.value,
                )
            )
        await session.commit()
    try:
        service = _email_service(contacts_redis, clock, email_sender)
        async with AsyncSession(db_engine) as session:
            user_a = await session.scalar(
                select(User).where(User.username == _USERNAME_A)
            )
            user_b = await session.scalar(
                select(User).where(User.username == _USERNAME_B)
            )
            assert user_a is not None and user_b is not None
            user_a_id, user_b_id = user_a.id, user_b.id

        results = await asyncio.gather(
            _bind_on_own_session(db_engine, service, user_a_id, _EMAIL),
            _bind_on_own_session(db_engine, service, user_b_id, _EMAIL),
        )

        successes = [result for result in results if isinstance(result, EmailChallenge)]
        failures = [
            result for result in results if isinstance(result, EmailAlreadyBoundError)
        ]
        assert len(successes) == 1
        assert len(failures) == 1

        async with AsyncSession(db_engine) as verifier:
            holders = list(
                await verifier.scalars(
                    select(User).where(User.email_normalized == _EMAIL)
                )
            )
            assert len(holders) == 1
    finally:
        async with AsyncSession(db_engine) as session:
            await session.execute(delete(User).where(User.username.in_(usernames)))
            await session.commit()


# --- logging discipline (backend-engineering §15) -----------------------------


@pytest.mark.integration
async def test_contact_flows_never_log_secrets(
    db_session: AsyncSession,
    contacts_redis: aioredis.Redis,
    clock: FrozenClock,
    sms: FakeSmsSender,
    email_sender: FakeEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = await _seed_user(db_session, username=_USERNAME_A, phone=_PHONE_A)
    profile = _profile_service(contacts_redis, clock, sms)
    email_service = _email_service(contacts_redis, clock, email_sender)

    with caplog.at_level(logging.INFO):
        await profile.change_nickname(db_session, user.id, "日志测试同学")
        challenge = await profile.request_phone_change(
            db_session, user.id, _PASSWORD, _PHONE_NEW, client_ip=_CLIENT_IP
        )
        with contextlib.suppress(Exception):
            await profile.confirm_phone_change(
                db_session, user.id, challenge.challenge_id, "000000"
            )
        await profile.confirm_phone_change(
            db_session, user.id, challenge.challenge_id, _sms_code(sms)
        )
        await email_service.request_email_verification(db_session, user.id, _EMAIL)
        token = email_sender.messages[-1].variables["token"]
        assert isinstance(token, str)
        await email_service.confirm_email_verification(db_session, user.id, token)
        await email_service.unbind_email(db_session, user.id, _PASSWORD)

    secret_material = {
        _PASSWORD,
        _PHONE_A,
        _PHONE_NEW,
        _EMAIL,
        token,
    }
    for record in caplog.records:
        message = record.getMessage()
        for secret in secret_material:
            assert secret not in message
