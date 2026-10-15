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
import hmac
import logging
import secrets
from dataclasses import dataclass
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
from app.integrations.email import EmailSender
from app.integrations.errors import ProviderError
from app.integrations.masking import mask_email
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.device_credentials import issue as issue_device
from app.modules.identity.device_credentials import validate as validate_device
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.login_failure_guard import LoginFailureGuard
from app.modules.identity.models import LoginEmailChallenge, TotpCredential, User
from app.modules.identity.repository import UserRepository
from app.modules.identity.session_service import SessionService, SessionTokens
from app.modules.identity.staff_service import TotpSetupRequiredError
from app.modules.identity.totp import decrypt_totp_secret, verify_totp_code

logger = logging.getLogger(__name__)

AUDIT_EMAIL_LOGIN_SUCCEEDED = "EMAIL_LOGIN_SUCCEEDED"
AUDIT_EMAIL_LOGIN_TOTP_REQUIRED = "EMAIL_LOGIN_TOTP_REQUIRED"
_AUDIT_TARGET_TYPE = "user"

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
_PENDING_KEY_PREFIX = "emaillogin:pending:"
#: The mid-login pending credential's lifetime (the batch-4 ruling ①
#: posture: 30 minutes is short enough for a half-finished login).
PENDING_TTL_SECONDS = 30 * 60
_LOGIN_CODE_TEMPLATE = "staff_login_code"
_COOLDOWN_429_MESSAGE = "验证码请求过于频繁，请稍后再试"
_DELIVERY_FAILED_MESSAGE = "验证码邮件发送失败，请稍后重试"


#: Uniform wrong/expired/exhausted-code rejection (V1.1 §2.1): one
#: message for every failure shape — the branch reason never leaves the
#: server (anti-enumeration, the authenticate_staff discipline).
_CODE_INVALID_MESSAGE = "验证码错误或已过期"
_ACCOUNT_LOCKED_MESSAGE = "尝试次数过多，请稍后再试"
_AUTHENTICATION_REQUIRED_MESSAGE = "未登录或登录状态已失效"


@dataclass(frozen=True, slots=True)
class EmailCodeLoginOutcome:
    """What verify answered: final tokens, or the ADMIN TOTP mid-state.

    Exactly one of ``tokens`` / ``pending_token`` is set. The pending
    token is an OPAQUE Redis-backed credential for the totp leg only —
    never a session JWT (final review F1: a real session here would pass
    every management guard, because a confirmed ADMIN satisfies the
    guards' only 2FA check, reducing 邮箱码+TOTP to 邮箱码). The optional
    ``device_cookie`` is a freshly minted remembered-device value the
    route must set (HttpOnly) on whichever response it belongs to.
    """

    user_id: UUID
    role: Role
    tokens: SessionTokens | None = None
    pending_token: str | None = None
    device_cookie: str | None = None


