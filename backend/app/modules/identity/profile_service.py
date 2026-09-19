# backend/app/modules/identity/profile_service.py
"""Student profile, contacts, and account recovery (spec §5.3-5.6; Task 8).

`ProfileService` owns the four flows the plan froze for this task:
nickname change (§5.3), phone change (§5.4), password recovery via the
bound phone (§5.6), and the Task-5-deferred `change_password`.

Design decisions:

- **Re-authentication precedes every side effect.** Phone change, email
  unbind, and password change verify the CURRENT Argon2id password before
  anything else runs (spec §5.4 再验证身份): a wrong password costs one
  Argon2 verify and raises the same `AUTHENTICATION_REQUIRED` 401 whether
  the account row exists or not — no ordering side effects, no SMS, no
  token issuance.
- **Phone change is two-phase and the old number survives phase one.**
  `request_phone_change` only issues a PHONE_CHANGE-purpose OTP challenge
  to the NEW number (after the friendly bound-elsewhere pre-check on the
  normalized E.164 form); `user.phone_e164` is untouched until
  `confirm_phone_change` consumes the challenge and swaps the column in
  one transaction. The pre-check is UX only — spec §5.4 最终唯一性仍由
  数据库 UNIQUE 保证: the confirmation UPDATE flushes inside the
  transaction, so a phone bound between request and confirm (or by a
  concurrent confirmation) surfaces as `IntegrityError` mapped to
  `PHONE_ALREADY_BOUND`, and the concurrent loser's transaction rolls back
  with its old phone intact. The user row is re-selected `FOR UPDATE` at
  confirmation so two changes of the SAME account serialize.
- **OTP proofs are purpose-bound.** Both confirm steps resolve the OTP
  through `verify_phone_challenge` + `verify_phone_token` and reject a
  proof whose purpose is not PHONE_CHANGE / PASSWORD_RESET: a code the
  user received for registration can never authorize a phone swap or a
  password reset (the token carries the purpose since this task).
- **Password reset is phone-factor only and enumeration-free.** Only
  STUDENT accounts use it — staff get `PasswordResetNotAllowedError`
  (their recovery is an admin flow, out of V1 scope). An UNKNOWN username
  — or a student row without a bound phone — returns a decoy
  `ChallengePublic` (fresh uuid, the same TTL window) instead of an error:
  the response shape is byte-for-byte what a real request returns, no SMS
  goes out, and confirming against the decoy fails exactly like any other
  unknown challenge (`UnknownChallengeError`). Response CONTENT is
  uniform; wall-clock timing of the SMS send is inherently observable and
  is not claimed.
- **Band validation never burns a single-use proof** (the registration
  ordering principle): `confirm_password_reset` checks the 10-128 band
  BEFORE touching the OTP, so a rejected new password leaves the challenge
  consumable.
- **Reset/change commit hash and revocations atomically.** The verifier
  UPDATE is flushed, then `SessionService.revoke_all` /
  `revoke_all_except` runs on the SAME session — its commit lands both.
  Spec §5.6 (修改密码、找回密码后 SHOULD 使旧 Refresh Session 失效):
  reset kills every session; `change_password` keeps only the caller's
  current session (``current_session_id``, the access token's ``sid``)
  alive — the documented simple choice over "revoke all + re-issue", so
  the client that made the request stays logged in.
- **Nothing sensitive is logged** (backend-engineering §15): no phone at
  all in this module's lines (the OTP service already masks), no
  passwords, no tokens.

Error taxonomy: `BusinessError` with frozen-registry codes
(`VALIDATION_ERROR`, `AUTHENTICATION_REQUIRED`, `PHONE_ALREADY_BOUND`);
OTP lifecycle errors propagate as `otp`'s typed module exceptions;
`PasswordResetNotAllowedError` is a module-level typed exception (no
registry code exists — T9 maps it doc-first, like `TotpSetupRequiredError`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import (
    hash_password,
    validate_password_length,
    verify_password,
)
from app.modules.identity.enums import Role
from app.modules.identity.models import User
from app.modules.identity.otp import (
    ChallengePublic,
    InvalidTokenError,
    OtpChallengeService,
    OtpPolicy,
    OtpPurpose,
    normalize_phone,
)
from app.modules.identity.repository import UserRepository
from app.modules.identity.session_service import SessionService
from app.modules.identity.validation import normalize_nickname

logger = logging.getLogger(__name__)

_PHONE_UNIQUE_CONSTRAINT = "uq_users_phone_e164"

_VALIDATION_MESSAGE = "资料修改校验失败"
_AUTHENTICATION_MESSAGE = "密码错误或登录状态已失效"
_PHONE_ALREADY_BOUND_MESSAGE = "该手机号已绑定其他账号"


class PasswordResetNotAllowedError(Exception):
    """Phone-factor password reset attempted by a non-student account.

    Staff (TEACHER/ADMIN) do not use the student phone-reset path; T9 maps
    this typed exception to its doc-first envelope response, the same way
    `TotpSetupRequiredError` is handled.
    """


class ProfileService:
    """Nickname, phone, and password management for an account (spec §5)."""

    def __init__(
        self,
        *,
        clock: Clock,
        otp: OtpChallengeService,
        otp_policy: OtpPolicy,
        sessions: SessionService,
        phone_default_region: str = "CN",
    ) -> None:
        # The decoy challenge in `request_password_reset` derives its expiry
        # window from the SAME `OtpPolicy` that shapes real challenges: an
        # independent knob here could drift from `policy.ttl_seconds` and the
        # drift itself would enumerate accounts (T8 review carry-forward).
        self._clock = clock
        self._otp = otp
        self._otp_policy = otp_policy
        self._sessions = sessions
        self._phone_region = phone_default_region
        self._challenge_ttl = timedelta(seconds=otp_policy.ttl_seconds)
        self._users = UserRepository()

    async def profile(self, db: AsyncSession, user_id: UUID) -> User:
        """The account row for the owner's own profile view (spec §40).

        Read-only counterpart to the state-changing methods: an
        authenticated-but-suspended account may still read its own page,
        so this deliberately carries no status gate.
        """
        return await self._require_user(db, user_id)

    async def change_nickname(
        self, db: AsyncSession, user_id: UUID, nickname: str
    ) -> User:
        """Validate and persist a new nickname (spec §5.3).

        The Task-2 `normalize_nickname` is the single authority (grapheme
        counting, control-character stripping, trim), so the persisted value
        is always its normalized output.
        """
        user = await self._require_user(db, user_id)
        user.nickname = self._validated_nickname(nickname)
        await db.commit()
        logger.info("nickname changed user_id=%s", user_id)
        return user

    async def request_phone_change(
        self,
        db: AsyncSession,
        user_id: UUID,
        password: str,
        new_raw_phone: str,
        *,
        client_ip: str,
    ) -> ChallengePublic:
        """Re-authenticate, then issue an OTP challenge to the NEW phone.

        Ordering is load-bearing (spec §5.4 已登录 + 再验证身份 + 新手机号
        OTP 验证): password verification first (no SMS on failure), then
        E.164 normalization and the friendly bound-elsewhere pre-check (a
        phone already held by ANY account — including the requester's own,
        a no-op swap — is rejected without sending), and only then the
        PHONE_CHANGE challenge. The old phone is preserved untouched here;
        the swap happens in `confirm_phone_change`.
        """
        user = await self._require_user(db, user_id)
        self._require_current_password(user, password)

        new_phone = normalize_phone(new_raw_phone, self._phone_region)
        if await self._users.find_by_phone_e164(db, new_phone) is not None:
            raise BusinessError(
                ErrorCode.PHONE_ALREADY_BOUND,
                _PHONE_ALREADY_BOUND_MESSAGE,
                status_code=409,
            )
        return await self._otp.request_phone_challenge(
            new_phone, OtpPurpose.PHONE_CHANGE, client_ip=client_ip
        )

    async def confirm_phone_change(
        self, db: AsyncSession, user_id: UUID, challenge_id: uuid.UUID, code: str
    ) -> User:
        """Consume the new-phone proof and swap `phone_e164` atomically.

        The proof must be PHONE_CHANGE-purpose (see the module docstring).
        The user row is locked FOR UPDATE so concurrent changes of one
        account serialize, and the swap flushes inside the transaction:
        the partial unique index is the final authority (spec §5.4), with
        `IntegrityError` mapped to the friendly `PHONE_ALREADY_BOUND`. A
        failed swap rolls the transaction back — the account keeps its old
        phone; the consumed OTP is deliberately NOT refunded (fail closed;
        the user re-requests).
        """
        await self._require_user(db, user_id)
        new_phone = await self._consume_phone_proof(
            challenge_id, code, OtpPurpose.PHONE_CHANGE
        )

        locked = await db.scalar(
            select(User).where(User.id == user_id).with_for_update()
        )
        if locked is None:
            raise self._authentication_required()
        locked.phone_e164 = new_phone
        try:
            await db.flush()
        except IntegrityError as exc:
            # Only the expected unique violation becomes the friendly
            # conflict; anything else surfaces as itself (§7 discipline).
            await db.rollback()
            violation = str(exc.orig) if exc.orig is not None else str(exc)
            if _PHONE_UNIQUE_CONSTRAINT not in violation:
                raise
            logger.info(
                "phone change rejected user_id=%s reason=already_bound", user_id
            )
            raise BusinessError(
                ErrorCode.PHONE_ALREADY_BOUND,
                _PHONE_ALREADY_BOUND_MESSAGE,
                status_code=409,
            ) from exc
        await db.commit()
        logger.info("phone changed user_id=%s", user_id)
        return locked

    async def request_password_reset(
        self, db: AsyncSession, username: str, *, client_ip: str
    ) -> ChallengePublic:
        """Issue a PASSWORD_RESET OTP to the account's BOUND phone (§5.6).

        Students only; staff raise `PasswordResetNotAllowedError`. Unknown
        usernames — and student rows without a bound phone — return a decoy
        challenge with the same expiry window and no SMS, so the response
        cannot enumerate accounts (see the module docstring for the timing
        caveat). Status is deliberately NOT checked: a SUSPENDED student
        may rotate the password, and login still refuses the account — the
        reset neither leaks status nor bypasses it.
        """
        user = await self._users.find_by_username(db, username.strip())
        if user is None:
            return self._decoy_challenge()
        if user.role != Role.STUDENT:
            logger.info(
                "password reset rejected user_id=%s reason=not_student", user.id
            )
            raise PasswordResetNotAllowedError("员工账号不支持手机号找回密码")
        if user.phone_e164 is None:
            # Unreachable through registration (accounts are born with a
            # verified phone); fail uniform rather than revealing state.
            return self._decoy_challenge()
        return await self._otp.request_phone_challenge(
            user.phone_e164, OtpPurpose.PASSWORD_RESET, client_ip=client_ip
        )

    async def confirm_password_reset(
        self,
        db: AsyncSession,
        challenge_id: uuid.UUID,
        code: str,
        new_password: str,
    ) -> None:
        """Consume the reset proof, rotate the hash, revoke everything.

        Band validation runs before the OTP is touched (a malformed request
        never burns a single-use proof). The proof must be PASSWORD_RESET-
        purpose and must resolve to a STUDENT account still bound to that
        phone; anything else is `InvalidTokenError` — the branch reason
        never leaves the server. The verifier update and `revoke_all`
        share one commit, so the new password and the dead sessions land
        together (spec §5.6 找回密码后 SHOULD 使旧 Refresh Session 失效).
        """
        self._require_usable_password(new_password)
        phone = await self._consume_phone_proof(
            challenge_id, code, OtpPurpose.PASSWORD_RESET
        )

        user = await self._users.find_by_phone_e164(db, phone)
        if user is None or user.role != Role.STUDENT:
            logger.info("password reset rejected reason=phone_not_student_bound")
            raise InvalidTokenError
        user.password_hash = hash_password(new_password)
        await db.flush()
        user_id = user.id
        await self._sessions.revoke_all(db, user_id)
        logger.info("password reset confirmed user_id=%s", user_id)

    async def change_password(
        self,
        db: AsyncSession,
        user_id: UUID,
        current_password: str,
        new_password: str,
        *,
        current_session_id: UUID | None = None,
    ) -> None:
        """Rotate the password after re-auth; kill other sessions (§5.6).

        Current-password verification precedes band validation (the same
        ordering discipline as login: an attacker without the current
        password learns nothing about policy). ``current_session_id`` (the
        authorizing request's ``sid``) stays live; every other session is
        revoked in the same commit that lands the new hash.
        """
        user = await self._require_user(db, user_id)
        self._require_current_password(user, current_password)
        self._require_usable_password(new_password)

        user.password_hash = hash_password(new_password)
        await db.flush()
        await self._sessions.revoke_all_except(db, user_id, current_session_id)
        logger.info(
            "password changed user_id=%s kept_session_id=%s",
            user_id,
            current_session_id,
        )

    async def _consume_phone_proof(
        self, challenge_id: uuid.UUID, code: str, expected_purpose: OtpPurpose
    ) -> str:
        """Verify the code, consume the token, enforce the purpose.

        OTP lifecycle failures (unknown/expired/consumed challenge, wrong
        code) propagate as `otp`'s typed exceptions for T9 to map. The
        two-step resolution mirrors registration: the challenge yields an
        opaque token, the token yields the certified phone + purpose.
        """
        token = await self._otp.verify_phone_challenge(challenge_id, code)
        verified = await self._otp.verify_phone_token(token.token)
        if verified.purpose != expected_purpose:
            logger.info("phone proof rejected reason=purpose_mismatch")
            raise InvalidTokenError
        return verified.phone_e164

    def _decoy_challenge(self) -> ChallengePublic:
        """A real-shaped challenge that was never backed by an OTP record.

        Fresh random id + the same TTL window as a live challenge: client-
        indistinguishable from the real return value, and any confirm
        attempt against it fails as an unknown challenge.
        """
        return ChallengePublic(
            challenge_id=uuid.uuid4(),
            expires_at=self._clock.now() + self._challenge_ttl,
        )

    async def _require_user(self, db: AsyncSession, user_id: UUID) -> User:
        user = await db.get(User, user_id)
        if user is None:
            raise self._authentication_required()
        return user

    @staticmethod
    def _require_current_password(user: User, password: str) -> None:
        """Re-authenticate the session owner against the stored verifier.

        Out-of-band lengths can never match (the band rejection becomes
        ``matched = False``), so policy leaks nothing and the failure is
        uniform — the SessionService login discipline.
        """
        try:
            matched = verify_password(password, user.password_hash)
        except ValueError:
            matched = False
        if not matched:
            raise ProfileService._authentication_required()

    @staticmethod
    def _validated_nickname(value: str) -> str:
        try:
            return normalize_nickname(value)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _VALIDATION_MESSAGE,
                status_code=400,
                details={"field": "nickname", "reason": str(exc)},
            ) from exc

    @staticmethod
    def _require_usable_password(password: str) -> None:
        """Enforce the §5.6 band (10-128, no composition rules) first."""
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
    def _authentication_required() -> BusinessError:
        return BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            _AUTHENTICATION_MESSAGE,
            status_code=401,
        )
