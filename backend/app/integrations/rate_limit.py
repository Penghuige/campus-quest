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
# student-heavy task and community write actions. Login caps are
# tighter-window (brute force); send-flavored endpoints cap per hour on top
# of the OTP service's own per-phone/per-IP caps (spec §33.1/§33.2).
# Claim/abandon cap per authenticated user id: both already carry
# business-side ceilings (the §8.2 quota, the §8.5 daily abandon cap), so
# these windows are anti-hammering, not quota enforcement. The community
# buckets (plan 06 task 9) key the same way: comment create and report are
# the stricter pair — publish-immediately thread content (spec §21.1 rate
# limit) and moderation-queue flooding (spec §23) are the abuse surfaces —
# while edit/vote/reaction windows only anti-hammer toggles the services
# already keep idempotent.
# --- parallel IP-keyed caps (hardening A-1) ---------------------------------------
#
# Every per-identifier cap above is defeated by one source address
# rotating through MANY identifiers (credential stuffing): each fresh
# username/email/phone opens a fresh bucket, so the attacker's aggregate
# volume stays unlimited. The login-family endpoints therefore carry a
# SECOND, IP-keyed rule named ``<identifier-bucket>-ip`` (the pairing
# convention `routing_common._enforce_rate_limit` resolves), enforced
# next to the identifier check — either layer refusing rejects the
# request.
#
# ``IP_HOURLY_LIMIT`` mirrors the OTP service's per-IP hourly default
# (``otp_ip_hourly_request_limit`` = 50, spec §33.2): the same
# "tens per hour per address" budget the OTP side already accepts for
# shared campus NAT egress, so a login burst from one lab address lands
# in the same order as its OTP traffic instead of behind an unrelated
# stricter/looser knob. Tunable here in one place.
IP_HOURLY_LIMIT = 50
HOURLY_WINDOW_SECONDS = 3600
# The verify endpoint's per-challenge identifier layer: the OTP service
# already bounds code attempts per challenge (5 wrong codes consume it),
# so 30/hour never binds a human retrying a mistyped code — it only
# anti-hammers the HTTP surface itself.
OTP_VERIFY_CHALLENGE_HOURLY_LIMIT = 30

