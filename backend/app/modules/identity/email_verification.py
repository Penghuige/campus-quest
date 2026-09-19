# backend/app/modules/identity/email_verification.py
"""Student email binding, verification, and unbinding (spec §5.5; Task 8).

`EmailVerificationService` is the student-facing email-verification
machinery `staff_service` deferred to this task (staff emails are
verified-on-invitation-acceptance instead).

Design decisions:

- **Tokens are 256-bit secrets stored as SHA-256 digests, in Redis.**
  ``secrets.token_urlsafe(32)`` — the refresh-token entropy class, see
  `app.core.security` for why that needs no key stretching — hashed with
  a module-local SHA-256 rather than `hash_refresh_token` only to keep the
  name honest; the scheme is identical. The plaintext token exists in the
  `EmailSender.send` variables and the recipient's mailbox, nowhere else
  (never persisted, never logged — backend-engineering §15).
- **Redis is the challenge store, like the OTP service.** Business TTL
  (24h default) is enforced client-side against the injected Clock; the
  Redis key TTL is the usual business-TTL-plus-grace backstop, so an
  expired token still answers with the precise error instead of silently
  vanishing. Single-use rests on one atomic ``GETDEL``: exactly one
  concurrent confirm receives the payload. A confirm that fails the
  user/address/expiry checks still consumed the token — a token holder
  can burn it, but only its addressee can use it (fail closed).
- **A token binds ONE (user, address) pair.** The Redis payload carries
  both; confirmation re-checks the address against the CURRENT row, so a
  token for a superseded address (the user re-requested for a different
  email) can never verify the newest one, and a token minted for user A
  cannot verify user B's account.
- **Unverified emails still hold the V1 uniqueness slot.** The request
  writes ``email_normalized`` with ``email_verified_at = NULL``
  immediately (spec §5.5: 未验证邮箱存在时 EMAIL 通知应跳过 — the state
  is representable), the partial unique index enforces global uniqueness
  for non-null values (models.py), and the friendly pre-check plus the
  ``IntegrityError`` mapping produce the typed `EmailAlreadyBoundError`
  for both the sequential and the racing path. Re-requesting one's own
  address restarts the cycle (verified-at cleared) — fail-safe, and the
  resend path for a lost email.
- **Ordering: DB first, send second.** The unverified address is
  committed before the token is stored and the email sent: a send failure
  leaves a consistent state (unverified address, no notification — §5.5
  skips rather than errors) that a re-request repairs.
- **Unbind is re-authenticated and email-only** (spec §5.5 解绑前不得影响
  手机号登录能力): current-password verification, then both email columns
  cleared to NULL — the partial unique index tolerates any number of NULL
  emails, and nothing here touches ``phone_e164``.

Error taxonomy: `BusinessError` for registered codes
(`VALIDATION_ERROR`, `AUTHENTICATION_REQUIRED`); typed module exceptions
(`EmailAlreadyBoundError`, `InvalidEmailTokenError`) for the two states
no frozen registry code covers — T9 maps them doc-first, following
`otp.py` / `TotpSetupRequiredError`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import verify_password
from app.integrations.email import EmailSender
from app.modules.identity.models import User
from app.modules.identity.repository import UserRepository

logger = logging.getLogger(__name__)

_TOKEN_KEY_PREFIX = "email:verify:"
# Redis backstop TTL = business TTL + grace (the OTP service pattern).
_REDIS_TTL_GRACE_SECONDS = 600
_EMAIL_TEMPLATE = "email_verification"
_EMAIL_MAX_LENGTH = 320
_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email_normalized"

_VALIDATION_MESSAGE = "邮箱信息校验失败"
_AUTHENTICATION_MESSAGE = "密码错误或登录状态已失效"
_EMAIL_TAKEN_MESSAGE = "该邮箱已被其他账号使用"


class EmailVerificationError(Exception):
    """Base for every email-verification flow failure."""


class EmailAlreadyBoundError(EmailVerificationError):
    """The normalized email is held by another account (spec §5.5 V1)."""

    def __init__(self) -> None:
        super().__init__(_EMAIL_TAKEN_MESSAGE)


class InvalidEmailTokenError(EmailVerificationError):
    """Unknown, expired, consumed, or wrong-(user, address) email token.

    One uniform error — the branch reason never leaves the server, the
    same discipline as `otp.InvalidTokenError`.
    """

    def __init__(self) -> None:
        super().__init__("邮箱验证链接无效或已过期")


@dataclass(frozen=True, slots=True)
class EmailChallenge:
    """Request-side view of one email verification cycle.

    Deliberately token-less: the token travels only inside the email, so
    no caller can leak it by echoing a return value. Tests read it from
    `FakeEmailSender.messages`.
    """

    user_id: UUID
    expires_at: datetime


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of a 256-bit token — the Redis lookup key.

    Same scheme and rationale as `app.core.security.hash_refresh_token`
    (deterministic UNIQUE-key lookup; entropy makes stretching pointless);
    restated locally so the name says "email token", not "refresh token".
    """
    return hashlib.sha256(token.encode()).hexdigest()


