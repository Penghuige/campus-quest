# backend/tests/unit/identity/test_otp.py
"""Unit tests for the phone OTP challenge lifecycle (spec §5.4, §33.2).

All time-dependent behavior (challenge TTL, resend cooldown, verified-token
TTL) is asserted with FrozenClock: the service enforces business time
client-side against the injected Clock (docs/quality/backend-engineering.md
§11) and treats the Redis key TTL purely as a cleanup backstop, so no test
needs to sleep. FrozenClock is a frozen dataclass, so advancing it uses
`object.__setattr__` on the single instance the service already holds.

Redis-fake choice (controller decision): a minimal in-memory async fake of
the exact command subset the service uses, instead of the `fakeredis`
package. Reasons:

1. No time-based key expiry has to be emulated at all, because expiry and
   cooldown are Clock-driven client-side checks on stored timestamps.
2. The one hard atomicity invariant (single consume) rests on two atomic
   server primitives — HSETNX for the challenge claim, GETDEL for the
   verified token — which the fake mirrors exactly; every command is
   synchronous code with no inner awaits, so commands cannot interleave
   under asyncio. True cross-connection atomicity is proven against real
   Redis in tests/integration/identity/test_otp_flow.py.
3. Zero new dependencies: `fakeredis` would add a dev dependency (plus its
   Lua engine if scripting were used) while still being a Python
   reimplementation whose fidelity for this command subset is no better
   than the fake below.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.clock import FrozenClock
from app.modules.identity.otp import (
    ChallengeAlreadyConsumedError,
    ChallengeExpiredError,
    ChallengePublic,
    InvalidPhoneError,
    InvalidTokenError,
    OtpChallengeService,
    OtpPolicy,
    OtpPurpose,
    OtpRateLimitError,
    ResendCooldownError,
    TooManyAttemptsError,
    UnknownChallengeError,
    VerifiedPhoneToken,
    WrongCodeError,
)
from tests.fakes.integrations import FakeSmsSender

_T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_E164 = "+8613800138000"
_PHONE_VARIANTS = [
    "+86 138 0013 8000",
    "008613800138000",
    "138 0013 8000",
    "+8613800138000",
    "13800138000",
]
_SIX_DIGITS = re.compile(r"\A[0-9]{6}\Z")
_MASKED_E164 = "+86****8000"


class InMemoryRedis:
    """Minimal async Redis fake for the OTP command subset.

    Mirrors real-command semantics (HSETNX only writes an absent hash field,
    creating the hash if needed; GETDEL returns and deletes atomically). TTLs
    are recorded but never enforced: the service owns expiry via the Clock,
    and the fake must not invent time behavior real Redis only approximates
    lazily anyway.
    """

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        fields = self.hashes.setdefault(key, {})
        added = sum(1 for field in mapping if field not in fields)
        fields.update(mapping)
        return added

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hsetnx(self, key: str, field: str, value: str) -> int:
        fields = self.hashes.setdefault(key, {})
        if field in fields:
            return 0
        fields[field] = value
        return 1

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        fields = self.hashes.setdefault(key, {})
        current = int(fields.get(field, "0")) + amount
        fields[field] = str(current)
        return current

    async def hexists(self, key: str, field: str) -> bool:
        return field in self.hashes.get(key, {})

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.strings or key in self.hashes:
                deleted += 1
            self.strings.pop(key, None)
            self.hashes.pop(key, None)
            self.ttls.pop(key, None)
        return deleted

    async def expire(self, key: str, seconds: int) -> bool:
        exists = key in self.strings or key in self.hashes
        if exists:
            self.ttls[key] = seconds
        return exists

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def incr(self, key: str) -> int:
        current = int(self.strings.get(key, "0")) + 1
        self.strings[key] = str(current)
        return current

    def stored_values(self) -> list[str]:
        """Every stored string value and hash field value, for leak tests."""
        values = list(self.strings.values())
        for fields in self.hashes.values():
            values.extend(fields.values())
        return values


def _advance(clock: FrozenClock, seconds: float) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(seconds=seconds))


def _policy(**overrides: int | str) -> OtpPolicy:
    values: dict[str, int | str] = {
        "ttl_seconds": 300,
        "max_verify_attempts": 5,
        "resend_cooldown_seconds": 60,
        "verified_token_ttl_seconds": 600,
        "phone_hourly_request_limit": 5,
        "phone_daily_request_limit": 20,
        "ip_hourly_request_limit": 50,
        "ip_daily_request_limit": 200,
        "hmac_secret": "unit-test-hmac-secret",
        "default_region": "CN",
    }
    values.update(overrides)
    return OtpPolicy(**values)  # type: ignore[arg-type]


def _make(
    clock: FrozenClock, policy: OtpPolicy | None = None
) -> tuple[OtpChallengeService, FakeSmsSender, InMemoryRedis]:
    sms = FakeSmsSender()
    redis_fake = InMemoryRedis()
    service = OtpChallengeService(
        redis=redis_fake,  # type: ignore[arg-type]
        clock=clock,
        sms_sender=sms,
        policy=policy if policy is not None else _policy(),
    )
    return service, sms, redis_fake


async def _request(
    service: OtpChallengeService,
    raw_phone: str = _E164,
    purpose: OtpPurpose = OtpPurpose.REGISTER,
    client_ip: str = "203.0.113.7",
) -> ChallengePublic:
    return await service.request_phone_challenge(
        raw_phone, purpose, client_ip=client_ip
    )


def _delivered_code(sms: FakeSmsSender) -> str:
    assert sms.messages, "no SMS was delivered"
    message = sms.messages[-1]
    code = message.variables["code"]
    assert isinstance(code, str)
    assert _SIX_DIGITS.fullmatch(code), f"code must be 6 numeric digits: {code!r}"
    return code


async def test_request_returns_public_challenge_with_five_minute_ttl():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)

    challenge = await _request(service)

    assert isinstance(challenge.challenge_id, UUID)
    assert challenge.expires_at == _T0 + timedelta(minutes=5)
    # ChallengePublic exposes exactly the public pair — the code is not a
    # field, so it can never leak through the return value (spec §33.2).
    assert [field.name for field in dataclasses.fields(challenge)] == [
        "challenge_id",
        "expires_at",
    ]
    # Delivery goes through the SmsSender port to the normalized E.164.
    assert len(sms.messages) == 1
    assert sms.messages[0].to == _E164
    assert _SIX_DIGITS.fullmatch(sms.messages[0].variables["code"])


async def test_verify_returns_token_resolving_to_normalized_phone():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service, "138 0013 8000")
    code = _delivered_code(sms)

    token = await service.verify_phone_challenge(challenge.challenge_id, code)
    assert isinstance(token, VerifiedPhoneToken)
    assert token.expires_at == _T0 + timedelta(seconds=600)

    verified = await service.verify_phone_token(token.token)
    assert verified.phone_e164 == _E164


async def test_challenge_and_token_are_each_consumable_once():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service)
    code = _delivered_code(sms)

    token = await service.verify_phone_challenge(challenge.challenge_id, code)
    with pytest.raises(ChallengeAlreadyConsumedError):
        await service.verify_phone_challenge(challenge.challenge_id, code)

    assert (await service.verify_phone_token(token.token)).phone_e164 == _E164
    with pytest.raises(InvalidTokenError):
        await service.verify_phone_token(token.token)


async def test_sixth_attempt_rejected_after_five_failures():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service)
    code = _delivered_code(sms)

    for expected_attempts in range(1, 6):
        with pytest.raises(WrongCodeError) as exc_info:
            await service.verify_phone_challenge(challenge.challenge_id, "000000")
        assert exc_info.value.attempts_used == expected_attempts

    # The 6th attempt is rejected even when the code is finally correct:
    # after 5 failures the challenge is locked until it expires (§33.2).
    with pytest.raises(TooManyAttemptsError):
        await service.verify_phone_challenge(challenge.challenge_id, code)
    assert len(sms.messages) == 1  # lockout never triggers a re-send


async def test_expired_challenge_fails():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service)
    code = _delivered_code(sms)

    _advance(clock, 299)  # one second before expiry: still consumable
    token = await service.verify_phone_challenge(challenge.challenge_id, code)
    assert token.expires_at > clock.now()

    challenge = await _request(service)
    code = _delivered_code(sms)
    _advance(clock, 300 + 299 + 2)  # past this challenge's 5-minute TTL
    with pytest.raises(ChallengeExpiredError):
        await service.verify_phone_challenge(challenge.challenge_id, code)


async def test_resend_inside_cooldown_rejected_then_allowed_after_cooldown():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    first = await _request(service)

    with pytest.raises(ResendCooldownError):
        await _request(service)
    # Cooldown is per normalized phone, across purposes and raw formats.
    with pytest.raises(ResendCooldownError):
        await _request(service, "+86 138 0013 8000", OtpPurpose.PASSWORD_RESET)
    assert len(sms.messages) == 1
    assert first.expires_at == _T0 + timedelta(minutes=5)

    _advance(clock, 60)
    second = await _request(service, "138 0013 8000")
    assert second.challenge_id != first.challenge_id
    assert len(sms.messages) == 2
    assert second.expires_at == _T0 + timedelta(seconds=60 + 300)


async def test_formatting_variants_resolve_to_one_normalized_identity():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)

    challenge = await _request(service, _PHONE_VARIANTS[0])
    # Every other raw variant hits the same normalized-phone cooldown key...
    for variant in _PHONE_VARIANTS[1:]:
        with pytest.raises(ResendCooldownError):
            await _request(service, variant)
    _advance(clock, 60)
    # ...and a challenge created from domestic formatting still verifies to
    # the canonical E.164 value (spec §5.4).
    challenge = await _request(service, "13800138000")
    code = _delivered_code(sms)
    token = await service.verify_phone_challenge(challenge.challenge_id, code)
    assert (await service.verify_phone_token(token.token)).phone_e164 == _E164
    assert {message.to for message in sms.messages} == {_E164}


async def test_invalid_phone_rejected_without_sending():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)

    for raw in ["not-a-phone", "+861234", ""]:
        with pytest.raises(InvalidPhoneError):
            await _request(service, raw)
    assert sms.messages == []


async def test_per_phone_hourly_cap_enforced():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(
        clock, _policy(phone_hourly_request_limit=2, resend_cooldown_seconds=0)
    )

    await _request(service)
    await _request(service)
    with pytest.raises(OtpRateLimitError) as exc_info:
        await _request(service)
    assert exc_info.value.scope == "phone_hourly"
    assert len(sms.messages) == 2

    # The cap is per phone: a different number is unaffected.
    await _request(service, "+8613800138001")
    assert len(sms.messages) == 3


async def test_per_phone_daily_cap_enforced():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(
        clock,
        _policy(
            phone_hourly_request_limit=100,
            phone_daily_request_limit=2,
            resend_cooldown_seconds=0,
        ),
    )

    await _request(service)
    await _request(service)
    with pytest.raises(OtpRateLimitError) as exc_info:
        await _request(service)
    assert exc_info.value.scope == "phone_daily"


async def test_per_ip_hourly_cap_enforced():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(
        clock, _policy(ip_hourly_request_limit=2, resend_cooldown_seconds=0)
    )

    # Two distinct phones so the per-phone caps cannot fire first.
    await _request(service, "+8613800138000", client_ip="198.51.100.4")
    await _request(service, "+8613800138001", client_ip="198.51.100.4")
    with pytest.raises(OtpRateLimitError) as exc_info:
        await _request(service, "+8613800138002", client_ip="198.51.100.4")
    assert exc_info.value.scope == "ip_hourly"

    # The cap is per IP: another origin is unaffected (and the capped
    # request above never sent — 3 sends, not 4).
    await _request(service, "+8613800138003", client_ip="198.51.100.5")
    assert len(sms.messages) == 3


async def test_redis_stores_hashed_code_with_full_metadata_never_plaintext():
    clock = FrozenClock(_T0)
    service, sms, redis_fake = _make(clock)
    challenge = await _request(service, "138 0013 8000")
    code = _delivered_code(sms)

    record = await redis_fake.hgetall(f"otp:challenge:{challenge.challenge_id}")
    assert set(record) == {
        "phone",
        "purpose",
        "code_hash",
        "salt",
        "attempts",
        "created_at",
        "expires_at",
    }
    assert record["phone"] == _E164
    assert record["purpose"] == "REGISTER"
    assert record["attempts"] == "0"
    # SHA-256 HMAC hex digest, not the code and not a reversible encoding.
    assert record["code_hash"] != code
    assert len(record["code_hash"]) == 64
    # Exact-match scan over every stored value: the plaintext code exists
    # only in the send call and test memory (spec §33.2).
    assert code not in redis_fake.stored_values()


async def test_otp_code_and_raw_phone_never_appear_in_logs(caplog):
    # Level first: caplog must capture INFO records while the calls run.
    caplog.set_level(logging.INFO)
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service, "138 0013 8000")
    code = _delivered_code(sms)

    with pytest.raises(WrongCodeError):
        await service.verify_phone_challenge(challenge.challenge_id, "000000")
    token = await service.verify_phone_challenge(challenge.challenge_id, code)
    await service.verify_phone_token(token.token)

    text = caplog.text
    assert text, "expected OTP log records to assert against"
    assert code not in text
    # Phone logging is masked (spec §5.4): neither the full E.164 form nor
    # the domestic raw form may appear, but the masked form does.
    assert _E164 not in text
    assert "13800138000" not in text
    assert _MASKED_E164 in text


async def test_concurrent_verify_consumes_exactly_once():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service)
    code = _delivered_code(sms)

    results = await asyncio.gather(
        service.verify_phone_challenge(challenge.challenge_id, code),
        service.verify_phone_challenge(challenge.challenge_id, code),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, VerifiedPhoneToken)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ChallengeAlreadyConsumedError)


async def test_verified_token_expires():
    clock = FrozenClock(_T0)
    service, sms, _ = _make(clock)
    challenge = await _request(service)
    code = _delivered_code(sms)
    token = await service.verify_phone_challenge(challenge.challenge_id, code)

    _advance(clock, 599)  # one second before token expiry: still valid
    assert (await service.verify_phone_token(token.token)).phone_e164 == _E164
    _advance(clock, 2)  # now past the token's own TTL
    with pytest.raises(InvalidTokenError):
        await service.verify_phone_token(token.token)


async def test_unknown_challenge_rejected():
    clock = FrozenClock(_T0)
    service, _, _ = _make(clock)

    with pytest.raises(UnknownChallengeError):
        await service.verify_phone_challenge(uuid4(), "123456")


async def test_policy_from_settings_maps_every_field():
    from app.core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://t:t@localhost:5432/t",
        redis_url="redis://localhost:6379/0",
        s3_endpoint_url="http://localhost:9000",
        s3_bucket="b",
        s3_access_key="a",
        s3_secret_key="s",
        business_timezone="Asia/Shanghai",
    )
    policy = OtpPolicy.from_settings(settings)
    assert policy == _policy(hmac_secret="dev-only-insecure-otp-hmac-secret")
