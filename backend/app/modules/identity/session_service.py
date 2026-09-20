# backend/app/modules/identity/session_service.py
"""Student login and refresh-session rotation (spec §5.6, §5.7).

`SessionService` owns three transactions: `login_student` (verify Argon2id,
open a server-side `UserSession` row), `rotate_refresh` (atomically replace
one refresh session with its successor), and `revoke_all` (kill every live
session of a user — the mechanism spec §5.6 mandates for password
change/reset).

Design decisions:

- **Uniform authentication failure.** Unknown username and wrong password
  raise the same `AUTHENTICATION_REQUIRED` BusinessError (same code, 401,
  same message), and the unknown-user path still runs one Argon2 verify
  against `security.timing_shield_hash()` so even the failure cost
  matches — no account enumeration via response or timing. Password
  verification precedes the status check: an attacker without the password
  learns nothing about the account's existence or state. The same uniform
  failure answers a correct password on a non-STUDENT account (mirroring
  staff_service's reverse STUDENT check): the student login door
  must never mint a password-only session for staff.
- **Rotation is one transaction guarded by a row lock.** The presented
  session row is selected `FOR UPDATE`, so two concurrent rotations of the
  same refresh token serialize: the winner links and revokes the old row,
  the loser reads `replaced_by IS NOT NULL` and fails (§5.6 replay
  detection). Insert of the successor and the revoke happen in the same
  transaction; there is no intermediate state where both tokens are live
  (or neither).
- **Only digests are persisted** (spec §5.6): the refresh token is
  `secrets.token_urlsafe(32)` in the return value only; the row stores its
  SHA-256 digest (see `app.core.security` for why SHA-256, not Argon2).
- **Expiry is Clock-driven** (backend-engineering §11): `expires_at` is
  compared against the injected business clock, never the database clock,
  so tests freeze time instead of sleeping.
- **Errors are `BusinessError` with registry codes** (`AUTHENTICATION_REQUIRED`,
  `ACCOUNT_NOT_ACTIVE`), unlike `otp.py`'s module-level taxonomy: these two
  codes already exist in the frozen §29 registry (docs/architecture/
  interfaces.md), so there is nothing doc-first to add.
- **`change_password` lives in `profile_service`**, deliberately: it needs
  the old-password check plus the reset-token flow; the §5.6 obligation
  ("修改密码、找回密码后 SHOULD 使旧 Refresh Session 失效") is discharged by
  `revoke_all`, which it calls verbatim.
- Nothing here reads settings or the environment: clock, access-token
  codec, and refresh TTL are constructor-injected; the composition root
  builds them from `Settings`.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import (
    AccessTokenCodec,
    generate_refresh_token,
    hash_refresh_token,
    timing_shield_hash,
    verify_password,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User, UserSession
from app.modules.identity.repository import UserRepository

logger = logging.getLogger(__name__)

_AUTHENTICATION_MESSAGE = "用户名或密码错误"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许登录"


@dataclass(frozen=True, slots=True)
class SessionTokens:
    """One issued token pair; plaintext exists only here (spec §5.6).

    The access token is a short-lived JWT (``sid`` names its refresh
    session); the refresh token is the bearer secret whose SHA-256 digest
    is the `UserSession` lookup key. Neither is ever logged.
    """

    access_token: str
    refresh_token: str


class SessionService:
    """Login, rotate, and revoke student sessions (spec §5.6)."""

    def __init__(
        self,
        *,
        clock: Clock,
        access_codec: AccessTokenCodec,
        refresh_token_ttl_days: int = 30,
    ) -> None:
        self._clock = clock
        self._codec = access_codec
        self._refresh_ttl = timedelta(days=refresh_token_ttl_days)
        self._users = UserRepository()

    async def login_student(
        self, db: AsyncSession, username: str, password: str
    ) -> SessionTokens:
        """Verify credentials and open one refresh session (spec §5.6).

        The username is stripped of outer whitespace only (spec §5.2 login
        rule; inner characters are never rewritten). Ordering is
        load-bearing: Argon2id verification before the status check, so a
        stranger without the password cannot probe account status. This is
        the STUDENT door: a TEACHER/ADMIN account with the correct password
        gets the same uniform authentication failure (the reverse mirror of
        ``StaffService.authenticate_staff``'s STUDENT check), so a staff
        account can never open a password-only session and reach the
        self-service endpoints without its second factor (spec §5.6, §33.4).
        """
        now = self._clock.now()
        user = await self._users.find_by_username(db, username.strip())
        if user is None:
            # Same error, same cost: burn one Argon2 verify on the shield
            # hash so the timing matches a wrong-password attempt.
            self._verify_shielded(password)
            raise self._authentication_required()

        try:
            matched = verify_password(password, user.password_hash)
        except ValueError:
            # Out-of-band length: cannot be anyone's password; keep the
            # failure uniform instead of leaking policy at login.
            matched = False
        if not matched:
            raise self._authentication_required()

        if user.status != UserStatus.ACTIVE:
            raise BusinessError(
                ErrorCode.ACCOUNT_NOT_ACTIVE,
                _ACCOUNT_NOT_ACTIVE_MESSAGE,
                status_code=403,
            )

        if user.role != Role.STUDENT:
            # Same uniform failure as a wrong password — no role oracle for
            # an attacker holding a staff password; staff login goes through
            # authenticate_staff's TOTP flow instead.
            logger.info(
                "student login rejected reason=non_student_role role=%s", user.role
            )
            raise self._authentication_required()

        session_row, tokens = await self.issue_session(db, user=user, now=now)
        # Ids for logging are captured pre-commit: the service must not
        # depend on the caller's session having ``expire_on_commit=False``
        # (accessing an expired attribute would trigger lazy IO).
        user_id, session_id = user.id, session_row.id
        await db.commit()
        logger.info("session opened user_id=%s session_id=%s", user_id, session_id)
        return tokens

    async def rotate_refresh(
        self, db: AsyncSession, refresh_token: str
    ) -> SessionTokens:
        """Replace one refresh session with a successor, atomically.

        The old row is locked `FOR UPDATE`, so concurrent presentations of
        the same token serialize and exactly one rotation wins; the loser
        sees `replaced_by` set and fails (§5.6: an old refresh token must
        never work twice). Unknown, expired, revoked, and replaced tokens
        raise the same error — the branch reason never leaves the server.
        """
        now = self._clock.now()
        token_hash = hash_refresh_token(refresh_token)
        current = await db.scalar(
            select(UserSession)
            .where(UserSession.refresh_token_hash == token_hash)
            .with_for_update()
        )
        if current is None:
            logger.info("session rotate rejected reason=unknown_token")
            raise self._authentication_required()
        if current.revoked_at is not None or current.replaced_by is not None:
            logger.info(
                "session rotate rejected session_id=%s reason=already_replaced",
                current.id,
            )
            raise self._authentication_required()
        if current.expires_at <= now:
            logger.info(
                "session rotate rejected session_id=%s reason=expired",
                current.id,
            )
            raise self._authentication_required()

        user = await db.scalar(select(User).where(User.id == current.user_id))
        if user is None:
            # FK-orphaned session: impossible barring manual surgery; fail
            # closed as an auth failure but leave a server-side trace.
            logger.error(
                "session %s references missing user %s", current.id, current.user_id
            )
            raise self._authentication_required()
        if user.status != UserStatus.ACTIVE:
            logger.info(
                "session rotate rejected session_id=%s reason=account_not_active",
                current.id,
            )
            raise BusinessError(
                ErrorCode.ACCOUNT_NOT_ACTIVE,
                _ACCOUNT_NOT_ACTIVE_MESSAGE,
                status_code=403,
            )

        successor_row, tokens = await self.issue_session(db, user=user, now=now)
        current.revoked_at = now
        current.replaced_by = successor_row.id
        # Captured pre-commit (see login_student).
        user_id, successor_id, replaced_id = user.id, successor_row.id, current.id
        await db.commit()
        logger.info(
            "session rotated user_id=%s session_id=%s replaced_session_id=%s",
            user_id,
            successor_id,
            replaced_id,
        )
        return tokens

    async def revoke_session(self, db: AsyncSession, refresh_token: str) -> None:
        """Revoke exactly the session named by ``refresh_token`` (logout).

        Idempotent by design: an unknown token (never issued, already
        rotated away, or garbage from a stale client) is a silent no-op, so
        a double-clicked logout or a cleared-cookie client never errors.
        The row is locked ``FOR UPDATE`` so a concurrent rotation of the
        same token serializes with the revocation; rows are never deleted.
        """
        now = self._clock.now()
        token_hash = hash_refresh_token(refresh_token)
        row = await db.scalar(
            select(UserSession)
            .where(UserSession.refresh_token_hash == token_hash)
            .with_for_update()
        )
        if row is None:
            logger.info("session logout no-op reason=unknown_token")
            return
        if row.revoked_at is None:
            row.revoked_at = now
        session_id = row.id
        await db.commit()
        logger.info("session revoked session_id=%s", session_id)

    async def revoke_all(self, db: AsyncSession, user_id: UUID) -> None:
        """Revoke every still-live session of ``user_id`` (spec §5.6).

        One UPDATE, one transaction. Called by password change/reset
        so old refresh sessions die with the old password; rows
        are never deleted — revocation is part of the audit trail.
        """
        revoked_count = await self._revoke(db, user_id, keep_session_id=None)
        logger.info("sessions revoked user_id=%s count=%d", user_id, revoked_count)

    async def revoke_all_except(
        self, db: AsyncSession, user_id: UUID, keep_session_id: UUID | None
    ) -> None:
        """Revoke every live session of ``user_id`` except ``keep_session_id``.

        The `change_password` variant: the caller passes the
        access token's ``sid`` so the session that AUTHORIZED the change
        stays usable while every other device is signed out. Like
        ``revoke_all`` this commits — deliberately: ``change_password``
        flushes the new Argon2id verifier first, so one commit lands the
        hash rotation and the revocations atomically (a crash between the
        two would otherwise leave old-password sessions alive).
        ``keep_session_id=None`` degrades to ``revoke_all``.
        """
        revoked_count = await self._revoke(db, user_id, keep_session_id=keep_session_id)
        logger.info(
            "sessions revoked user_id=%s kept_session_id=%s count=%d",
            user_id,
            keep_session_id,
            revoked_count,
        )

    async def _revoke(
        self, db: AsyncSession, user_id: UUID, *, keep_session_id: UUID | None
    ) -> int:
        now = self._clock.now()
        conditions = [
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        ]
        if keep_session_id is not None:
            conditions.append(UserSession.id != keep_session_id)
        result = await db.execute(
            update(UserSession).where(*conditions).values(revoked_at=now)
        )
        await db.commit()
        # `CursorResult.rowcount` exists on every driver result here, but
        # the declared `Result` type does not carry it; the log count is
        # informational, so a missing attribute degrades to 0.
        return int(getattr(result, "rowcount", 0) or 0)

    async def issue_session(
        self, db: AsyncSession, *, user: User, now: datetime
    ) -> tuple[UserSession, SessionTokens]:
        """Add one live `UserSession` row and mint its token pair.

        Flush allocates the row id (server default) so the access token's
        ``sid`` claim can name it; this method deliberately does NOT
        commit — the caller owns the transaction. `login_student` and
        `rotate_refresh` commit their own; staff onboarding
        (`StaffService.accept_staff_invitation`) composes the mint into
        the same transaction that creates the account and consumes the
        invitation, so a half-onboarded account can never rest committed.
        """
        refresh_token = generate_refresh_token()
        session_row = UserSession(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            expires_at=now + self._refresh_ttl,
        )
        db.add(session_row)
        await db.flush()
        access_token = self._codec.encode(
            user_id=user.id,
            session_id=session_row.id,
            role=Role(user.role).value,
            now=now,
        )
        return session_row, SessionTokens(
            access_token=access_token, refresh_token=refresh_token
        )

    @staticmethod
    def _verify_shielded(password: str) -> None:
        """Run one doomed Argon2 verify for uniform failure timing.

        The result is discarded and a ValueError (out-of-band length) is
        swallowed: this call exists only to spend the same CPU a
        wrong-password attempt would.
        """
        with contextlib.suppress(ValueError):
            verify_password(password, timing_shield_hash())

    @staticmethod
    def _authentication_required() -> BusinessError:
        return BusinessError(
            ErrorCode.AUTHENTICATION_REQUIRED,
            _AUTHENTICATION_MESSAGE,
            status_code=401,
        )
