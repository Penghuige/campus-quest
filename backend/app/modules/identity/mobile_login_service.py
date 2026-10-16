# backend/app/modules/identity/mobile_login_service.py
"""Student mobile-code login (V1.1 spec 2026-10-16 §3).

The EmailLoginService discipline mirrored onto SMS — constant 202 with
the cooldown armed for ANY phone before any lookup, one live challenge
per phone with the keyed digest (hmac_challenge_code), the 5-attempt
cap, the shared account lockout — plus the student edition of the
remembered device (#87 craft): with a valid ``cq_staff_device`` cookie
the verify leg answers tokens with NO code at all (the cookie is the
credential; it also re-issues a fresh 30-day value, rotating it).

Design decisions:
- **The phone IS the login name** (owner decision): the user is located
  by ``phone_e164`` (globally unique by constraint). Only an
  ACTIVE/PENDING_APPROVAL STUDENT receives codes — any other shape is
  the silent no-op, same 202, nothing stored or sent.
- **PENDING_APPROVAL logs in** (spec §2): the freeze is per-acting
  surface; the session itself is browsing-grade.
- **The remembered-device leg never needs a code**: a valid cookie
  matching the phone's user is a full credential (single factor by
  design — the student channel is single-factor; the cookie's 30-day
  lifetime is the documented equivalence). A wrong/absent cookie with
  no code is the uniform invalid-code answer, never a branch oracle.
- The code exists in two places only: the SMS send call and the
  recipient's handset. Never logged; masked phone in every log line.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from datetime import timedelta
from uuid import UUID

import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import hmac_challenge_code
from app.integrations.errors import ProviderError
from app.integrations.masking import mask_phone
from app.integrations.sms import SmsSender
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.device_credentials import (
    issue as issue_device,
)
from app.modules.identity.device_credentials import (
    validate as validate_device,
)
from app.modules.identity.email_login_service import (
    CODE_TTL_MINUTES,
    MAX_VERIFY_ATTEMPTS,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.login_failure_guard import LoginFailureGuard
from app.modules.identity.models import LoginMobileChallenge
from app.modules.identity.otp import InvalidPhoneError, normalize_phone
from app.modules.identity.repository import UserRepository
from app.modules.identity.session_service import SessionService, SessionTokens

logger = logging.getLogger(__name__)

#: Six digits, five minutes — the shared login-code contract (the email
#: service owns the constants; the mobile mirror reuses them so the two
#: channels can never drift).
COOLDOWN_SECONDS = 60
_SMS_TEMPLATE = "phone_otp_verification"
_COOLDOWN_KEY_PREFIX = "mobilecode:cooldown:"
_CODE_INVALID_MESSAGE = "验证码错误或已过期"
_DELIVERY_FAILED_MESSAGE = "验证码短信发送失败，请稍后重试"
_SMS_TTL_MINUTES = "5"
AUDIT_MOBILE_LOGIN_SUCCEEDED = "MOBILE_LOGIN_SUCCEEDED"
_AUDIT_TARGET_TYPE = "user"


class MobileLoginService:
    """Issues and verifies SMSed student login codes."""

    def __init__(
        self,
        *,
        sms_sender: SmsSender,
        redis: aioredis.Redis,
        clock: Clock,
        sessions: SessionService | None = None,
        fernet: Fernet | None = None,
        guard: LoginFailureGuard | None = None,
        users: UserRepository | None = None,
        audit: AuditLogWriter | None = None,
        hmac_secret: str = "",
    ) -> None:
        self._sms = sms_sender
        self._redis = redis
        self._clock = clock
        self._sessions = sessions
        self._fernet = fernet  # symmetry with the email service; unused here
        self._guard = guard
        self._users = users if users is not None else UserRepository()
        self._audit = audit if audit is not None else AuditLogWriter()
        self._hmac_secret = hmac_secret

    def _hash_code(self, code: str) -> str:
        return hmac_challenge_code(self._hmac_secret, code)

    async def _account_locked(self, user_id: UUID) -> BusinessError:
        assert self._guard is not None
        retry_after = await self._guard.retry_after_seconds(user_id)
        return BusinessError(
            ErrorCode.ACCOUNT_LOCKED,
            "尝试次数过多，请稍后再试",
            status_code=429,
            details={"retry_after_seconds": retry_after},
        )

    async def request_code(self, db: AsyncSession, phone: str) -> None:
        """SMS one login code to a student phone — or nothing at all.

        Anti-enumeration ordering identical to the email side: the
        cooldown arms for EVERY submitted phone before any lookup.
        """
        try:
            normalized = normalize_phone(phone, "CN")
        except InvalidPhoneError:
            # A malformed number arms no cooldown and stores nothing —
            # the endpoint still answers the constant 202; the client's
            # own band validation is where this shape belongs.
            return
        cooldown_key = _COOLDOWN_KEY_PREFIX + normalized
        armed = await self._redis.set(cooldown_key, "1", ex=COOLDOWN_SECONDS, nx=True)
        if not armed:
            logger.info(
                "mobile login code rejected phone=%s reason=cooldown",
                mask_phone(normalized),
            )
            raise BusinessError(
                ErrorCode.RATE_LIMITED,
                "验证码请求过于频繁，请稍后再试",
                status_code=429,
            )

        user = await self._users.find_by_phone_e164(db, normalized)
        if (
            user is None
            or user.role != Role.STUDENT.value
            or user.status
            not in (UserStatus.ACTIVE.value, UserStatus.PENDING_APPROVAL.value)
        ):
            return  # silent no-op: same 202, nothing stored or sent

        code = f"{secrets.randbelow(1_000_000):06d}"
        now = self._clock.now()
        await db.execute(
            delete(LoginMobileChallenge).where(
                LoginMobileChallenge.phone_e164 == normalized
            )
        )
        challenge = LoginMobileChallenge(
            phone_e164=normalized,
            code_hash=self._hash_code(code),
            expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
        )
        db.add(challenge)
        await db.flush()

        try:
            await asyncio.to_thread(
                self._sms.send,
                to=normalized,
                template=_SMS_TEMPLATE,
                variables={"code": code, "ttl_minutes": _SMS_TTL_MINUTES},
            )
        except ProviderError as exc:
            await db.delete(challenge)
            await db.flush()
            await self._redis.delete(cooldown_key)
            logger.warning(
                "mobile login code send failed phone=%s error=%s",
                mask_phone(normalized),
                type(exc).__name__,
            )
            raise BusinessError(
                ErrorCode.EMAIL_DELIVERY_FAILED,
                _DELIVERY_FAILED_MESSAGE,
                status_code=502,
            ) from exc

        # COMMIT BOUNDARY: the 202 must leave the challenge durable for
        # the next request/connection (the email-service hotfix lesson).
        challenge_id = challenge.id
        await db.commit()
        logger.info(
            "mobile login code issued phone=%s challenge_id=%s",
            mask_phone(normalized),
            challenge_id,
        )

    async def verify_code(
        self,
        db: AsyncSession,
        *,
        phone: str,
        code: str | None,
        device_cookie: str | None = None,
        remember_device: bool = False,
        audit_context: AuditContext | None = None,
    ) -> tuple[SessionTokens, str | None]:
        """Consume the SMS code (or the remembered-device cookie) and
        open a browsing-grade student session.

        Returns (tokens, device_cookie): the cookie is a freshly minted
        30-day value when the login should set one (remember_device on
        the code leg, or ALWAYS on the cookie leg — the rotation is the
        renewal), else None.
        """
        assert self._sessions is not None and self._guard is not None, (
            "verify legs require sessions and guard wiring"
        )
        try:
            normalized = normalize_phone(phone, "CN")
        except InvalidPhoneError as exc:
            raise BusinessError(
                ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE
            ) from exc

        user = await self._users.find_by_phone_e164(db, normalized)
        if (
            user is None
            or user.role != Role.STUDENT.value
            or user.status
            not in (UserStatus.ACTIVE.value, UserStatus.PENDING_APPROVAL.value)
        ):
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        now = self._clock.now()

        # The remembered-device leg: a valid cookie matching THIS user is
        # a full credential — no code, no SMS, and a fresh 30-day value.
        if code is None or code.strip() == "":
            if (
                device_cookie is not None
                and (await validate_device(db, device_cookie, clock=self._clock))
                == user.id
            ):
                _, tokens = await self._sessions.issue_session(db, user=user, now=now)
                new_cookie: str | None = await issue_device(
                    db, user.id, clock=self._clock
                )
                await self._audit.append(
                    db,
                    actor=Actor(user_id=user.id, role=Role.STUDENT),
                    action=AUDIT_MOBILE_LOGIN_SUCCEEDED,
                    target_type=_AUDIT_TARGET_TYPE,
                    target_id=str(user.id),
                    after_snapshot={"channel": "mobile_code+device"},
                    ip_address=audit_context.ip_address if audit_context else None,
                    request_id=audit_context.request_id if audit_context else None,
                )
                await db.commit()
                await self._guard.reset(user.id)
                logger.info(
                    "mobile login succeeded user_id=%s channel=mobile_code+device",
                    user.id,
                )
                return tokens, new_cookie
            # No code and no valid cookie: the uniform invalid answer.
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        if await self._guard.is_locked(user.id):
            raise await self._account_locked(user.id)

        challenge = (
            await db.scalars(
                select(LoginMobileChallenge)
                .where(LoginMobileChallenge.phone_e164 == normalized)
                .order_by(
                    LoginMobileChallenge.created_at.desc(),
                    LoginMobileChallenge.id,
                )
                .limit(1)
            )
        ).first()
        if (
            challenge is None
            or challenge.expires_at <= now
            or challenge.attempts >= MAX_VERIFY_ATTEMPTS
        ):
            await self._guard.record_failure(user.id)
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        submitted_hash = self._hash_code(code.strip())
        if not hmac.compare_digest(submitted_hash, challenge.code_hash):
            challenge.attempts += 1
            # Persist the increment BEFORE raising (final review M-1):
            # the request transaction rolls back on the error path, and
            # without its own commit the attempts>=5 branch above is dead
            # code that only the lockout guard happens to backstop.
            await db.commit()
            await self._guard.record_failure(user.id)
            logger.info(
                "mobile login verify rejected user_id=%s reason=wrong_code",
                user.id,
            )
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        await db.delete(challenge)
        await db.flush()
        await self._guard.reset(user.id)
        _, tokens = await self._sessions.issue_session(db, user=user, now=now)
        new_cookie = (
            await issue_device(db, user.id, clock=self._clock)
            if remember_device
            else None
        )
        await self._audit.append(
            db,
            actor=Actor(user_id=user.id, role=Role.STUDENT),
            action=AUDIT_MOBILE_LOGIN_SUCCEEDED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(user.id),
            after_snapshot={"channel": "mobile_code"},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info("mobile login succeeded user_id=%s channel=mobile_code", user.id)
        return tokens, new_cookie