RATE_LIMIT_RULES: dict[str, RateLimitRule] = {
    rule.bucket: rule
    for rule in (
        RateLimitRule(bucket="auth:login", limit=10, window_seconds=300),
        RateLimitRule(
            bucket="auth:login-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        RateLimitRule(bucket="auth:staff-login", limit=10, window_seconds=300),
        RateLimitRule(
            bucket="auth:staff-login-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        # The staff email-code channel (V1.1 §2.1: 邮箱+IP 双桶): every
        # request mails a code for a staff mailbox, so the per-email cap
        # matches the OTP-send posture (5/hour) — the 60s resend cooldown
        # inside the service is the finer throttle, this is the bound.
        RateLimitRule(bucket="auth:staff-email-code", limit=5, window_seconds=3600),
        RateLimitRule(
            bucket="auth:staff-email-code-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        # The student mobile-code channel (V1.1 2026-10-16 §3): every
        # request SMSes a code for a student phone, so the caps mirror
        # the OTP-send posture; the 60s cooldown inside the service is
        # the finer throttle.
        RateLimitRule(bucket="auth:mobile-code", limit=5, window_seconds=3600),
        RateLimitRule(
            bucket="auth:mobile-code-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        # The unified challenge facade (2026-10-19 spec §3.1): the
        # per-identifier caps mirror the OTP-send posture the routed
        # services own (their internal cooldowns stay the finer
        # throttle); the totp leg is ip-keyd only — its identifier is
        # a single-use opaque token, useless as a bucket key, and the
        # 6-digit factor under online brute force is exactly the login
        # limiter's job (interfaces.md).
        RateLimitRule(bucket="auth:challenge", limit=5, window_seconds=3600),
        RateLimitRule(
            bucket="auth:challenge-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        RateLimitRule(bucket="auth:challenge-totp", limit=10, window_seconds=300),
        RateLimitRule(
            bucket="auth:challenge-totp-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        RateLimitRule(bucket="auth:register", limit=5, window_seconds=3600),
        RateLimitRule(bucket="auth:otp-send", limit=5, window_seconds=3600),
        RateLimitRule(
            bucket="auth:otp-send-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        RateLimitRule(
            bucket="auth:otp-verify",
            limit=OTP_VERIFY_CHALLENGE_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        RateLimitRule(
            bucket="auth:otp-verify-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        RateLimitRule(bucket="auth:password-reset", limit=5, window_seconds=3600),
        RateLimitRule(bucket="me:email-verify", limit=5, window_seconds=3600),
        RateLimitRule(bucket="me:phone-change", limit=5, window_seconds=3600),
        # The staff mobile-code channel (V1.1 login iteration item 1):
        # the SMS-send posture mirrored — the 60s service cooldown is
        # the finer throttle.
        RateLimitRule(bucket="auth:staff-mobile-code", limit=5, window_seconds=3600),
        RateLimitRule(
            bucket="auth:staff-mobile-code-ip",
            limit=IP_HOURLY_LIMIT,
            window_seconds=HOURLY_WINDOW_SECONDS,
        ),
        # Staff first-bind (item 1): the OTP-send posture, same as the
        # student change flow beside it.
        RateLimitRule(bucket="me:phone-bind", limit=5, window_seconds=3600),
        # The avatar write (spec amendment D2): per authenticated user
        # id — bytes land on the API, so this is the anti-abuse bound.
        RateLimitRule(bucket="me:avatar-upload", limit=1, window_seconds=600),
        RateLimitRule(bucket="tasks:claim", limit=20, window_seconds=60),
        RateLimitRule(bucket="claims:abandon", limit=10, window_seconds=60),
        # Import preview (security round F2): per staff actor. The body
        # rides the API streaming-bounded, so this is the anti-abuse
        # bound on repeated parse work, not a size control.
        RateLimitRule(bucket="tasks:import-preview", limit=10, window_seconds=60),
        # The presigned-upload grant: per authenticated user id. Each
        # intent issues a short-lived presigned URL against the object
        # store, so hammering it burns provider signing quota; the
        # business ceilings (window, size caps) live in the upload service.
        RateLimitRule(bucket="submissions:upload-intent", limit=10, window_seconds=60),
        RateLimitRule(bucket="comments:create", limit=10, window_seconds=60),
        RateLimitRule(bucket="comments:edit", limit=20, window_seconds=60),
        RateLimitRule(bucket="comments:vote", limit=30, window_seconds=60),
        RateLimitRule(bucket="comments:reaction", limit=30, window_seconds=60),
        RateLimitRule(bucket="comments:report", limit=5, window_seconds=60),
        # Staff invitation issuance (Plan 08 T9): per inviting Admin.
        # Each invitation mints a one-time capability and records an
        # audited row, so hammering the endpoint spends audit noise and
        # token material; the role/target validation lives in the staff
        # service.
        RateLimitRule(bucket="staff:invitations", limit=20, window_seconds=3600),
        # Talent contact reveal (innovation batch 1): per authenticated
        # viewer. Each grant reads another student's chosen contact value,
        # so this is the privacy anti-abuse bound; the authorization and
        # audit live in the talent service, not here.
        RateLimitRule(bucket="ie:talent-contacts", limit=30, window_seconds=60),
        RateLimitRule(bucket="ie:recruitment-contacts", limit=30, window_seconds=60),
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


# --- e2e battery relief (CQ_E2E_LOGIN_RATELIMIT; opt-in per deployment) ------

#: The login family the relief raises: both doors' identifier and IP
#: buckets, including the email-code channel (the battery logs in staff
#: per test; retry loops fed the IP bucket past 50/h — structural, not
#: flaky). Anything NOT in this tuple is untouched by the relief.
LOGIN_FAMILY_BUCKETS: tuple[str, ...] = (
    "auth:login",
    "auth:login-ip",
    "auth:staff-login",
    "auth:staff-login-ip",
    "auth:staff-email-code",
    "auth:staff-email-code-ip",
    "auth:challenge",
    "auth:challenge-ip",
    "auth:challenge-totp",
    "auth:challenge-totp-ip",
)
_E2E_LOGIN_LIMIT = 1000
_E2E_LOGIN_WINDOW_SECONDS = 3600
_DEFAULT_LOGIN_RULES: dict[str, RateLimitRule] = {
    bucket: RATE_LIMIT_RULES[bucket] for bucket in LOGIN_FAMILY_BUCKETS
}


def apply_e2e_login_relief() -> None:
    """Raise the login family to battery scale (idempotent).

    Applied once at app assembly when Settings.e2e_login_ratelimit is
    on — the local e2e orchestration stack only. Production and every
    default deployment never call this: their limits stay
    byte-identical (the default-shape pin lives in
    tests/unit/integrations/test_e2e_login_relief.py).
    """
    for bucket in LOGIN_FAMILY_BUCKETS:
        RATE_LIMIT_RULES[bucket] = RateLimitRule(
            bucket=bucket,
            limit=_E2E_LOGIN_LIMIT,
            window_seconds=_E2E_LOGIN_WINDOW_SECONDS,
        )


def restore_default_login_rules() -> None:
    """Return the login family to its shipped defaults (test hygiene)."""
    RATE_LIMIT_RULES.update(_DEFAULT_LOGIN_RULES)