class EmailLoginService:
    """Issues and verifies emailed staff login codes."""

    def __init__(
        self,
        *,
        sender: EmailSender,
        redis: aioredis.Redis,
        clock: Clock,
        sessions: SessionService | None = None,
        fernet: Fernet | None = None,
        guard: LoginFailureGuard | None = None,
        users: UserRepository | None = None,
        audit: AuditLogWriter | None = None,
        hmac_secret: str = "",
    ) -> None:
        # sessions/fernet/guard serve the verify legs; a caller that only
        # issues codes (request_code) may leave them unset.
        self._sender = sender
        self._redis = redis
        self._clock = clock
        self._sessions = sessions
        self._fernet = fernet
        self._guard = guard
        # The keyed-digest secret (settings.otp_hmac_secret at the
        # composition root): a bare SHA-256 of a 10^6 code space would be
        # brute-forceable from a database dump (hmac_challenge_code).
        self._hmac_secret = hmac_secret
        self._users = users if users is not None else UserRepository()
        self._audit: AuditLogWriter = audit if audit is not None else AuditLogWriter()

    def _hash_code(self, code: str) -> str:
        """Keyed digest of a login code (hmac_challenge_code): the
        stored form and the verify comparison share one construction."""
        return hmac_challenge_code(self._hmac_secret, code)

    @staticmethod
    def _hash_pending(token: str) -> str:
        """SHA-256 lookup key for the OPAQUE pending credential.

        Unlike the 10^6 code space, the token is 256-bit random — plain
        SHA-256 is the hash_refresh_token posture (deterministic lookup,
        no stretching needed), not the keyed-digest threat model.
        """
        return hashlib.sha256(token.encode()).hexdigest()

    async def _account_locked(self, user_id: UUID) -> BusinessError:
        """The ACCOUNT_LOCKED envelope with the remaining cooldown."""
        assert self._guard is not None, "verify legs require guard wiring"
        retry_after = await self._guard.retry_after_seconds(user_id)
        return BusinessError(
            ErrorCode.ACCOUNT_LOCKED,
            _ACCOUNT_LOCKED_MESSAGE,
            status_code=429,
            details={"retry_after_seconds": retry_after},
        )

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
        armed = await self._redis.set(cooldown_key, "1", ex=COOLDOWN_SECONDS, nx=True)
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
            code_hash=self._hash_code(code),
            expires_at=now + timedelta(minutes=CODE_TTL_MINUTES),
        )
        db.add(challenge)
        await db.flush()

        try:
            # KNOWN SIDE CHANNEL (final review F4, documented by the
            # reviewer's ruling 2026-10-15): the send happens inline, so
            # under a real SMTP relay a staff mailbox answers measurably
            # slower than an unknown one (delivery latency vs. the no-op
            # return) — a staff-existence probe. Accepted for V1.1
            # (staff mailboxes are guessable anyway; the 60s cooldown and
            # the request limiter bound the probe rate); the fix is
            # queueing the send behind the 202, which changes the
            # product semantics and is a deliberate follow-up, not a
            # patch here.
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
            # The code reached nobody: the caller's transaction ends in a
            # rollback (the route raises past any commit), which discards
            # the flushed challenge row by itself; the explicit delete
            # keeps that true even if a future caller commits on another
            # path. The Redis cooldown is released so the recipient's
            # retry is clean (module docstring).
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

        # COMMIT BOUNDARY (production hotfix): get_db_session never
        # commits and rolls back in its finally — without this commit the
        # 202 returns and the challenge row silently vanishes, and the
        # verify leg (a different request/connection) finds nothing. The
        # flush above is NOT enough; every verify leg already commits the
        # same way (the authenticate_staff discipline). The id is
        # captured pre-commit (the SessionService rule: no post-commit
        # attribute access on a session that may expire).
        challenge_id = challenge.id
        await db.commit()
        logger.info(
            "email login code issued to=%s challenge_id=%s",
            mask_email(normalized),
            challenge_id,
        )

    # --- verify legs (routes mount these; V1.1 §2.1/§2.2) -----------------------

    async def verify_code(
        self,
        db: AsyncSession,
        *,
        email: str,
        code: str,
        device_cookie: str | None = None,
        remember_device: bool = False,
        audit_context: AuditContext | None = None,
    ) -> EmailCodeLoginOutcome:
        """Consume one emailed code: TEACHER finishes, ADMIN branches.

        Ordering (each failure uniform, never an oracle): staff/ACTIVE
        check -> lockout check (fail fast, V1.1 §6) -> challenge state
        (missing/expired/exhausted all invalid) -> constant-time digest
        compare. A wrong code increments ``attempts`` and counts toward
        the account lockout; the correct code deletes the challenge and
        RESETS the failure streak (§6 成功登录清零).

        ADMIN without a valid remembered-device cookie receives a
        pending session (the totp leg completes it); with one, the login
        finishes here (§2.2 30 天内邮箱→码→进).
        """
        assert self._sessions is not None and self._guard is not None, (
            "verify legs require sessions and guard wiring"
        )
        normalized = email.strip().lower()
        now = self._clock.now()

        user = await self._users.find_by_username(db, normalized)
        if (
            user is None
            or user.role == Role.STUDENT.value
            or user.status != UserStatus.ACTIVE.value
        ):
            # No account detail leaves the server: the same invalid-code
            # answer a wrong code gets.
            raise BusinessError(ErrorCode.EMAIL_CODE_INVALID, _CODE_INVALID_MESSAGE)

        if await self._guard.is_locked(user.id):
            raise await self._account_locked(user.id)

        challenge = (
            await db.scalars(
                select(LoginEmailChallenge)
                .where(LoginEmailChallenge.email_normalized == normalized)
                .order_by(LoginEmailChallenge.created_at.desc(), LoginEmailChallenge.id)
                .limit(1)
            )
        ).first()
        if (
            challenge is None
            or challenge.expires_at <= now
            or challenge.attempts >= MAX_VERIFY_ATTEMPTS
        ):
            await self._guard.record_failure(user.id)
            logger.info(
                "email login verify rejected user_id=%s reason=invalid_challenge",
                user.id,
            )
            raise BusinessError(ErrorCode.EMAIL_CODE_INVALID, _CODE_INVALID_MESSAGE)

        submitted_hash = self._hash_code(code.strip())
        if not hmac.compare_digest(submitted_hash, challenge.code_hash):
            challenge.attempts += 1
            await db.flush()
            await self._guard.record_failure(user.id)
            logger.info(
                "email login verify rejected user_id=%s reason=wrong_code "
                "attempts=%d max=%d",
                user.id,
                challenge.attempts,
                MAX_VERIFY_ATTEMPTS,
            )
            raise BusinessError(ErrorCode.EMAIL_CODE_INVALID, _CODE_INVALID_MESSAGE)

        # Correct code: single-use consumption, streak reset, then the
        # role branch. Ids captured pre-commit (the SessionService rule).
        await db.delete(challenge)
        await db.flush()
        await self._guard.reset(user.id)
        user_id, role_value = user.id, Role(user.role)
        actor = Actor(user_id=user_id, role=role_value)

        remember = bool(remember_device)
        if role_value is Role.TEACHER:
            _, tokens = await self._sessions.issue_session(db, user=user, now=now)
            device_cookie = (
                await issue_device(db, user_id, clock=self._clock) if remember else None
            )
            await self._audit.append(
                db,
                actor=actor,
                action=AUDIT_EMAIL_LOGIN_SUCCEEDED,
                target_type=_AUDIT_TARGET_TYPE,
                target_id=str(user_id),
                after_snapshot={"channel": "email_code"},
                ip_address=audit_context.ip_address if audit_context else None,
                request_id=audit_context.request_id if audit_context else None,
            )
            await db.commit()
            logger.info("email login succeeded user_id=%s channel=email_code", user_id)
            return EmailCodeLoginOutcome(
                user_id=user_id,
                role=role_value,
                tokens=tokens,
                device_cookie=device_cookie,
            )

        # ADMIN: a valid remembered device finishes the login here.
        if (
            device_cookie is not None
            and (await validate_device(db, device_cookie, clock=self._clock)) == user_id
        ):
            _, tokens = await self._sessions.issue_session(db, user=user, now=now)
            new_cookie = (
                await issue_device(db, user_id, clock=self._clock) if remember else None
            )
            await self._audit.append(
                db,
                actor=actor,
                action=AUDIT_EMAIL_LOGIN_SUCCEEDED,
                target_type=_AUDIT_TARGET_TYPE,
                target_id=str(user_id),
                after_snapshot={"channel": "email_code", "remembered_device": True},
                ip_address=audit_context.ip_address if audit_context else None,
                request_id=audit_context.request_id if audit_context else None,
            )
            await db.commit()
            logger.info(
                "email login succeeded user_id=%s channel=email_code+device", user_id
            )
            return EmailCodeLoginOutcome(
                user_id=user_id,
                role=role_value,
                tokens=tokens,
                device_cookie=new_cookie,
            )

        # ADMIN without the device: an OPAQUE pending credential for the
        # totp leg only (Redis, 30-minute TTL, single-use on completion).
        # Deliberately NOT a session: a real UserSession JWT would pass
        # every management guard (the guards' only 2FA check is
        # confirmed_at, which a logging-in ADMIN already has) — final
        # review F1.
        pending_token = secrets.token_urlsafe(32)
        await self._redis.set(
            _pending_key(self._hash_pending(pending_token)),
            str(user_id),
            ex=PENDING_TTL_SECONDS,
        )
        await self._audit.append(
            db,
            actor=actor,
            action=AUDIT_EMAIL_LOGIN_TOTP_REQUIRED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(user_id),
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info("email login totp_required user_id=%s", user_id)
        return EmailCodeLoginOutcome(
            user_id=user_id, role=role_value, pending_token=pending_token
        )

    async def verify_totp(
        self,
        db: AsyncSession,
        *,
        pending_token: str,
        code: str,
        remember_device: bool = False,
        audit_context: AuditContext | None = None,
    ) -> EmailCodeLoginOutcome:
        """Finish an ADMIN email-code login with the TOTP second leg.

        ``pending_token`` is the opaque Redis credential the verify leg
        minted: resolved (and consumed atomically via GETDEL) here — it
        authenticates NOTHING else, so the identity it names is only
        proven once the second factor below also passes. A wrong TOTP
        code is the staff-login uniform authentication failure and
        counts toward the lockout (the pending credential survives a
        wrong code — the lockout, not token churn, bounds the retries);
        a missing or unconfirmed credential is the typed setup error.
        """
        assert self._sessions is not None and self._guard is not None, (
            "verify legs require sessions and guard wiring"
        )
        now = self._clock.now()
        raw_user_id = await self._redis.getdel(
            _pending_key(self._hash_pending(pending_token))
        )
        if raw_user_id is None:
            # Unknown, expired, or already-consumed pending credential —
            # one uniform answer; the branch reason never leaves the server.
            logger.info("email login totp rejected reason=unknown_pending_token")
            raise BusinessError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                _AUTHENTICATION_REQUIRED_MESSAGE,
                status_code=401,
            )
        user_id = UUID(
            raw_user_id.decode() if isinstance(raw_user_id, bytes) else raw_user_id
        )
        user = await db.get(User, user_id)
        if user is None or Role(user.role) is not Role.ADMIN:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                "该登录通道的管理员验证码步骤不适用于此账号",
                status_code=403,
            )
        if user.status != UserStatus.ACTIVE.value:
            raise BusinessError(
                ErrorCode.ACCOUNT_NOT_ACTIVE,
                "账号当前状态不允许执行该操作",
                status_code=403,
            )
        if await self._guard.is_locked(user_id):
            raise await self._account_locked(user_id)

        credential = await db.get(TotpCredential, user_id)
        if credential is None or credential.confirmed_at is None:
            raise TotpSetupRequiredError("该账号必须先绑定 TOTP 动态验证码")

        assert self._fernet is not None
        secret = decrypt_totp_secret(self._fernet, credential.secret_encrypted)
        if not verify_totp_code(secret, code, at=now):
            await self._guard.record_failure(user_id)
            logger.info(
                "email login totp rejected user_id=%s reason=totp_code", user_id
            )
            raise BusinessError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                _AUTHENTICATION_REQUIRED_MESSAGE,
                status_code=401,
            )

        await self._guard.reset(user_id)
        _, tokens = await self._sessions.issue_session(db, user=user, now=now)
        device_cookie = (
            await issue_device(db, user_id, clock=self._clock)
            if remember_device
            else None
        )
        await self._audit.append(
            db,
            actor=Actor(user_id=user_id, role=Role.ADMIN),
            action=AUDIT_EMAIL_LOGIN_SUCCEEDED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(user_id),
            after_snapshot={"channel": "email_code+totp"},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info("email login succeeded user_id=%s channel=email_code+totp", user_id)
        return EmailCodeLoginOutcome(
            user_id=user_id,
            role=Role.ADMIN,
            tokens=tokens,
            device_cookie=device_cookie,
        )


def _pending_key(token_hash: str) -> str:
    """Redis key of one mid-login pending credential (hashed lookup)."""
    return _PENDING_KEY_PREFIX + token_hash
