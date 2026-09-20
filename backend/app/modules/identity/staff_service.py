# backend/app/modules/identity/staff_service.py
"""Staff invitations and mandatory TOTP 2FA onboarding (spec §5.6, §5.8).

`StaffService` owns the staff lifecycle: Admin creates a one-shot,
short-lived invitation; the invitee trades the single-use token for an
Argon2id password and a PENDING session; that session is not a management
session until one valid TOTP code confirms the credential; only then does
`authenticate_staff` (verified email + password + TOTP-or-recovery-code)
open a normal session.

Design decisions:

- **Invitation tokens are 256-bit secrets stored as SHA-256 digests** —
  the exact scheme refresh tokens use (`app.core.security`): deterministic
  digest as the UNIQUE-index lookup key, infeasible-to-brute-force entropy
  by construction, so no key stretching is needed. The plaintext token
  exists only in `create_staff_invitation`'s return value (never
  persisted, never logged).
- **Single-use and expiry are enforced under a row lock in one
  transaction**: `accept_staff_invitation` selects the invitation
  ``FOR UPDATE``, checks ``accepted_at``/``expires_at``, and consumes it in
  the same transaction that creates the user and mints the pending
  session — two concurrent accepts of one token serialize, and the loser
  fails without creating anything.
- **The pending session is a real, revocable session marked
  ``must_setup_totp``**, not a crippled token: identity was just proven by
  possession of the single-use link plus a fresh password, and every JWT
  claim stays truthful (no fabricated session ids). It is NOT a management
  session — `authenticate_staff` refuses the account until TOTP is
  confirmed (`TotpSetupRequiredError`), and the identity dependencies
  enforce the same two-factor state on management endpoints (spec §5.8
  step 3). Keeping the refresh capability means an interrupted setup
  (closed tab, lost phone) can resume within the refresh window. PAST
  the refresh window, an account whose TOTP was never confirmed is
  stranded — login refuses it and setup needs a live session — and only
  a future admin reset/re-invite flow can recover it; no such tool
  exists in V1 (deliberate scope cut, not an oversight).
- **Email is verified-on-acceptance (V1 simplification, documented)**: the
  invitation link was delivered to that address, and only its recipient
  can consume the single-use token, so `email_verified_at` is set at
  accept time instead of a separate verification round-trip (spec §5.5's
  full email-verification flow is student-facing). The staff
  login identifier is this verified email (a deliberate choice fixing one
  way, spec §5.8): staff usernames ARE the normalized email, which can
  never collide with a 6-20 digit student number, so staff cannot
  impersonate student identities.
- **TOTP secrets rest encrypted** (Fernet under
  `Settings.totp_encryption_key`; see `totp.py` for the primitive choice),
  **recovery codes rest as Argon2id hashes** and are returned exactly
  once. Confirmation requires one valid RFC 6238 code before
  ``confirmed_at`` is set (spec §5.8).
- **Recovery codes substitute for the TOTP code at staff login exactly
  once**: the user's unused rows are locked ``FOR UPDATE`` and the match
  is consumed inside the login transaction, so a replayed code fails
  atomically.
- **Failures are uniform where they could enumerate**: unknown/expired/
  used invitation tokens and wrong password/TOTP/recovery inputs all
  raise the same ``AUTHENTICATION_REQUIRED``; the unknown-identifier path
  still burns one Argon2 verify (timing shield). Wrong second-factor
  attempts are counted via server-side rejection logs (rate limiting is
  deliberately deferred; the login already requires the password).
- **Audit events** (`events.py`) are published inside the transaction,
  before commit, so the audit/outbox module's `AuditService` can persist
  them atomically; the in-memory collector is the interim adapter.

Error taxonomy: `BusinessError` with existing registry codes
(`PERMISSION_DENIED`, `VALIDATION_ERROR`, `AUTHENTICATION_REQUIRED`,
`ACCOUNT_NOT_ACTIVE`, `USERNAME_ALREADY_EXISTS`); `TotpSetupRequiredError`
is a module-level exception (like `otp.py`'s taxonomy) because the frozen
§29 registry has no setup-required code — the router maps it.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import (
    hash_password,
    hash_refresh_token,
    timing_shield_hash,
    validate_password_length,
    verify_password,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import (
    STAFF_INVITATION_ACCEPTED,
    STAFF_INVITATION_CREATED,
    TOTP_ENABLED,
    Actor,
    DomainEvent,
    DomainEventPublisher,
)
from app.modules.identity.models import (
    RecoveryCode,
    StaffInvitation,
    TotpCredential,
    User,
)
from app.modules.identity.repository import UserRepository
from app.modules.identity.session_service import SessionService, SessionTokens
from app.modules.identity.totp import (
    build_otpauth_uri,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    recovery_code_matches,
    verify_totp_code,
)

logger = logging.getLogger(__name__)

_PERMISSION_DENIED_MESSAGE = "仅管理员可以创建员工邀请"
_VALIDATION_MESSAGE = "员工邀请信息校验失败"
_AUTHENTICATION_MESSAGE = "邀请链接无效、已过期或已被使用"
_STAFF_AUTHENTICATION_MESSAGE = "邮箱、密码或动态验证码错误"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许登录"
_EMAIL_TAKEN_MESSAGE = "该邮箱已被其他账号使用"
_ALREADY_BOUND_MESSAGE = "该账号已绑定 TOTP，不能重复绑定"
_NOT_STARTED_MESSAGE = "尚未开始 TOTP 绑定"
_WRONG_CODE_MESSAGE = "动态验证码错误"
_EMAIL_MAX_LENGTH = 320


class TotpSetupRequiredError(Exception):
    """Staff login attempted before the mandatory TOTP was confirmed.

    Raised only after email AND password verified, for a TEACHER/ADMIN
    account whose credential is missing or unconfirmed — the caller must
    complete `begin_totp_setup`/`confirm_totp_setup` (spec §5.8 step 3).
    The router maps this to its HTTP response; no registry code
    exists for it yet (frozen §29 table).
    """


@dataclass(frozen=True, slots=True)
class IssuedStaffInvitation:
    """A persisted invitation plus its one-time token.

    The row carries only ``token_hash``; ``token`` exists exclusively in
    this return value so the caller can build the invitation link once.
    """

    invitation: StaffInvitation
    token: str


@dataclass(frozen=True, slots=True)
class PendingStaffSession:
    """Identity proof granted by accepting an invitation — not management.

    ``must_setup_totp`` is the contract marker: the tokens are a real
    (revocable) session pair usable for the TOTP setup endpoints, but the
    account cannot pass `authenticate_staff` (or the management
    dependencies) until the credential's ``confirmed_at`` is set.
    """

    user_id: UUID
    role: Role
    tokens: SessionTokens
    must_setup_totp: bool = True


@dataclass(frozen=True, slots=True)
class TotpSetup:
    """A freshly generated (not yet confirmed) TOTP credential secret.

    ``secret`` and the QR-ready ``otpauth_uri`` are returned exactly once
    for display; only the Fernet ciphertext rests in the database.
    """

    secret: str
    otpauth_uri: str


class StaffService:
    """Staff invitation, TOTP onboarding, and second-factor login (§5.8)."""

    def __init__(
        self,
        *,
        clock: Clock,
        sessions: SessionService,
        fernet: Fernet,
        events: DomainEventPublisher,
        invitation_ttl_hours: int = 48,
    ) -> None:
        # The clock is the only business-time source (expiry, verified-at,
        # confirmed-at); `sessions` shares the same instance at the
        # composition root. Fernet is built from Settings'
        # sentinel-guarded `totp_encryption_key`.
        self._clock = clock
        self._sessions = sessions
        self._fernet = fernet
        self._events = events
        self._invitation_ttl = timedelta(hours=invitation_ttl_hours)
        self._users = UserRepository()

    async def create_staff_invitation(
        self, db: AsyncSession, actor: Actor, email: str, role: Role
    ) -> IssuedStaffInvitation:
        """Create a one-shot invitation for ``email`` into staff ``role``.

        Only an ADMIN actor may invite (spec §5.8); STUDENT/TEACHER are
        denied before anything is written. The target role must be
        TEACHER or ADMIN — students self-register through the whitelist
        flow, never through invitations.
        """
        if actor.role != Role.ADMIN:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _PERMISSION_DENIED_MESSAGE,
                status_code=403,
            )
        if role not in (Role.TEACHER, Role.ADMIN):
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "role", "reason": "must be TEACHER or ADMIN"},
            )
        email_normalized = self._normalized_email(email)

        now = self._clock.now()
        token = secrets.token_urlsafe(32)
        invitation = StaffInvitation(
            email_normalized=email_normalized,
            role=role.value,
            token_hash=hash_refresh_token(token),
            expires_at=now + self._invitation_ttl,
            created_by=actor.user_id,
        )
        db.add(invitation)
        await db.flush()
        self._events.publish(
            DomainEvent(
                event_type=STAFF_INVITATION_CREATED,
                aggregate_type="StaffInvitation",
                aggregate_id=invitation.id,
                occurred_at=now,
                payload={
                    "email": email_normalized,
                    "role": role.value,
                    "invited_by": str(actor.user_id),
                    "expires_at": invitation.expires_at.isoformat(),
                },
            )
        )
        invitation_id = invitation.id
        await db.commit()
        logger.info(
            "staff invitation created invitation_id=%s role=%s actor_id=%s",
            invitation_id,
            role.value,
            actor.user_id,
        )
        return IssuedStaffInvitation(invitation=invitation, token=token)

    async def accept_staff_invitation(
        self, db: AsyncSession, token: str, password: str
    ) -> PendingStaffSession:
        """Trade the single-use invitation token for a staff account.

        Validation order is load-bearing (the registration principle): the
        password band is checked BEFORE the invitation is looked up, so a
        malformed request never burns the one-time token. Unknown, expired,
        and already-used tokens fail with one uniform error — the branch
        reason never leaves the server.

        The consumption (``accepted_at``), the User insert, and the
        pending session mint share one transaction under the invitation
        row lock: single-use is atomic, and a crashed accept leaves
        neither a half-created account nor a consumed invitation.
        """
        self._require_usable_password(password)

        now = self._clock.now()
        invitation = await db.scalar(
            select(StaffInvitation)
            .where(StaffInvitation.token_hash == hash_refresh_token(token))
            .with_for_update()
        )
        if invitation is None or invitation.accepted_at is not None:
            logger.info("staff invitation rejected reason=unknown_or_used")
            raise self._invitation_rejected()
        if invitation.expires_at <= now:
            logger.info(
                "staff invitation rejected invitation_id=%s reason=expired",
                invitation.id,
            )
            raise self._invitation_rejected()

        email = invitation.email_normalized
        if await self._users.find_by_username(db, email) is not None:
            raise BusinessError(
                ErrorCode.USERNAME_ALREADY_EXISTS,
                _EMAIL_TAKEN_MESSAGE,
                status_code=409,
            )

        user = User(
            username=email,
            password_hash=hash_password(password),
            nickname=self._nickname_for_email(email),
            email_normalized=email,
            email_verified_at=now,
            role=invitation.role,
            status=UserStatus.ACTIVE,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError as exc:
            # Race on the email/username unique index (a concurrent
            # registration or invitation acceptance won it); everything
            # else surfaces as itself.
            await db.rollback()
            raise BusinessError(
                ErrorCode.USERNAME_ALREADY_EXISTS,
                _EMAIL_TAKEN_MESSAGE,
                status_code=409,
            ) from exc

        _, tokens = await self._sessions.issue_session(db, user=user, now=now)
        invitation.accepted_at = now
        role = Role(invitation.role)
        self._events.publish(
            DomainEvent(
                event_type=STAFF_INVITATION_ACCEPTED,
                aggregate_type="User",
                aggregate_id=user.id,
                occurred_at=now,
                payload={
                    "email": email,
                    "role": role.value,
                    "invitation_id": str(invitation.id),
                },
            )
        )
        # Captured pre-commit: the service must not depend on the caller's
        # session having ``expire_on_commit=False`` (see SessionService).
        user_id, invitation_id = user.id, invitation.id
        await db.commit()
        logger.info(
            "staff invitation accepted user_id=%s invitation_id=%s",
            user_id,
            invitation_id,
        )
        return PendingStaffSession(user_id=user_id, role=role, tokens=tokens)

    async def begin_totp_setup(self, db: AsyncSession, user_id: UUID) -> TotpSetup:
        """Generate and store (encrypted, unconfirmed) a new TOTP secret.

        Re-running begin before confirmation rotates the pending secret, so
        the displayed QR always matches what confirm will verify. After
        confirmation, regeneration is refused — it must go through an
        audited reset flow, not a silent overwrite.
        """
        user = await db.get(User, user_id)
        if user is None:
            raise self._staff_authentication_required()
        account_name = user.email_normalized or user.username

        now = self._clock.now()
        secret = generate_totp_secret()
        encrypted = encrypt_totp_secret(self._fernet, secret)
        credential = await db.scalar(
            select(TotpCredential)
            .where(TotpCredential.user_id == user_id)
            .with_for_update()
        )
        if credential is not None:
            if credential.confirmed_at is not None:
                raise BusinessError(
                    ErrorCode.VALIDATION_ERROR,
                    _ALREADY_BOUND_MESSAGE,
                    status_code=409,
                )
            credential.secret_encrypted = encrypted
            credential.created_at = now
        else:
            db.add(TotpCredential(user_id=user_id, secret_encrypted=encrypted))
            try:
                await db.flush()
            except IntegrityError as exc:
                # Lost a concurrent begin for the same user (PK race);
                # adopt the winner's row and install our secret so this
                # call still returns the credential it displayed.
                await db.rollback()
                credential = await db.scalar(
                    select(TotpCredential)
                    .where(TotpCredential.user_id == user_id)
                    .with_for_update()
                )
                if credential is None or credential.confirmed_at is not None:
                    raise BusinessError(
                        ErrorCode.VALIDATION_ERROR,
                        _ALREADY_BOUND_MESSAGE,
                        status_code=409,
                    ) from exc
                credential.secret_encrypted = encrypted
                credential.created_at = now
        await db.commit()
        logger.info("totp setup begun user_id=%s", user_id)
        return TotpSetup(
            secret=secret,
            otpauth_uri=build_otpauth_uri(secret, account_name=account_name),
        )

    async def confirm_totp_setup(
        self, db: AsyncSession, user_id: UUID, code: str
    ) -> list[str]:
        """Confirm the credential with one valid code; issue recovery codes.

        One valid RFC 6238 code is proof the authenticator holds the
        secret: ``confirmed_at`` is set, eight one-time codes
        (`totp.RECOVERY_CODE_COUNT`) are stored as Argon2id hashes, and the
        plaintext list is returned EXACTLY ONCE (spec §5.8 step 4). A wrong
        code changes nothing — the credential stays unconfirmed and no
        codes are issued, so a failed attempt burns nothing.
        """
        user = await db.get(User, user_id)
        if user is None:
            raise self._staff_authentication_required()
        credential = await db.scalar(
            select(TotpCredential)
            .where(TotpCredential.user_id == user_id)
            .with_for_update()
        )
        if credential is None:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _NOT_STARTED_MESSAGE, status_code=400
            )
        if credential.confirmed_at is not None:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR, _ALREADY_BOUND_MESSAGE, status_code=409
            )

        now = self._clock.now()
        secret = decrypt_totp_secret(self._fernet, credential.secret_encrypted)
        if not verify_totp_code(secret, code, at=now):
            logger.info("totp confirm rejected user_id=%s reason=wrong_code", user_id)
            raise BusinessError(
                ErrorCode.AUTHENTICATION_REQUIRED,
                _WRONG_CODE_MESSAGE,
                status_code=401,
            )

        credential.confirmed_at = now
        # Defensive: a confirmed credential blocks re-begin, so no rows
        # should exist; wiping keeps a hypothetical future reset flow from
        # coexisting with stale codes.
        await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
        codes = generate_recovery_codes()
        db.add_all(
            RecoveryCode(user_id=user_id, code_hash=hash_recovery_code(code))
            for code in codes
        )
        self._events.publish(
            DomainEvent(
                event_type=TOTP_ENABLED,
                aggregate_type="User",
                aggregate_id=user_id,
                occurred_at=now,
                payload={"recovery_codes_issued": len(codes)},
            )
        )
        await db.commit()
        logger.info("totp enabled user_id=%s recovery_codes=%d", user_id, len(codes))
        return codes

    async def authenticate_staff(
        self, db: AsyncSession, identifier: str, password: str, totp_code: str
    ) -> SessionTokens:
        """Staff login: verified email + password + valid second factor.

        ``totp_code`` accepts a current RFC 6238 code or one unused
        recovery code (the documented lockout path; spec §5.6 推荐一次性
        恢复码). A TEACHER/ADMIN account without a CONFIRMED credential
        raises `TotpSetupRequiredError` — but only after the password
        proved correct, so an attacker without it learns nothing. Student
        accounts answer with the uniform authentication failure (they have
        no staff second factor by design, and a setup-required signal
        would leak the account's role).
        """
        now = self._clock.now()
        email = identifier.strip().lower()
        user = await self._users.find_by_username(db, email)
        if user is None:
            # Uniform failure cost: burn one Argon2 verify (see
            # SessionService.login_student for the enumeration rationale).
            self._verify_shielded(password)
            raise self._staff_authentication_required()
        try:
            matched = verify_password(password, user.password_hash)
        except ValueError:
            matched = False
        if not matched:
            raise self._staff_authentication_required()
        if user.status != UserStatus.ACTIVE:
            raise BusinessError(
                ErrorCode.ACCOUNT_NOT_ACTIVE,
                _ACCOUNT_NOT_ACTIVE_MESSAGE,
                status_code=403,
            )
        if user.role == Role.STUDENT:
            raise self._staff_authentication_required()

        credential = await db.get(TotpCredential, user.id)
        if credential is None or credential.confirmed_at is None:
            raise TotpSetupRequiredError("该账号必须先绑定 TOTP 动态验证码")

        secret = decrypt_totp_secret(self._fernet, credential.secret_encrypted)
        # The second factor is a current TOTP code, or — as the documented
        # lockout path — one unused recovery code. Neither matching is the
        # uniform authentication failure; the rejection log line below is
        # the server-side count (rate limiting is deferred, see the module
        # docstring).
        if not verify_totp_code(
            secret, totp_code, at=now
        ) and not await self._consume_recovery_code(db, user.id, totp_code, now):
            logger.info(
                "staff second factor rejected user_id=%s reason=totp_or_recovery",
                user.id,
            )
            raise self._staff_authentication_required()

        session_row, tokens = await self._sessions.issue_session(db, user=user, now=now)
        user_id, session_id = user.id, session_row.id
        await db.commit()
        logger.info(
            "staff session opened user_id=%s session_id=%s", user_id, session_id
        )
        return tokens

    async def _consume_recovery_code(
        self, db: AsyncSession, user_id: UUID, code: str, now: datetime
    ) -> bool:
        """Try ``code`` against the user's unused recovery codes, once.

        The candidate rows are locked ``FOR UPDATE`` so two concurrent
        logins presenting the same code serialize: the winner marks
        ``used_at`` inside its transaction, the loser's locked re-read no
        longer matches ``used_at IS NULL`` and fails. True means consumed.
        """
        submitted = code.strip()
        rows = (
            await db.scalars(
                select(RecoveryCode)
                .where(
                    RecoveryCode.user_id == user_id,
                    RecoveryCode.used_at.is_(None),
                )
                .with_for_update()
            )
        ).all()
        for row in rows:
            if recovery_code_matches(submitted, row.code_hash):
                row.used_at = now
                await db.flush()
                logger.info(
                    "recovery code consumed user_id=%s recovery_code_id=%s",
                    user_id,
                    row.id,
                )
                return True
        return False

    @staticmethod
    def _normalized_email(value: str) -> str:
        """Casefold/trim an invitation email; minimal shape check.

        Full RFC-style validation is out of scope here: the address is
        Admin-typed and becomes an account only after the invitee
        completes the flow; the student-facing email-verification
        machinery (spec §5.5) lives in `email_verification.py`.
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
    def _nickname_for_email(email: str) -> str:
        """Display default for a staff account: the email's local part."""
        local = email.split("@", 1)[0]
        return (local or "staff")[:255]

    @staticmethod
    def _require_usable_password(password: str) -> None:
        """Enforce the §5.6 band before the invitation token is touched."""
        try:
            validate_password_length(password)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "password", "reason": str(exc)},
            ) from exc

    @staticmethod
    def _verify_shielded(password: str) -> None:
        """One doomed Argon2 verify for uniform unknown-user timing."""
        with contextlib.suppress(ValueError):
            verify_password(password, timing_shield_hash())

    @staticmethod
    def _invitation_rejected() -> BusinessError:
        return BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            _AUTHENTICATION_MESSAGE,
            status_code=401,
        )

    @staticmethod
    def _staff_authentication_required() -> BusinessError:
        return BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            _STAFF_AUTHENTICATION_MESSAGE,
            status_code=401,
        )