class EmailVerificationService:
    """Bind, verify, and unbind a student email (spec §5.5)."""

    def __init__(
        self,
        *,
        clock: Clock,
        email_sender: EmailSender,
        redis: aioredis.Redis,
        token_ttl_hours: int = 24,
    ) -> None:
        # 24h is the plan's default window. A constructor knob (like
        # `StaffService.invitation_ttl_hours`) rather than a Settings read:
        # the router task wires deployment settings into constructors at
        # the composition root, where config.py is in scope.
        self._clock = clock
        self._email_sender = email_sender
        self._redis = redis
        self._ttl = timedelta(hours=token_ttl_hours)
        self._users = UserRepository()

    async def request_email_verification(
        self, db: AsyncSession, user_id: UUID, raw_email: str
    ) -> EmailChallenge:
        """Bind the normalized email (unverified) and send its token.

        The friendly uniqueness pre-check rejects an address held by any
        OTHER account before anything is written; the flush's
        ``IntegrityError`` mapping is the race closer (the index is the
        law, §5.5 / backend-engineering §7). Re-requesting one's own
        address restarts the verification cycle. The address is committed
        before the token is stored and the email sent (see the module
        docstring for the failure-mode argument).
        """
        user = await self._require_user(db, user_id)
        email = self._normalized_email(raw_email)

        bound = await self._users.find_by_email_normalized(db, email)
        if bound is not None and bound.id != user.id:
            raise EmailAlreadyBoundError

        user.email_normalized = email
        user.email_verified_at = None
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            violation = str(exc.orig) if exc.orig is not None else str(exc)
            if _EMAIL_UNIQUE_CONSTRAINT not in violation:
                raise
            raise EmailAlreadyBoundError from exc
        await db.commit()

        token = secrets.token_urlsafe(32)
        expires_at = self._clock.now() + self._ttl
        await self._redis.set(
            f"{_TOKEN_KEY_PREFIX}{_hash_token(token)}",
            json.dumps(
                {
                    "user_id": str(user_id),
                    "email": email,
                    "expires_at": expires_at.timestamp(),
                }
            ),
            ex=int(self._ttl.total_seconds()) + _REDIS_TTL_GRACE_SECONDS,
        )
        self._email_sender.send(
            to=email,
            template=_EMAIL_TEMPLATE,
            variables={
                "token": token,
                "ttl_hours": str(int(self._ttl.total_seconds() // 3600)),
            },
        )
        logger.info(
            "email verification requested user_id=%s email=%s",
            user_id,
            _mask_email(email),
        )
        return EmailChallenge(user_id=user_id, expires_at=expires_at)

    async def confirm_email_verification(
        self, db: AsyncSession, user_id: UUID, token: str
    ) -> User:
        """Consume the single-use token and stamp ``email_verified_at``.

        The payload's (user, address) pair must match the current row, and
        the business clock must still be inside the TTL window — the Redis
        key TTL is only a backstop. Until this succeeds the address stays
        unverified and receives no business email (spec §5.5).
        """
        user = await self._require_user(db, user_id)

        raw = await self._redis.getdel(f"{_TOKEN_KEY_PREFIX}{_hash_token(token)}")
        if raw is None:
            logger.info("email token rejected reason=unknown_or_consumed")
            raise InvalidEmailTokenError
        loaded: object = json.loads(_decode(raw))
        if not isinstance(loaded, dict):
            logger.info("email token rejected reason=malformed_payload")
            raise InvalidEmailTokenError
        payload_user = loaded.get("user_id")
        payload_email = loaded.get("email")
        expires_at = loaded.get("expires_at")
        if (
            not isinstance(payload_user, str)
            or not isinstance(payload_email, str)
            or not isinstance(expires_at, (int, float))
        ):
            logger.info("email token rejected reason=malformed_payload")
            raise InvalidEmailTokenError
        if float(expires_at) <= self._clock.now().timestamp():
            logger.info("email token rejected reason=expired")
            raise InvalidEmailTokenError
        if payload_user != str(user_id) or user.email_normalized != payload_email:
            # Minted for another account, or for an address the row no
            # longer carries (a newer re-request superseded it).
            logger.info("email token rejected reason=owner_or_address_mismatch")
            raise InvalidEmailTokenError

        user.email_verified_at = self._clock.now()
        await db.commit()
        logger.info(
            "email verified user_id=%s email=%s",
            user_id,
            _mask_email(payload_email),
        )
        return user

    async def unbind_email(
        self, db: AsyncSession, user_id: UUID, password: str
    ) -> None:
        """Re-authenticate, then clear the email binding entirely (§5.5).

        Both columns go NULL — the partial unique index tolerates it, the
        slot is freed for another account, and the phone binding (and with
        it the account's login capability) is untouched.
        """
        user = await self._require_user(db, user_id)
        self._require_current_password(user, password)

        user.email_normalized = None
        user.email_verified_at = None
        await db.commit()
        logger.info("email unbound user_id=%s", user_id)

    async def _require_user(self, db: AsyncSession, user_id: UUID) -> User:
        user = await db.get(User, user_id)
        if user is None:
            raise self._authentication_required()
        return user

    @staticmethod
    def _require_current_password(user: User, password: str) -> None:
        """Re-auth before unbinding (the SessionService discipline)."""
        try:
            matched = verify_password(password, user.password_hash)
        except ValueError:
            matched = False
        if not matched:
            raise EmailVerificationService._authentication_required()

    @staticmethod
    def _normalized_email(value: str) -> str:
        """Case-normalize an email; minimal shape check (§5.5).

        The same shape policy `staff_service` applies to invitations:
        trim + lowercase, one interior ``@``, bounded length. Full RFC
        validation is deliberately out of scope — deliverability is
        proven by the verification email itself, and `find_by_email_
        normalized` keys on exactly this normalized form.
        """
        normalized = value.strip().lower()
        if (
            not normalized
            or "@" not in normalized
            or normalized.startswith("@")
            or normalized.endswith("@")
            or len(normalized) > _EMAIL_MAX_LENGTH
        ):
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "email", "reason": "must be a valid email address"},
            )
        return normalized

    @staticmethod
    def _authentication_required() -> BusinessError:
        return BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            _AUTHENTICATION_MESSAGE,
            status_code=401,
        )


def _decode(value: bytes | str) -> str:
    """Accept str or bytes Redis replies (client wiring may set either)."""
    return value.decode() if isinstance(value, bytes) else value


def _mask_email(email: str) -> str:
    """Mask an email for logs (§15): first local char + domain only."""
    local, separator, domain = email.partition("@")
    if not separator or not local:
        return "***"
    return f"{local[:1]}***@{domain}"
