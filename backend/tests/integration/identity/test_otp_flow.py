# backend/tests/integration/identity/test_otp_flow.py
"""OTP challenge lifecycle against real Redis (spec §5.4, §33.2; Task 4 brief).

Runs on the compose Redis from `settings.redis_url`, rewritten onto the
dedicated test database 15 and flushed around every test — same philosophy
as `db_guard`: integration tests never touch the developer database.

Atomicity here is the real thing: the two concurrently awaited verifies run
as independent commands on pooled connections, so the exactly-one-consume
proof exercises Redis server-side serialization of HSETNX (challenge claim)
and GETDEL (token consume), not a fake's cooperative scheduling.

Business time stays frozen (FrozenClock): the service enforces expiry and
cooldown client-side against the injected Clock, and the Redis key TTLs are
cleanup backstops with a grace period, so no test sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.modules.identity.otp import (
    ChallengeAlreadyConsumedError,
    ChallengeExpiredError,
    OtpChallengeService,
    OtpPolicy,
    OtpPurpose,
    ResendCooldownError,
    VerifiedPhone,
    VerifiedPhoneToken,
)
from tests.fakes.integrations import FakeSmsSender

_OTP_TEST_REDIS_DB = 15
_T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_PHONE = "+8613700137001"
_CHALLENGE_KEY = "otp:challenge:{challenge_id}"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"OTP integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_OTP_TEST_REDIS_DB}"))


def _advance(clock: FrozenClock, seconds: float) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(seconds=seconds))


@pytest_asyncio.fixture
async def otp_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def otp_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def otp_sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def otp_service(
    otp_redis: aioredis.Redis, otp_clock: FrozenClock, otp_sms: FakeSmsSender
) -> OtpChallengeService:
    return OtpChallengeService(
        redis=otp_redis,
        clock=otp_clock,
        sms_sender=otp_sms,
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


@pytest.mark.integration
async def test_full_request_verify_and_token_resolution(
    otp_service: OtpChallengeService,
    otp_sms: FakeSmsSender,
    otp_redis: aioredis.Redis,
) -> None:
    challenge = await otp_service.request_phone_challenge(
        "+86 137 0013 7001", OtpPurpose.REGISTER, client_ip="203.0.113.7"
    )
    assert challenge.expires_at == _T0 + timedelta(minutes=5)

    assert len(otp_sms.messages) == 1
    sent = otp_sms.messages[0]
    assert sent.to == _PHONE
    code = sent.variables["code"]
    assert isinstance(code, str) and len(code) == 6 and code.isdigit()

    # At-rest check on the real instance: full metadata, no plaintext code.
    record = await otp_redis.hgetall(
        _CHALLENGE_KEY.format(challenge_id=challenge.challenge_id)
    )
    assert set(record) == {
        "phone",
        "purpose",
        "code_hash",
        "salt",
        "attempts",
        "created_at",
        "expires_at",
    }
    assert record["phone"] == _PHONE
    assert record["purpose"] == "REGISTER"
    assert code not in set(record.values())

    token = await otp_service.verify_phone_challenge(challenge.challenge_id, code)
    verified = await otp_service.verify_phone_token(token.token)
    assert verified.phone_e164 == _PHONE

    # The consumed challenge keeps its record until the backstop TTL, so a
    # replay is told "already consumed", not "unknown".
    with pytest.raises(ChallengeAlreadyConsumedError):
        await otp_service.verify_phone_challenge(challenge.challenge_id, code)


@pytest.mark.integration
async def test_concurrent_verify_of_one_challenge_consumes_exactly_once(
    otp_service: OtpChallengeService, otp_sms: FakeSmsSender
) -> None:
    challenge = await otp_service.request_phone_challenge(
        "137 0013 7001", OtpPurpose.REGISTER, client_ip="203.0.113.7"
    )
    code = otp_sms.messages[-1].variables["code"]
    assert isinstance(code, str)

    async def _verify() -> VerifiedPhoneToken | Exception:
        try:
            return await otp_service.verify_phone_challenge(
                challenge.challenge_id, code
            )
        except Exception as exc:  # noqa: BLE001 - classify gather results
            return exc

    results = await asyncio.gather(_verify(), _verify())
    successes = [r for r in results if isinstance(r, VerifiedPhoneToken)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ChallengeAlreadyConsumedError)

    # The single winner's token resolves to the verified phone.
    verified = await otp_service.verify_phone_token(successes[0].token)
    assert verified.phone_e164 == _PHONE


@pytest.mark.integration
async def test_concurrent_token_resolution_consumes_exactly_once(
    otp_service: OtpChallengeService, otp_sms: FakeSmsSender
) -> None:
    challenge = await otp_service.request_phone_challenge(
        _PHONE, OtpPurpose.REGISTER, client_ip="203.0.113.7"
    )
    code = otp_sms.messages[-1].variables["code"]
    assert isinstance(code, str)
    token = await otp_service.verify_phone_challenge(challenge.challenge_id, code)

    async def _resolve() -> VerifiedPhone | Exception:
        try:
            return await otp_service.verify_phone_token(token.token)
        except Exception as exc:  # noqa: BLE001 - classify gather results
            return exc

    results = await asyncio.gather(_resolve(), _resolve())
    successes = [r for r in results if isinstance(r, VerifiedPhone)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert successes[0].phone_e164 == _PHONE
    assert len(failures) == 1


@pytest.mark.integration
async def test_expired_challenge_rejected(
    otp_service: OtpChallengeService, otp_sms: FakeSmsSender, otp_clock: FrozenClock
) -> None:
    challenge = await otp_service.request_phone_challenge(
        _PHONE, OtpPurpose.REGISTER, client_ip="203.0.113.7"
    )
    code = otp_sms.messages[-1].variables["code"]
    assert isinstance(code, str)

    _advance(otp_clock, 301)  # past the 5-minute TTL
    with pytest.raises(ChallengeExpiredError):
        await otp_service.verify_phone_challenge(challenge.challenge_id, code)


@pytest.mark.integration
async def test_resend_cooldown_enforced_with_normalized_identity(
    otp_service: OtpChallengeService,
    otp_sms: FakeSmsSender,
    otp_clock: FrozenClock,
) -> None:
    first = await otp_service.request_phone_challenge(
        _PHONE, OtpPurpose.REGISTER, client_ip="203.0.113.7"
    )
    # Domestic formatting of the same number: the normalized cooldown key
    # must reject the resend on the real Redis instance.
    with pytest.raises(ResendCooldownError):
        await otp_service.request_phone_challenge(
            "137 0013 7001", OtpPurpose.REGISTER, client_ip="203.0.113.8"
        )
    assert len(otp_sms.messages) == 1

    _advance(otp_clock, 60)
    second = await otp_service.request_phone_challenge(
        "008613700137001", OtpPurpose.REGISTER, client_ip="203.0.113.7"
    )
    assert second.challenge_id != first.challenge_id
    assert len(otp_sms.messages) == 2
    assert {message.to for message in otp_sms.messages} == {_PHONE}
