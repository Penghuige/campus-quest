# backend/app/modules/identity/email_login_service.py
"""Staff email-code login (V1.1 §2.1, Wave 1).

TEACHER log in with email + one 6-digit emailed code; ADMIN add a TOTP
leg and the optional remembered-device cookie. This service owns the
challenge lifecycle: ``request_code`` mints and mails a code,
``verify_code`` (added with the routes) consumes it exactly once. The
old password+TOTP channel is untouched (spec §2.4).

Design decisions:

- **Anti-enumeration is structural, not cosmetic.** The 60s cooldown is
  armed on the RAW submitted (normalized) mailbox BEFORE the user
  lookup, for every request — unknown, student, and suspended mailboxes
  trip exactly the same 429 on a second hit as a real teacher's does.
  No-ops for non-staff mailboxes store nothing, send nothing, and raise
  nothing; the request endpoint renders a constant 202 either way.
- **One live challenge per mailbox.** A new request deletes the
  mailbox's previous row: ``verify_code`` can then simply load "the"
  challenge, and an un-expired older code can never verify.
- **The code exists in two places only**: the send call and the
  recipient's mailbox. The row stores ``SHA-256(code)``; verification
  recomputes and compares digests constant-time. Never logged.
- **Send failure cleans up after itself.** The challenge row is stored
  before the send (so the mail can race the response), but a provider
  failure raises ``EMAIL_DELIVERY_FAILED`` (502) with the row deleted
  and the cooldown released: the code reached nobody, so a dead
  challenge plus a 60s penalty would only punish the recipient for the
  provider's failure. The immediate retry is a clean re-request.
- Business time is the injected ``Clock`` (backend-engineering §11);
  the Redis cooldown TTL is a cleanup backstop, never the check itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import timedelta

import redis.asyncio as aioredis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.integrations.email import EmailSender
from app.integrations.errors import ProviderError
from app.integrations.masking import mask_email
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import LoginEmailChallenge
from app.modules.identity.repository import UserRepository

logger = logging.getLogger(__name__)

#: Six digits, five minutes of validity (V1.1 §2.1 — frontend copy
#: promises "5 分钟内有效", so this constant is the contract).
CODE_TTL_MINUTES = 5
CODE_TTL_SECONDS = CODE_TTL_MINUTES * 60
#: Resend cooldown per mailbox, in seconds (V1.1 §2.1).
COOLDOWN_SECONDS = 60
#: Maximum wrong-code attempts before the challenge is dead (V1.1 §2.1;
#: Review Focus 2 — after the 5th miss even the CORRECT code fails).
MAX_VERIFY_ATTEMPTS = 5
_COOLDOWN_KEY_PREFIX = "emailcode:cooldown:"
_LOGIN_CODE_TEMPLATE = "staff_login_code"
_COOLDOWN_429_MESSAGE = "验证码请求过于频繁，请稍后再试"
_DELIVERY_FAILED_MESSAGE = "验证码邮件发送失败，请稍后重试"


class EmailLoginService:
    """Issues and (with the routes) verifies emailed staff login codes."""

    def __init__(
        self,
        *,
        sender: EmailSender,
        redis: aioredis.Redis,
        clock: Clock,
        users: UserRepository | None = None,
    ) -> None:
        self._sender = sender
        self._redis = redis
        self._clock = clock
        self._users = users if users is not None else UserRepository()

    async def request_code(self, db: AsyncSession, email: str) -> None:
        """Mail one login code to a staff mailbox — or nothing at all.

        See the module docstring for the anti-enumeration ordering: the
        cooldown arms for EVERY submitted mailbox before any lookup, so
        the observable 202/429 shape is mailbox-independent.
        """
        normalized = email.strip().lower()
        cooldown_key = _COOLDOWN_KEY_PREFIX + normalized
        # SET NX EX as one server-side primitive: the first request arms
        # the window; a second inside it gets None -> 429. Armed even
        # when the mailbox will turn out to be non-staff (see above).
        armed = await self._redis.set(
            cooldown_key, "1", ex=COOLDOWN_SECONDS, nx=True
        )
        if not armed:
            logger.info(
                "email login code rejected to=%s reason=cooldown",
                mask_email(normalized),
            )
            raise BusinessError(
                ErrorCode.RATE_LIMITED, _COOLDOWN_429_MESSAGE, status_code=429
            )

        user = await self._users.find_by_username(db, normalized)
        if (
            user is None
            or user.role == Role.STUDENT.value
            or user.status != UserStatus.ACTIVE.value
        ):
            # Silent no-op: same return, same 202, nothing stored or
            # sent. The cooldown armed above keeps the 429 shape uniform.
            return

        code = f"{secrets.randbelow(1_000_000):06d}"
        now = self._clock.now()
        # One live challenge per mailbox: the predecessor row goes first.
        await db.execute(
            delete(LoginEmailChallenge).where(
                LoginEmailChallenge.email_normalized == normalized
            )
        )
        challenge = LoginEmailChallenge(
            email_normalized=normalized,
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
        )
        db.add(challenge)
        await db.flush()

        try:
            await asyncio.to_thread(
                self._sender.send,
                to=normalized,
                template=_LOGIN_CODE_TEMPLATE,
                variables={
                    "code": code,
                    "ttl_minutes": str(CODE_TTL_MINUTES),
                },
            )
        except ProviderError as exc:
            # The code reached nobody: erase this request's traces so the
            # recipient's retry is clean (module docstring).
            await db.delete(challenge)
            await db.flush()
            await self._redis.delete(cooldown_key)
            logger.warning(
                "email login code send failed to=%s error=%s",
                mask_email(normalized),
                type(exc).__name__,
            )
            raise BusinessError(
                ErrorCode.EMAIL_DELIVERY_FAILED,
                _DELIVERY_FAILED_MESSAGE,
                status_code=502,
            ) from exc

        logger.info(
            "email login code issued to=%s challenge_id=%s",
            mask_email(normalized),
            challenge.id,
        )
