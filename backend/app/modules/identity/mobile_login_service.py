# backend/app/modules/identity/mobile_login_service.py
"""Student mobile-code login (V1.1 spec 2026-10-16 §3).

The EmailLoginService discipline mirrored onto SMS — constant 202 with
the cooldown armed for ANY phone before any lookup, one live challenge
per phone with the keyed digest (hmac_challenge_code), the 5-attempt
cap, the shared account lockout. The student edition of the remembered
device (#87 craft) now lives in the unified facade's empty-body verify
(the multi-slot batch, 2026-10-24): this service mints slot cookies on
remember_device but no longer carries its own cookie leg.

Design decisions:
- **The phone IS the login name** (owner decision): the user is located
  by ``phone_e164`` (globally unique by constraint). Only an
  ACTIVE/PENDING_APPROVAL STUDENT receives codes — any other shape is
  the silent no-op, same 202, nothing stored or sent.
- **PENDING_APPROVAL logs in** (spec §2): the freeze is per-acting
  surface; the session itself is browsing-grade.
- The code exists in two places only: the SMS send call and the
  recipient's handset. Never logged; masked phone in every log line.
"""

from __future__ import annotations

import asyncio
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
from app.integrations.errors import ProviderError
from app.integrations.masking import mask_phone
from app.integrations.sms import SmsSender
from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.device_credentials import (
    issue as issue_device,
)
from app.modules.identity.email_login_service import (
    CODE_TTL_MINUTES,
    MAX_VERIFY_ATTEMPTS,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.login_failure_guard import LoginFailureGuard
from app.modules.identity.models import LoginMobileChallenge, User
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

#: The staff channel's cooldown namespace (a phone can arm both a
#: student and a staff request independently) and the ADMIN mid-login
#: pending credential (opaque, single-use, 30 minutes — the email-login
#: F1 discipline mirrored).
_STAFF_COOLDOWN_KEY_PREFIX = "staffmobilecode:cooldown:"
_STAFF_PENDING_KEY_PREFIX = "staffmobilecode:pending:"
STAFF_PENDING_TTL_SECONDS = 30 * 60

#: The phone-first ENTRY proof (the queue-jump batch): a 5-minute,
#: single-use credential answering "this phone just passed a code
#: check". Stored hashed in Redis; consumed by GETDEL at register.
_REGISTER_KEY_PREFIX = "mobreg:"
REGISTER_TOKEN_TTL_SECONDS = 5 * 60
_ENTRY_STATUS_UNREGISTERED = "unregistered"


def _hash_text(value: str) -> str:
    from hashlib import sha256

    return sha256(value.encode()).hexdigest()


def _staff_pending_key(token_hash: str) -> str:
    return _STAFF_PENDING_KEY_PREFIX + token_hash


@dataclass(frozen=True, slots=True)
class MobileVerifyOutcome:
    """The verify leg's three terminal shapes: a student session
    (tokens, maybe a rotated device cookie), or the ENTRY token for an
    unregistered phone (register_token; tokens None)."""

    tokens: SessionTokens | None = None
    device_cookie: str | None = None
    device_slot: str | None = None
    register_token: str | None = None

    @property
    def unregistered(self) -> bool:
        return self.tokens is None


@dataclass(frozen=True, slots=True)
class StaffMobileCodeOutcome:
    """Exactly one of ``tokens`` / ``pending_token`` is set: the pending
    token is the ADMIN mid-login state (the totp leg finishes it), the
    tokens mean the login is complete."""

    tokens: SessionTokens | None = None
    device_cookie: str | None = None
    device_slot: str | None = None
    pending_token: str | None = None


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

        # The entry channel (the queue-jump batch): EVERY well-formed
        # phone receives a real code — registered or not. The challenge
        # table keys by phone, nothing here is account-gated; cooldown
        # and the endpoint limiter bound the send surface.
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
        phone: str | None,
        code: str | None,
        device_cookies: dict[str, str] | None = None,
        remember_device: bool = False,
        audit_context: AuditContext | None = None,
    ) -> MobileVerifyOutcome:
        """Consume the SMS code and open a browsing-grade student
        session — or, for a code-checked phone with NO account, hand
        back the 5-minute single-use ENTRY token (the queue-jump
        batch's register door).

        Returns the outcome with a freshly minted 30-day device cookie
        (value + slot) when the login should set one (remember_device
        on the code leg), else both None.

        The empty-code cookie-as-credential leg this service once
        carried is RETIRED (2026-10-24, the multi-slot batch): the
        unified facade owns that leg (its empty-body verify enumerates
        the whole presented jar); this service's only caller never
        sends an empty code. ``device_cookies`` rides the signature for
        the arms that honor a remembered credential; the student code
        leg does not.
        """
        assert self._sessions is not None and self._guard is not None, (
            "verify legs require sessions and guard wiring"
        )

        def _invalid() -> BusinessError:
            return BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        if code is None or code.strip() == "":
            # The cookie-as-credential leg lives in the unified facade
            # (the empty-body verify); an empty code here is simply an
            # invalid request — the uniform envelope, no oracle.
            raise _invalid()
        if phone is None:
            # A code with no phone has no challenge to match.
            raise _invalid()
        try:
            normalized = normalize_phone(phone, "CN")
        except InvalidPhoneError as exc:
            raise _invalid() from exc
        user = await self._users.find_by_phone_e164(db, normalized)
        if user is not None and (
            user.role != Role.STUDENT.value
            or user.status
            not in (UserStatus.ACTIVE.value, UserStatus.PENDING_APPROVAL.value)
        ):
            # A phone bound to staff/non-student is not an entry.
            raise _invalid()

        now = self._clock.now()

        if user is not None and await self._guard.is_locked(user.id):
            raise await self._account_locked(user.id)

        assert code is not None and normalized is not None  # the code leg
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
            if user is not None:  # an accountless phone has no lock row
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
            if user is not None:
                await self._guard.record_failure(user.id)
            logger.info(
                "mobile login verify rejected reason=wrong_code phone=%s",
                mask_phone(normalized),
            )
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        await db.delete(challenge)
        await db.flush()
        if user is None:
            # The ENTRY branch: the phone just passed a code check and
            # owns no account — mint the 5-minute single-use register
            # proof (hashed at rest; GETDEL consumes it at register).
            register_token = secrets.token_urlsafe(32)
            await self._redis.set(
                _REGISTER_KEY_PREFIX + _hash_text(register_token),
                normalized,
                ex=REGISTER_TOKEN_TTL_SECONDS,
            )
            logger.info("mobile entry token issued phone=%s", mask_phone(normalized))
            return MobileVerifyOutcome(register_token=register_token)
        assert user is not None  # every accountless path returned above
        await self._guard.reset(user.id)
        _, tokens = await self._sessions.issue_session(db, user=user, now=now)
        new_cookie, new_slot = (
            await issue_device(db, user, clock=self._clock)
            if remember_device
            else ("", "")
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
        return MobileVerifyOutcome(
            tokens=tokens,
            device_cookie=new_cookie or None,
            device_slot=new_slot or None,
        )

    async def register_by_phone(
        self,
        db: AsyncSession,
        *,
        register_token: str,
        student_number: str,
        password: str | None = None,
        nickname: str | None = None,
    ) -> User:
        """Trade the ENTRY proof for a PENDING_APPROVAL account with the
        phone bound (the queue-jump batch).

        The proof is single-use (GETDEL): a replay answers the uniform
        MOBILE_CODE_INVALID. The student number carries the classic
        register validations (band/normalization) and the database's
        unique indexes adjudicate any race; the account lands in
        PENDING_APPROVAL exactly like the /auth/register door. The
        session mint rides the CALLER (the router composes the
        register-ends-logged-in response)."""
        from sqlalchemy.exc import IntegrityError

        from app.core.error_codes import ErrorCode as _ErrorCode
        from app.modules.identity.enums import Role as _Role
        from app.modules.identity.enums import UserStatus as _Status
        from app.modules.identity.validation import validate_student_number

        phone = await self._redis.getdel(
            _REGISTER_KEY_PREFIX + _hash_text(register_token)
        )
        if not isinstance(phone, str):
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        number = validate_student_number(student_number)
        nickname_value = (
            number if nickname is None or nickname.strip() == "" else nickname.strip()
        )
        from app.core.security import hash_password as _hash
        from app.core.security import validate_password_length

        password_hash: str | None = None
        if password is not None and password != "":
            validate_password_length(password)
            password_hash = _hash(password)

        if await self._users.find_by_phone_e164(db, phone) is not None:
            raise BusinessError(
                ErrorCode.PHONE_ALREADY_BOUND,
                "该手机号已绑定其他账号",
                status_code=409,
            )

        user = User(
            username=number,
            password_hash=password_hash,
            nickname=nickname_value,
            phone_e164=phone,
            role=_Role.STUDENT,
            status=_Status.PENDING_APPROVAL,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            violation = str(exc.orig) if exc.orig is not None else str(exc)
            if "uq_users_username" in violation:
                raise BusinessError(
                    _ErrorCode.USERNAME_ALREADY_EXISTS,
                    "该学号已注册，请直接登录",
                    status_code=409,
                ) from exc
            if "uq_users_phone_e164" in violation:
                raise BusinessError(
                    _ErrorCode.PHONE_ALREADY_BOUND,
                    "该手机号已绑定其他账号",
                    status_code=409,
                ) from exc
            raise
        logger.info("mobile entry registered user_id=%s", user.id)
        return user

    # --- the staff channel (V1.1 login iteration item 1) ------------------

    async def request_staff_code(self, db: AsyncSession, phone: str) -> None:
        """SMS one login code to a STAFF-bound phone — or nothing at all.

        The router answers the constant 202; this method decides what
        actually happens: the cooldown arms for EVERY phone (same NX
        discipline as the student channel), and a code is minted and
        sent only when the phone binds an ACTIVE TEACHER/ADMIN. A
        student-bound or unknown phone stays silent — staff existence is
        never an answer.
        """
        try:
            normalized = normalize_phone(phone, "CN")
        except InvalidPhoneError:
            return
        cooldown_key = _STAFF_COOLDOWN_KEY_PREFIX + normalized
        armed = await self._redis.set(cooldown_key, "1", ex=COOLDOWN_SECONDS, nx=True)
        if not armed:
            raise BusinessError(
                ErrorCode.RATE_LIMITED,
                "验证码请求过于频繁，请稍后再试",
                status_code=429,
            )
        user = await self._users.find_by_phone_e164(db, normalized)
        if (
            user is None
            or user.role == Role.STUDENT.value
            or user.status != UserStatus.ACTIVE.value
        ):
            return  # silent: the same 202, nothing stored or sent

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
            await db.commit()
            # The failed attempt burns no cooldown (the student leg's
            # discipline): release the arm so the retry is not 429-blocked.
            await self._redis.delete(_STAFF_COOLDOWN_KEY_PREFIX + normalized)
            logger.warning(
                "staff mobile code send failed phone=%s", mask_phone(normalized)
            )
            raise BusinessError(
                ErrorCode.EMAIL_DELIVERY_FAILED,  # the shared delivery-failure code
                _DELIVERY_FAILED_MESSAGE,
                status_code=502,
            ) from exc

        # COMMIT BOUNDARY: the 202 must leave the challenge durable for
        # the next request/connection (the email-service hotfix lesson —
        # the e2e-caught second instance; the suite's savepoint sessions
        # cannot see this defect, only a fresh connection can).
        challenge_id = challenge.id
        await db.commit()
        logger.info(
            "staff mobile code issued phone=%s challenge_id=%s",
            mask_phone(normalized),
            challenge_id,
        )

    async def verify_staff_code(
        self,
        db: AsyncSession,
        *,
        phone: str | None,
        code: str | None,
        device_cookies: dict[str, str] | None = None,
        remember_device: bool = False,
        audit_context: AuditContext | None = None,
    ) -> StaffMobileCodeOutcome:
        """Trade the staff SMS code for a staff session — or, for an
        ADMIN, the pending TOTP leg.

        The code leg is the student channel's craft with a staff
        filter; TEACHER completes in one step, ADMIN gets the opaque
        single-use pending token (30 minutes, Redis, hashed at rest —
        the email-login F1 discipline). Every failure is the uniform
        MOBILE_CODE_INVALID. The empty-code cookie leg this service
        once carried is RETIRED (2026-10-24): the unified facade owns
        the cookie-as-credential leg, and this service's only caller
        never sends an empty code. ``device_cookies`` rides the
        signature for honoring-armed callers; this leg does not honor.
        """
        assert self._sessions is not None and self._guard is not None, (
            "verify legs require sessions and guard wiring"
        )

        def _invalid() -> BusinessError:
            return BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        if code is None or code.strip() == "":
            # The cookie-as-credential leg lives in the unified facade
            # (the empty-body verify); an empty code here is the
            # uniform invalid, no oracle.
            raise _invalid()
        if phone is None:
            raise _invalid()
        try:
            normalized = normalize_phone(phone, "CN")
        except InvalidPhoneError as exc:
            raise _invalid() from exc
        user = await self._users.find_by_phone_e164(db, normalized)
        if (
            user is None
            or user.role == Role.STUDENT.value
            or user.status != UserStatus.ACTIVE.value
        ):
            raise _invalid()
        if await self._guard.is_locked(user.id):
            raise await self._account_locked(user.id)

        now = self._clock.now()
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
            raise _invalid()
        submitted_hash = self._hash_code(code.strip())
        if not hmac.compare_digest(submitted_hash, challenge.code_hash):
            challenge.attempts += 1
            await db.commit()  # the M-1 discipline: persist before raising
            await self._guard.record_failure(user.id)
            logger.info(
                "staff mobile code rejected user_id=%s reason=wrong_code", user.id
            )
            raise _invalid()

        await db.delete(challenge)
        await db.flush()
        await self._guard.reset(user.id)

        if Role(user.role) is Role.ADMIN:
            pending_token = secrets.token_urlsafe(32)
            await self._redis.set(
                _staff_pending_key(_hash_text(pending_token)),
                str(user.id),
                ex=STAFF_PENDING_TTL_SECONDS,
            )
            logger.info("staff mobile login pending totp user_id=%s", user.id)
            return StaffMobileCodeOutcome(pending_token=pending_token)
        return await self._staff_login_succeeded(
            db,
            user,
            remember_device=remember_device,
            channel="staff_mobile_code",
            audit_context=audit_context,
        )

    async def mint_staff_pending(self, user_id: UUID) -> str:
        """PR-1's unified password leg: mint the SAME opaque, single-use,
        30-minute staff pending credential the staff mobile-code totp
        leg consumes — for an ADMIN whose password just proved but whose
        second factor is owed. No challenge is involved (the first
        factor was the password), so there is nothing to cool down."""
        pending_token = secrets.token_urlsafe(32)
        await self._redis.set(
            _staff_pending_key(_hash_text(pending_token)),
            str(user_id),
            ex=STAFF_PENDING_TTL_SECONDS,
        )
        return pending_token

    async def complete_staff_totp(
        self,
        db: AsyncSession,
        *,
        pending_token: str,
        code: str,
        remember_device: bool = False,
        audit_context: AuditContext | None = None,
    ) -> tuple[SessionTokens, str | None, str | None]:
        """The ADMIN second leg: opaque pending token + a current TOTP
        (or recovery — the authenticate_staff discipline) opens the
        session. The pending credential is single-use (GETDEL).
        Returns ``(tokens, cookie value, cookie slot)`` — the cookie
        pair is None unless the response should set one."""
        assert self._sessions is not None
        user_id_raw = await self._redis.getdel(
            _staff_pending_key(_hash_text(pending_token))
        )
        if not isinstance(user_id_raw, str):
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)
        user = await db.get(User, UUID(user_id_raw))
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        from app.modules.identity.models import TotpCredential
        from app.modules.identity.totp import decrypt_totp_secret, verify_totp_code

        credential = await db.get(TotpCredential, user.id)
        if credential is None or credential.confirmed_at is None:
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)
        assert self._fernet is not None, "staff totp leg requires fernet wiring"
        secret = decrypt_totp_secret(self._fernet, credential.secret_encrypted)
        if not verify_totp_code(secret, code, at=self._clock.now()):
            # The pending token was consumed by the GETDEL above — the
            # client restarts from verify (fail closed, like the OTP
            # proofs: no refund of single-use credentials).
            logger.info("staff mobile totp rejected user_id=%s", user.id)
            raise BusinessError(ErrorCode.MOBILE_CODE_INVALID, _CODE_INVALID_MESSAGE)

        outcome = await self._staff_login_succeeded(
            db,
            user,
            remember_device=remember_device,
            channel="staff_mobile_code+totp",
            audit_context=audit_context,
        )
        assert outcome.tokens is not None
        return outcome.tokens, outcome.device_cookie, outcome.device_slot

    async def _staff_login_succeeded(
        self,
        db: AsyncSession,
        user: User,
        *,
        remember_device: bool,
        channel: str,
        audit_context: AuditContext | None,
    ) -> StaffMobileCodeOutcome:
        assert self._sessions is not None
        _, tokens = await self._sessions.issue_session(
            db, user=user, now=self._clock.now()
        )
        new_cookie, new_slot = (
            await issue_device(db, user, clock=self._clock)
            if remember_device
            else ("", "")
        )
        await self._audit.append(
            db,
            actor=Actor(user_id=user.id, role=Role(user.role)),
            action=AUDIT_MOBILE_LOGIN_SUCCEEDED,
            target_type=_AUDIT_TARGET_TYPE,
            target_id=str(user.id),
            after_snapshot={"channel": channel},
            ip_address=audit_context.ip_address if audit_context else None,
            request_id=audit_context.request_id if audit_context else None,
        )
        await db.commit()
        logger.info("mobile login succeeded user_id=%s channel=%s", user.id, channel)
        return StaffMobileCodeOutcome(
            tokens=tokens,
            device_cookie=new_cookie or None,
            device_slot=new_slot or None,
        )
