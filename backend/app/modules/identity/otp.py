# backend/app/modules/identity/otp.py
"""Phone OTP challenge lifecycle (spec §5.4, §33.2).

`OtpChallengeService` is the real implementation behind the
`PhoneVerificationPort` seam plus the two challenge-lifecycle operations
``request_phone_challenge`` and ``verify_phone_challenge``. All
dependencies (Redis client, Clock,
SmsSender, policy) are constructor-injected — no global state.

Design decisions:

- **Business time is the injected Clock** (backend-engineering §11).
  Expiry, resend cooldown, and verified-token lifetime are enforced
  client-side by comparing stored unix timestamps against ``clock.now()``.
  Redis key TTLs are cleanup backstops only, always set to the business TTL
  plus a grace period, so a record the Clock already rejected still answers
  with the precise error (EXPIRED / CONSUMED / TOO MANY ATTEMPTS) instead
  of silently vanishing, and a live record is never deleted by the backstop
  first. This is also what makes the whole lifecycle testable with
  ``FrozenClock`` without sleeping.
- **Codes are never stored or logged in plaintext** (spec §33.2). Redis
  keeps ``HMAC-SHA256(hmac_secret, "{salt}:{code}")`` with a fresh random
  salt per challenge; verification recomputes the HMAC over the submitted
  code and compares digests with ``hmac.compare_digest`` (constant time).
  The plaintext code exists only in the ``SmsSender.send`` call and the
  recipient's handset. Phone numbers are masked in every log line
  (spec §5.4).
- **Single-use consumption rests on single atomic Redis primitives**, not
  Lua scripting or WATCH/MULTI retries:
  - the challenge claim is one ``HSETNX consumed_at`` — server-side, either
    exactly one concurrent verify wins the field or the others see 0;
  - the verified-token consume is one ``GETDEL`` — the token hash key is
    returned to exactly one caller.
  One compensation case exists: if the challenge key's backstop TTL fires
  between the state read and the claim, ``HSETNX`` would recreate a stub
  hash, so a successful claim re-checks ``HEXISTS code_hash`` and deletes
  the stub, reporting EXPIRED. The grace period makes this window
  effectively unreachable (defense in depth, not a hot path).
- **Rate limiting is on the request side** (spec §33.2: per-phone and
  per-IP, hourly and daily) using fixed-window ``INCR`` counters whose TTL
  starts at the first hit. Requests rejected by the cap still count: the
  request itself is the capped resource. Cooldown is checked before the
  counters, so a cooldown rejection never burns a cap, and the cooldown key
  is written before the SMS send, so a failed send still throttles resends.
- Redis replies are exchanged as ``str``; the service decodes ``bytes``
  replies too, so production wiring (the identity routes, mirroring
  `app.core.readiness`) may construct the client with or without
  ``decode_responses=True``.

Error taxonomy: module-level exceptions, not `BusinessError` — the
§29/`error_codes.py` registry is frozen and gains codes doc-first
(interfaces.md); the router maps each exception to an
envelope response. Callers can already branch precisely today.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import phonenumbers
import redis.asyncio as aioredis

from app.modules.identity.ports import VerifiedPhone

if TYPE_CHECKING:
    from app.core.clock import Clock
    from app.core.config import Settings
    from app.integrations.sms import SmsSender

logger = logging.getLogger(__name__)

_CODE_DIGITS = 6
_CHALLENGE_KEY_PREFIX = "otp:challenge:"
_COOLDOWN_KEY_PREFIX = "otp:cooldown:"
_TOKEN_KEY_PREFIX = "otp:token:"
_RATE_KEY_PREFIX = "otp:rate:"
# Salt reused as the HMAC domain separator for verified-token keys so the
# token hash and the code hash cannot collide across purposes.
_TOKEN_HASH_SALT = "verified-token"
_SMS_TEMPLATE = "phone_otp_verification"
_HOUR_SECONDS = 3600
_DAY_SECONDS = 86400
# Redis backstop TTL = business TTL + grace (see module docstring).
_REDIS_TTL_GRACE_SECONDS = 600


class OtpPurpose(StrEnum):
    """Why a phone challenge exists; recorded on the challenge and token."""

    REGISTER = "REGISTER"
    PHONE_CHANGE = "PHONE_CHANGE"
    PASSWORD_RESET = "PASSWORD_RESET"


@dataclass(frozen=True, slots=True)
class ChallengePublic:
    """Request-side view of a challenge: never the code (spec §33.2)."""

    challenge_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedPhoneToken:
    """Single-use proof returned by a successful code verification.

    Carries the opaque token and its expiry; the phone itself is only
    resolvable through ``verify_phone_token`` (the port in ``ports.py``),
    so a caller that never consumes the token never learns the number.
    """

    token: str
    expires_at: datetime


class OtpError(Exception):
    """Base for every phone-OTP lifecycle failure."""


class InvalidPhoneError(OtpError):
    def __init__(self) -> None:
        super().__init__("手机号格式无效")


class ResendCooldownError(OtpError):
    def __init__(self) -> None:
        super().__init__("验证码发送过于频繁，请稍后再试")


class OtpRateLimitError(OtpError):
    """A request cap was exceeded; ``scope`` names which one."""

    def __init__(self, scope: str) -> None:
        super().__init__("验证码请求次数超限，请稍后再试")
        self.scope = scope


class UnknownChallengeError(OtpError):
    def __init__(self) -> None:
        super().__init__("验证码挑战不存在或已失效")


class ChallengeExpiredError(OtpError):
    def __init__(self) -> None:
        super().__init__("验证码已过期，请重新获取")


class TooManyAttemptsError(OtpError):
    def __init__(self) -> None:
        super().__init__("验证码错误次数过多，请重新获取")


class WrongCodeError(OtpError):
    """Wrong code submitted; ``attempts_used`` counts failures so far."""

    def __init__(self, attempts_used: int, max_attempts: int) -> None:
        super().__init__("验证码错误")
        self.attempts_used = attempts_used
        self.max_attempts = max_attempts


class ChallengeAlreadyConsumedError(OtpError):
    def __init__(self) -> None:
        super().__init__("验证码已被使用")


class InvalidTokenError(OtpError):
    def __init__(self) -> None:
        super().__init__("手机验证凭据无效或已过期")


@dataclass(frozen=True, slots=True)
class OtpPolicy:
    """Every OTP knob in one injectable value (spec §33.2 defaults).

    ``OtpPolicy.from_settings`` maps the typed deployment settings; unit
    tests construct policies directly so no environment is needed. Kept in
    the module (not `enums.py`) because the frozen interfaces.md registry
    does not yet list OTP contracts; promote it when a second consumer
    needs the type.
    """

    ttl_seconds: int
    max_verify_attempts: int
    resend_cooldown_seconds: int
    verified_token_ttl_seconds: int
    phone_hourly_request_limit: int
    phone_daily_request_limit: int
    ip_hourly_request_limit: int
    ip_daily_request_limit: int
    hmac_secret: str
    default_region: str = "CN"

    def __post_init__(self) -> None:
        if self.ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        if self.max_verify_attempts < 1:
            raise ValueError("max_verify_attempts must be >= 1")
        if self.resend_cooldown_seconds < 0:
            raise ValueError("resend_cooldown_seconds must be >= 0")
        if self.verified_token_ttl_seconds < 1:
            raise ValueError("verified_token_ttl_seconds must be >= 1")
        for name in (
            "phone_hourly_request_limit",
            "phone_daily_request_limit",
            "ip_hourly_request_limit",
            "ip_daily_request_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if not self.hmac_secret:
            raise ValueError("hmac_secret must be a non-empty string")
        # Fail fast on an unusable region instead of rejecting every phone
        # at request time with a confusing parse error.
        try:
            phonenumbers.country_code_for_valid_region(self.default_region)
        except Exception as exc:
            raise ValueError(
                f"default_region {self.default_region!r} is not a valid "
                "phonenumbers region code"
            ) from exc

    @classmethod
    def from_settings(cls, settings: Settings) -> OtpPolicy:
        return cls(
            ttl_seconds=settings.otp_ttl_seconds,
            max_verify_attempts=settings.otp_max_verify_attempts,
            resend_cooldown_seconds=settings.otp_resend_cooldown_seconds,
            verified_token_ttl_seconds=settings.otp_verified_token_ttl_seconds,
            phone_hourly_request_limit=settings.otp_phone_hourly_request_limit,
            phone_daily_request_limit=settings.otp_phone_daily_request_limit,
            ip_hourly_request_limit=settings.otp_ip_hourly_request_limit,
            ip_daily_request_limit=settings.otp_ip_daily_request_limit,
            hmac_secret=settings.otp_hmac_secret,
            default_region=settings.phone_default_region,
        )


class OtpChallengeService:
    """Redis-backed phone OTP lifecycle; implements ``PhoneVerificationPort``."""

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        clock: Clock,
        sms_sender: SmsSender,
        policy: OtpPolicy,
    ) -> None:
        self._redis = redis
        self._clock = clock
        self._sms = sms_sender
        self._policy = policy

    async def request_phone_challenge(
        self, raw_phone: str, purpose: OtpPurpose, *, client_ip: str
    ) -> ChallengePublic:
        """Create one challenge and send its code (spec §5.4, §33.2).

        Ordering is load-bearing: normalization, then cooldown, then the
        rate counters (a cooldown rejection must not burn a cap), then the
        challenge record and cooldown key, and the SMS send last — a failed
        send leaves the cooldown set, which fails closed against resend
        abuse. The generated code never appears in the return value, the
        Redis record, or any log line.
        """
        phone = normalize_phone(raw_phone, self._policy.default_region)
        now = self._clock.now()
        now_ts = now.timestamp()

        await self._require_not_in_cooldown(phone, now_ts)
        await self._require_within_caps(phone, client_ip)

        code = f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"
        salt = secrets.token_hex(16)
        challenge_id = uuid.uuid4()
        expires_at = now + timedelta(seconds=self._policy.ttl_seconds)
        challenge_key = _challenge_key(challenge_id)
        await self._redis.hset(
            challenge_key,
            mapping={
                "phone": phone,
                "purpose": purpose.value,
                "code_hash": _hmac_hex(self._policy.hmac_secret, salt, code),
                "salt": salt,
                "attempts": "0",
                "created_at": str(now_ts),
                "expires_at": str(expires_at.timestamp()),
            },
        )
        await self._redis.expire(
            challenge_key, self._policy.ttl_seconds + _REDIS_TTL_GRACE_SECONDS
        )
        cooldown = self._policy.resend_cooldown_seconds
        await self._redis.set(
            _cooldown_key(phone),
            str(now_ts + cooldown),
            ex=max(1, cooldown + _REDIS_TTL_GRACE_SECONDS),
        )
        self._sms.send(
            to=phone,
            template=_SMS_TEMPLATE,
            variables={
                "code": code,
                "ttl_minutes": str(self._policy.ttl_seconds // 60),
            },
        )
        logger.info(
            "otp challenge created phone=%s purpose=%s challenge_id=%s",
            _mask_phone(phone),
            purpose.value,
            challenge_id,
        )
        return ChallengePublic(challenge_id=challenge_id, expires_at=expires_at)

    async def verify_phone_challenge(
        self, challenge_id: uuid.UUID, code: str
    ) -> VerifiedPhoneToken:
        """Match ``code`` against a challenge and consume it exactly once.

        A success mints a single-use verified token (its own lifetime, its
        own Redis key) and marks the challenge consumed via one atomic
        ``HSETNX``; see the module docstring for the atomicity argument and
        the one compensation case around the backstop TTL.
        """
        now = self._clock.now()
        now_ts = now.timestamp()
        challenge_key = _challenge_key(challenge_id)
        raw_record = await self._redis.hgetall(challenge_key)
        record: dict[str, str] = {
            _decode(key): _decode(value) for key, value in raw_record.items()
        }

        if not record:
            logger.info(
                "otp verify rejected challenge_id=%s reason=unknown", challenge_id
            )
            raise UnknownChallengeError
        consumed_at = record.get("consumed_at")
        if consumed_at is not None:
            logger.info(
                "otp verify rejected challenge_id=%s reason=already_consumed",
                challenge_id,
            )
            raise ChallengeAlreadyConsumedError
        expires_at_raw = record.get("expires_at")
        stored_hash = record.get("code_hash")
        stored_salt = record.get("salt")
        if expires_at_raw is None or stored_hash is None or stored_salt is None:
            # Partial record can only be a backstop-TTL stub (see docstring).
            logger.info(
                "otp verify rejected challenge_id=%s reason=incomplete_record",
                challenge_id,
            )
            raise UnknownChallengeError
        if float(expires_at_raw) <= now_ts:
            logger.info(
                "otp verify rejected challenge_id=%s reason=expired", challenge_id
            )
            raise ChallengeExpiredError
        if int(record.get("attempts", "0")) >= self._policy.max_verify_attempts:
            logger.info(
                "otp verify rejected challenge_id=%s reason=attempt_limit",
                challenge_id,
            )
            raise TooManyAttemptsError

        submitted_hash = _hmac_hex(self._policy.hmac_secret, stored_salt, code)
        if not hmac.compare_digest(submitted_hash, stored_hash):
            attempts_used = int(await self._redis.hincrby(challenge_key, "attempts", 1))
            logger.info(
                "otp verify rejected challenge_id=%s reason=wrong_code "
                "attempts=%d max=%d",
                challenge_id,
                attempts_used,
                self._policy.max_verify_attempts,
            )
            raise WrongCodeError(
                attempts_used=attempts_used,
                max_attempts=self._policy.max_verify_attempts,
            )

        if not await self._redis.hsetnx(challenge_key, "consumed_at", str(now_ts)):
            logger.info(
                "otp verify rejected challenge_id=%s reason=consume_race_lost",
                challenge_id,
            )
            raise ChallengeAlreadyConsumedError
        if not await self._redis.hexists(challenge_key, "code_hash"):
            # The backstop TTL fired between read and claim, so HSETNX just
            # recreated a stub hash: report expiry and remove the stub.
            await self._redis.delete(challenge_key)
            logger.info(
                "otp verify rejected challenge_id=%s reason=expired_during_claim",
                challenge_id,
            )
            raise ChallengeExpiredError

        token = secrets.token_urlsafe(32)
        token_ttl = self._policy.verified_token_ttl_seconds
        token_expires_ts = now_ts + token_ttl
        await self._redis.set(
            _token_key(_hmac_hex(self._policy.hmac_secret, _TOKEN_HASH_SALT, token)),
            json.dumps(
                {
                    "phone": record["phone"],
                    "purpose": record.get("purpose"),
                    "expires_at": token_expires_ts,
                }
            ),
            ex=token_ttl + _REDIS_TTL_GRACE_SECONDS,
        )
        logger.info(
            "otp challenge consumed challenge_id=%s phone=%s",
            challenge_id,
            _mask_phone(record["phone"]),
        )
        return VerifiedPhoneToken(
            token=token, expires_at=now + timedelta(seconds=token_ttl)
        )

    async def verify_phone_token(self, token: str) -> VerifiedPhone:
        """Resolve and consume one verified token (``PhoneVerificationPort``).

        ``GETDEL`` is the single atomic consume: exactly one concurrent
        caller receives the payload. Unknown, already-consumed, and expired
        tokens all raise `InvalidTokenError` — callers must re-verify, and
        distinguishing the cases would only leak lifecycle state.

        The returned phone carries the challenge's ``purpose`` so consumers
        can reject a proof minted for a different operation: phone
        change and password reset — and registration too, which enforces
        ``REGISTER`` itself (``IdentityService.register_student`` rejects
        any other purpose; no consumer may ignore the field).
        """
        token_key = _token_key(
            _hmac_hex(self._policy.hmac_secret, _TOKEN_HASH_SALT, token)
        )
        raw = await self._redis.getdel(token_key)
        if raw is None:
            logger.info("otp token rejected reason=unknown_or_consumed")
            raise InvalidTokenError
        loaded: object = json.loads(raw)
        if not isinstance(loaded, dict):
            logger.info("otp token rejected reason=malformed_payload")
            raise InvalidTokenError
        expires_at = loaded.get("expires_at")
        phone = loaded.get("phone")
        purpose = loaded.get("purpose")
        if (
            not isinstance(phone, str)
            or not isinstance(expires_at, (int, float))
            or (purpose is not None and not isinstance(purpose, str))
        ):
            # A payload we did not write cannot authenticate anything.
            logger.info("otp token rejected reason=malformed_payload")
            raise InvalidTokenError
        if float(expires_at) <= self._clock.now().timestamp():
            logger.info("otp token rejected reason=expired")
            raise InvalidTokenError
        logger.info("otp token consumed phone=%s", _mask_phone(phone))
        return VerifiedPhone(phone_e164=phone, purpose=purpose)

    async def _require_not_in_cooldown(self, phone: str, now_ts: float) -> None:
        cooldown_until = await self._redis.get(_cooldown_key(phone))
        if cooldown_until is not None and float(_decode(cooldown_until)) > now_ts:
            logger.info(
                "otp request rejected phone=%s reason=cooldown", _mask_phone(phone)
            )
            raise ResendCooldownError

    async def _require_within_caps(self, phone: str, client_ip: str) -> None:
        windows = (
            (
                "phone",
                "hourly",
                phone,
                self._policy.phone_hourly_request_limit,
                _HOUR_SECONDS,
            ),
            (
                "phone",
                "daily",
                phone,
                self._policy.phone_daily_request_limit,
                _DAY_SECONDS,
            ),
            (
                "ip",
                "hourly",
                client_ip,
                self._policy.ip_hourly_request_limit,
                _HOUR_SECONDS,
            ),
            (
                "ip",
                "daily",
                client_ip,
                self._policy.ip_daily_request_limit,
                _DAY_SECONDS,
            ),
        )
        for scope, window, subject, limit, window_seconds in windows:
            key = f"{_RATE_KEY_PREFIX}{scope}:{window}:{subject}"
            count = int(await self._redis.incr(key))
            # EXPIRE ... NX on every hit, not only on the first: the window
            # still starts at the first counted request (NX never extends an
            # armed TTL), but a crash between INCR and EXPIRE can no longer
            # strand a TTL-less counter that throttles the subject forever —
            # the next hit re-arms it.
            await self._redis.expire(key, window_seconds, nx=True)
            if count > limit:
                logged_subject = _mask_phone(subject) if scope == "phone" else subject
                logger.info(
                    "otp request rejected subject=%s reason=rate_limit "
                    "scope=%s_%s count=%d limit=%d",
                    logged_subject,
                    scope,
                    window,
                    count,
                    limit,
                )
                raise OtpRateLimitError(scope=f"{scope}_{window}")


def normalize_phone(raw: str, region: str) -> str:
    """Normalize raw caller input to canonical E.164 (spec §5.4).

    Raw formatting (spaces, +86 vs 0086, domestic trunk) is parsed away by
    the standard library and never stored: only the E.164 form keys
    cooldown/caps and reaches the SmsSender and the challenge record.
    Public for cross-module use: the profile service normalizes a
    caller-supplied new phone BEFORE requesting a challenge, so the
    friendly bound-elsewhere pre-check compares the same canonical form
    the index enforces (backend-engineering §7) without re-implementing
    parsing.
    """
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneError from exc
    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneError
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _hmac_hex(secret: str, salt: str, value: str) -> str:
    """Domain-separated HMAC-SHA256 hex digest (spec §33.2: no plaintext)."""
    return hmac.new(
        secret.encode(), f"{salt}:{value}".encode(), hashlib.sha256
    ).hexdigest()


def _decode(value: bytes | str) -> str:
    """Accept str or bytes Redis replies, so client wiring may set either."""
    return value.decode() if isinstance(value, bytes) else value


def _mask_phone(phone_e164: str) -> str:
    """Mask a phone for logs (spec §5.4): keep prefix and last 4 digits."""
    if len(phone_e164) <= 8:
        return f"{phone_e164[:2]}****"
    return f"{phone_e164[:3]}****{phone_e164[-4:]}"


def _challenge_key(challenge_id: uuid.UUID) -> str:
    return f"{_CHALLENGE_KEY_PREFIX}{challenge_id}"


def _cooldown_key(phone_e164: str) -> str:
    return f"{_COOLDOWN_KEY_PREFIX}{phone_e164}"


def _token_key(token_hash: str) -> str:
    return f"{_TOKEN_KEY_PREFIX}{token_hash}"
