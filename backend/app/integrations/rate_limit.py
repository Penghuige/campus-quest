# backend/app/integrations/rate_limit.py
"""Endpoint-level request-rate limiting port (spec §33.1 API rate limit).

Identity endpoints cap requests per normalized identifier (login username,
registration student number, E.164 phone, normalized email) on top of the
OTP lifecycle's own caps (`app.modules.identity.otp`). The port follows the
adapter discipline of the other integration ports: domain/API code depends
on the `RateLimiter` Protocol, the Redis implementation lives here, and
tests inject the deterministic fake from `tests/fakes/integrations.py`.

Design decisions:

- **Fixed window, clock-driven.** The window index is
  ``floor(clock.now() / window_seconds)`` — the injected business Clock
  (backend-engineering §11), never database or Redis time — and it keys the
  counter, so crossing a window boundary starts a fresh counter without
  waiting for key expiry. The Redis key TTL is a cleanup backstop set to
  twice the window, re-armed with ``EXPIRE NX`` on every hit (the
  crash-between-INCR-and-EXPIRE discipline of `otp._require_within_caps`).
- **Exceeding raises `RateLimitExceededError`** with the bucket name; the
  identity router's exception handler renders the frozen §29 envelope code
  ``RATE_LIMITED`` with HTTP 429 (docs/architecture/interfaces.md).
- **Rules are declared once** in `RATE_LIMIT_RULES` and looked up by bucket
  name, so endpoints and tests share one definition of every cap.

Limits are per-process-deployment defaults; they protect
against credential stuffing and send-spam, not a dedicated abuse pipeline
(§33.1 calls for API rate limiting; a global gateway limiter can layer on
top without touching this contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as aioredis

from app.core.clock import Clock
from app.integrations.masking import mask_email, mask_phone, mask_student_number

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ratelimit:"
# Redis backstop TTL = 2x window: long enough that a counter can never
# expire inside its own live window, short enough to garbage-collect.
_TTL_WINDOW_MULTIPLIER = 2


class RateLimitExceededError(Exception):
    """The (bucket, identifier) fixed window is exhausted; `bucket` names it."""

    def __init__(self, bucket: str) -> None:
        super().__init__("请求过于频繁，请稍后再试")
        self.bucket = bucket


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """One endpoint cap: ``limit`` requests per ``window_seconds``."""

    bucket: str
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")


# One rule per rate-limited endpoint: identity endpoints plus the
# student-heavy task actions. Login caps are tighter-window
# (brute force); send-flavored endpoints cap per hour on top of the OTP
# service's own per-phone/per-IP caps (spec §33.1/§33.2). Claim/abandon cap
# per authenticated user id: both already carry business-side ceilings (the
# §8.2 quota, the §8.5 daily abandon cap), so these windows are
# anti-hammering, not quota enforcement.
RATE_LIMIT_RULES: dict[str, RateLimitRule] = {
    rule.bucket: rule
    for rule in (
        RateLimitRule(bucket="auth:login", limit=10, window_seconds=300),
        RateLimitRule(bucket="auth:staff-login", limit=10, window_seconds=300),
        RateLimitRule(bucket="auth:register", limit=5, window_seconds=3600),
        RateLimitRule(bucket="auth:otp-send", limit=5, window_seconds=3600),
        RateLimitRule(bucket="auth:password-reset", limit=5, window_seconds=3600),
        RateLimitRule(bucket="me:email-verify", limit=5, window_seconds=3600),
        RateLimitRule(bucket="me:phone-change", limit=5, window_seconds=3600),
        RateLimitRule(bucket="tasks:claim", limit=20, window_seconds=60),
        RateLimitRule(bucket="claims:abandon", limit=10, window_seconds=60),
    )
}


class RateLimiter(Protocol):
    """Enforce a fixed-window cap on ``(bucket, identifier)``."""

    async def check(
        self, *, bucket: str, identifier: str, limit: int, window_seconds: int
    ) -> None:
        """Count one request; raise `RateLimitExceededError` past the cap.

        Rejected requests still count (the request itself is the capped
        resource), matching the OTP request-counter discipline.
        """
        ...


@dataclass(frozen=True, slots=True)
class RedisFixedWindowLimiter:
    """Fixed-window limiter on Redis ``INCR`` counters (spec §33.1).

    The client may be constructed with or without ``decode_responses=True``
    (the counter comparison goes through ``int()``).
    """

    redis: aioredis.Redis
    clock: Clock

    async def check(
        self, *, bucket: str, identifier: str, limit: int, window_seconds: int
    ) -> None:
        window_index = int(self.clock.now().timestamp()) // window_seconds
        key = f"{_KEY_PREFIX}{bucket}:{identifier}:{window_index}"
        count = int(await self.redis.incr(key))
        # EXPIRE ... NX on every hit (see module docstring): the window still
        # starts at the first counted request, but a crash between INCR and
        # EXPIRE cannot strand a TTL-less counter that throttles forever.
        await self.redis.expire(key, window_seconds * _TTL_WINDOW_MULTIPLIER, nx=True)
        if count > limit:
            logger.info(
                "rate limit exceeded bucket=%s identifier=%s count=%d limit=%d",
                bucket,
                _masked_identifier(identifier),
                count,
                limit,
            )
            raise RateLimitExceededError(bucket)


def _masked_identifier(identifier: str) -> str:
    """Mask an identifier when it is contact data or a student number.

    OTP-send/phone-change identifiers are E.164 phones, email-verify and
    staff-login identifiers are emails — both masked with the shared
    adapter forms. Digit-only identifiers (login usernames, registration
    and password-reset student numbers) take the student-number mask:
    student numbers ARE the students' login usernames (spec §5.2), so they
    are log-sensitive even though they are not contact data. Other
    identifiers (non-digit usernames, staff emails are already covered
    above) stay exact.
    """
    if identifier.startswith("+"):
        return mask_phone(identifier)
    if "@" in identifier:
        return mask_email(identifier)
    if identifier.isdigit():
        return mask_student_number(identifier)
    return identifier
