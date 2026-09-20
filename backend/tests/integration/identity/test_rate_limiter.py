# backend/tests/integration/identity/test_rate_limiter.py
"""Redis fixed-window rate limiter against real Redis (spec §33.1).

The limiter adapter backs the identity API's endpoint-level request caps
(login/register/OTP-send/email-verify/phone-change/password-reset; Plan 02
Task 9). Redis runs on the dedicated test database 14, flushed around every
test; the window boundary is crossed by advancing the injected FrozenClock,
never by sleeping (backend-engineering §11).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.integrations.masking import mask_email, mask_phone, mask_student_number
from app.integrations.rate_limit import (
    RateLimitExceededError,
    RedisFixedWindowLimiter,
)

_RATE_LIMIT_TEST_REDIS_DB = 14
_T0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
_PHONE = "+8613700137001"
_EMAIL = "user@pku.edu.cn"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"Rate-limit integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_RATE_LIMIT_TEST_REDIS_DB}"))


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


@pytest_asyncio.fixture
async def limiter_redis() -> AsyncIterator[aioredis.Redis]:
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


@pytest.mark.integration
async def test_allows_until_limit_then_raises(
    limiter_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    limiter = RedisFixedWindowLimiter(redis=limiter_redis, clock=clock)

    for _ in range(3):
        await limiter.check(
            bucket="auth:login", identifier="20250010001", limit=3, window_seconds=300
        )

    with pytest.raises(RateLimitExceededError) as exc_info:
        await limiter.check(
            bucket="auth:login", identifier="20250010001", limit=3, window_seconds=300
        )
    assert exc_info.value.bucket == "auth:login"


@pytest.mark.integration
async def test_identifiers_and_buckets_are_independent(
    limiter_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    # Two usernames under one bucket, one username under another: only the
    # exhausted (bucket, identifier) pair is refused.
    limiter = RedisFixedWindowLimiter(redis=limiter_redis, clock=clock)
    for _ in range(2):
        await limiter.check(
            bucket="auth:login", identifier="alice", limit=2, window_seconds=300
        )
    with pytest.raises(RateLimitExceededError):
        await limiter.check(
            bucket="auth:login", identifier="alice", limit=2, window_seconds=300
        )

    await limiter.check(
        bucket="auth:login", identifier="bob", limit=2, window_seconds=300
    )
    await limiter.check(
        bucket="auth:register", identifier="alice", limit=2, window_seconds=300
    )


@pytest.mark.integration
async def test_window_resets_when_the_clock_crosses_the_boundary(
    limiter_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    # Fixed window keyed by floor(now / window): advancing past the window
    # starts a fresh counter for the same identifier.
    limiter = RedisFixedWindowLimiter(redis=limiter_redis, clock=clock)
    for _ in range(2):
        await limiter.check(
            bucket="auth:otp-send",
            identifier="+8613700137001",
            limit=2,
            window_seconds=300,
        )
    with pytest.raises(RateLimitExceededError):
        await limiter.check(
            bucket="auth:otp-send",
            identifier="+8613700137001",
            limit=2,
            window_seconds=300,
        )

    _advance(clock, seconds=301)

    await limiter.check(
        bucket="auth:otp-send", identifier=_PHONE, limit=2, window_seconds=300
    )


@pytest.mark.integration
async def test_exceeded_log_masks_contact_identifiers(
    limiter_redis: aioredis.Redis, clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Spec §5.4/§40: contact info in logs is masked. The 429 branch logs the
    # rate-limit identifier, which for otp-send/phone-change is a full E.164
    # phone, for email-verify a full email, and for login/register/forgot a
    # full STUDENT NUMBER (PR review fix: student numbers are log-sensitive —
    # they are the students' login usernames, spec §5.2) — the log must carry
    # only the shared masked forms.
    limiter = RedisFixedWindowLimiter(redis=limiter_redis, clock=clock)
    student_number = "20250010001"

    with caplog.at_level(logging.INFO, logger="app.integrations.rate_limit"):
        for bucket, identifier in (
            ("auth:otp-send", _PHONE),
            ("me:email-verify", _EMAIL),
            ("auth:login", student_number),
        ):
            await limiter.check(
                bucket=bucket, identifier=identifier, limit=1, window_seconds=300
            )
            with contextlib.suppress(RateLimitExceededError):
                await limiter.check(
                    bucket=bucket, identifier=identifier, limit=1, window_seconds=300
                )

    messages = [record.getMessage() for record in caplog.records]
    assert any(mask_phone(_PHONE) in message for message in messages)
    assert any(mask_email(_EMAIL) in message for message in messages)
    assert any(mask_student_number(student_number) in message for message in messages)
    assert not any(_PHONE in message for message in messages)
    assert not any(_EMAIL in message for message in messages)
    assert not any(student_number in message for message in messages)
