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
  same refresh token serialize: the winner links and revokes the old row;
  the loser either fails (§5.6 rotate-once — the default, grace OFF) or
  re-issues the live generation through the replay envelope (grace ON).
  Insert of the successor and the revoke happen in the same
  transaction; there is no intermediate state where both generations are
  independently live (or neither).
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

from cryptography.fernet import Fernet, InvalidToken
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
# Domain separation for the replay envelope plaintext: one Fernet key,
# distinct purpose strings (the TOTP secrets use their own prefix in
# totp.py), so an envelope can never be confused with another secret.
_REPLAY_ENVELOPE_PREFIX = "replay:v1:"
# Pathological-loop guard for chain walks; realistic races are one or
# two generations deep.
_CHAIN_HOP_BOUND = 8
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
        refresh_grace_seconds: int = 0,
        replay_crypt: Fernet | None = None,
    ) -> None:
        self._clock = clock
        self._codec = access_codec
        self._refresh_ttl = timedelta(days=refresh_token_ttl_days)
        self._grace = timedelta(seconds=refresh_grace_seconds)
        self._replay_crypt = replay_crypt
        if self._grace.total_seconds() > 0 and replay_crypt is None:
            raise ValueError(
                "refresh_grace_seconds > 0 requires replay_crypt (the "
                "envelope could not be written or resolved otherwise)"
            )
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
        the same token serialize and exactly one rotation wins. With the
        grace window OFF (the default), the loser sees `replaced_by` set
        and fails — §5.6 rotate-once. With the window ON, the loser
        instead re-issues the live generation via the envelope chain
        (`_stable_replay`), converging the race instead of failing it.
        Unknown, expired, revoked, and out-of-window tokens raise the
        same error — the branch reason never leaves the server.
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
        # The PRESENTED credential's own expiry is checked before any
        # grace logic: an expired token stays expired even when it was
        # retired recently and a live successor exists (review P1).
        if current.expires_at <= now:
            logger.info(
                "session rotate rejected session_id=%s reason=expired",
                current.id,
            )
            raise self._authentication_required()
        if current.revoked_at is not None or current.replaced_by is not None:
            replayed = await self._stable_replay(db, current, now)
            if replayed is None:
                logger.info(
                    "session rotate rejected session_id=%s reason=already_replaced",
                    current.id,
                )
                raise self._authentication_required()
            # Stable-successor replay (PR #10 rework): the presented
            # token was retired moments ago by a concurrent client.
            # Resolve through the envelope chain to the CURRENT live
            # generation and re-issue it — no rotation, so concurrent
            # callers converge on one live lineage instead of
            # invalidating each other's freshly issued credentials.
            logger.info(
                "session rotate replay-resumed live_session_id=%s "
                "retired_session_id=%s",
                replayed[1].id,
                current.id,
            )
            await db.commit()
            return replayed[0]

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
        current.replaced_at = now
        crypt = self._replay_crypt
        if self._grace.total_seconds() > 0 and crypt is not None:
            # The stable-successor envelope: this row's within-window
            # replays re-issue the successor instead of rotating again.
            current.replay_envelope = crypt.encrypt(
                (_REPLAY_ENVELOPE_PREFIX + tokens.refresh_token).encode()
            ).decode()
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

    async def _stable_replay(
        self, db: AsyncSession, presented: UserSession, now: datetime
    ) -> tuple[SessionTokens, UserSession] | None:
        """Re-issue the CURRENT live generation for a within-window
        replay of a retired token, else ``None`` (caller fails the
        rotation).

        The PR #10 review invariant: concurrent callers must converge
        on ONE still-live refresh generation — replaying a retired
        token must resolve to the same live successor the original
        rotation issued, NOT rotate the tip again (which would instantly
        invalidate the earlier caller's access token and cookie).

        Resolution walks the ENVELOPE chain: each retired row carries
        its successor's refresh secret Fernet-encrypted; every hop is
        verified by hash lookup under ``FOR UPDATE``. The terminating
        live row's secret is returned with a freshly minted access
        token bound to that same row (access tokens are stateless JWTs
        — several may name one live session). No row is written.

        Eligibility — ALL must hold:
        - grace enabled (``REFRESH_GRACE_SECONDS > 0``);
        - the row was RETIRED by rotation (``replaced_by`` set): a
          logout-revoked row (``revoked_at`` only) never replays;
        - ``replaced_at`` recorded and within the window (legacy NULL
          rows fail closed);
        - the resolved live row is unrevoked, unreplaced, unexpired,
          and its account is ACTIVE (a logout-revoked tip refuses —
          grace can never resurrect a logged-out lineage).

        The hop bound is a pathological-loop guard only: realistic
        races retire one or two generations back.
        """
        if self._grace.total_seconds() <= 0 or presented.replaced_by is None:
            return None
        if presented.replaced_at is None:
            return None
        if now - presented.replaced_at > self._grace:
            return None

        row: UserSession | None = presented
        secret: str | None = None
        for _ in range(_CHAIN_HOP_BOUND):
            if row is None:
                return None
            secret = self._decrypt_envelope(row)
            if secret is None:
                return None
            successor = await db.scalar(
                select(UserSession)
                .where(UserSession.refresh_token_hash == hash_refresh_token(secret))
                .with_for_update()
            )
            if successor is None:
                return None
            if successor.revoked_at is not None and successor.replaced_by is None:
                # The lineage was logged out: dead is dead.
                return None
            row = successor
            if row.replaced_by is None:
                break
        if (
            row is None
            or secret is None
            or row.revoked_at is not None
            or row.replaced_by is not None
            or row.expires_at <= now
        ):
            return None

        user = await db.scalar(select(User).where(User.id == row.user_id))
        if user is None or user.status != UserStatus.ACTIVE:
            # Deliberate asymmetry vs the live path: a NON-ACTIVE account
            # gets a typed 403 on a LIVE presentation but a uniform 401
            # here — a replay must never become an account-state oracle
            # (the presented token's holder already failed primary auth).
            return None
        access_token = self._codec.encode(
            user_id=user.id,
            session_id=row.id,
            role=Role(user.role).value,
            now=now,
        )
        return SessionTokens(access_token=access_token, refresh_token=secret), row

    def _decrypt_envelope(self, row: UserSession) -> str | None:
        """The successor secret inside ``row``'s envelope, or ``None``.

        Any shape mismatch (missing envelope, undecryptable ciphertext,
        wrong domain prefix) is a plain refusal — never an error path,
        matching the uniform authentication failure discipline.
        """
        if not row.replay_envelope or self._replay_crypt is None:
            return None
        try:
            plaintext = self._replay_crypt.decrypt(
                row.replay_envelope.encode()
            ).decode()
        except InvalidToken:
            return None
        if not plaintext.startswith(_REPLAY_ENVELOPE_PREFIX):
            return None
        return plaintext[len(_REPLAY_ENVELOPE_PREFIX) :]

    async def revoke_session(self, db: AsyncSession, refresh_token: str) -> None:
        """Revoke the live session lineage named by ``refresh_token``.

        Idempotent by design: an unknown token (never issued, or
        garbage from a stale client) is a silent no-op, so a
        double-clicked logout or a cleared-cookie client never errors.

        Logout follows the ``replaced_by`` chain to the LIVE tip and
        revokes THAT (review P0-2): a client holding a retired
        generation — exactly what rotation races leave in cookie jars —
        must never see its logout silently no-op while a later
        generation keeps the account signed in. Rows are never
        deleted; every hop is locked ``FOR UPDATE``.
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
        # The walk is deliberately UNBOUNDED: a long-lived lineage earns
        # one row per refresh (15-minute access TTL -> dozens of
        # generations per day), and a hop cap here would fail OPEN — the
        # truncated walk lands on a retired row and logout silently
        # no-ops while the real tip stays signed in (review P1, reproduced
        # on PG with a 12-generation chain). `replaced_by` is set exactly
        # once, always forward to a NEWER row, so the chain is acyclic
        # and the forward lock order cannot deadlock; the depth guard
        # below only LOUDLY reports the impossible.
        hops = 0
        while row.replaced_by is not None:
            successor = await db.get(UserSession, row.replaced_by, with_for_update=True)
            if successor is None:
                logger.error(
                    "logout chain broken at session_id=%s — revoking the "
                    "last reachable row",
                    row.id,
                )
                break
            row = successor
            hops += 1
            if hops > _CHAIN_HOP_BOUND:
                logger.error(
                    "logout chain depth exceeded %d at session_id=%s "
                    "(pathological, continuing)",
                    _CHAIN_HOP_BOUND,
                    row.id,
                )
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
