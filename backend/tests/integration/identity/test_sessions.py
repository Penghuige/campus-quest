# backend/tests/integration/identity/test_sessions.py
"""Student login and refresh-session rotation against real PostgreSQL.

Spec §5.6 (密码与会话): Argon2id verification at login, short-lived access
token paired with a rotatable/revocable refresh token persisted as a
server-side `UserSession` row, and old-token reuse detected through the
`replaced_by` chain. backend-engineering §15: neither password nor token
ever reaches a log line.

Time is FrozenClock-driven (backend-engineering §11): the service enforces
refresh expiry against the injected business clock, so "expired refresh"
is a clock advance, never a sleep. The timing-shield assertion (unknown
user still pays an Argon2 verify) is deterministic — a counting wrapper
around the verify seam — because a wall-clock statistical timing test is
flaky by nature (controller decision: skipped, shield behavior still
asserted structurally).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import (
    AccessTokenCodec,
    hash_password,
    hash_refresh_token,
)
from app.modules.identity import session_service as session_service_module
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User, UserSession
from app.modules.identity.session_service import SessionService, SessionTokens

# Anchored to the real now: PyJWT validates ``exp`` against wall-clock
# time, so access tokens decoded in assertions must have been minted
# "just now". Refresh expiry is FrozenClock-driven and anchored to the
# same value for the row-equality assertions.
_T0 = datetime.now(UTC).replace(microsecond=0)
_USERNAME = "20250010001"
_PASSWORD = "correct-horse-battery"
# >= 32 bytes so PyJWT does not warn about short HS256 keys (RFC 7518 §3.2).
_ACCESS_SECRET = "integration-test-access-token-secret-0123456789"
_ACCESS_TTL_MINUTES = 15
_REFRESH_TTL_DAYS = 30
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许登录"


async def _seed_user(
    db: AsyncSession,
    *,
    username: str = _USERNAME,
    status: UserStatus = UserStatus.ACTIVE,
    role: Role = Role.STUDENT,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="测试同学",
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


def _make_service(clock: FrozenClock) -> SessionService:
    return SessionService(
        clock=clock,
        access_codec=AccessTokenCodec(
            secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
        ),
        refresh_token_ttl_days=_REFRESH_TTL_DAYS,
    )


# The stable-successor grace configuration (PR #10 rework): a real Fernet
# key so envelopes round-trip; default deployments run grace=0 (strict
# rotate-once) until the §5.6 amendment ruling flips it.
_REPLAY_KEY = Fernet.generate_key()


def _make_grace_service(clock: FrozenClock, seconds: int = 30) -> SessionService:
    return SessionService(
        clock=clock,
        access_codec=AccessTokenCodec(
            secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
        ),
        refresh_token_ttl_days=_REFRESH_TTL_DAYS,
        refresh_grace_seconds=seconds,
        replay_crypt=Fernet(_REPLAY_KEY),
    )


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


async def _session_row(db: AsyncSession, refresh_token: str) -> UserSession | None:
    return await db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == hash_refresh_token(refresh_token)
        )
    )


async def _count_unrevoked_sessions(db: AsyncSession, user_id: UUID) -> int:
    """Rows with neither revoke nor replacement recorded (DB state only).

    Expiry is a Clock comparison, not a column: an expired-but-unrevoked
    row still counts here, which is exactly what lets the tests tell
    "revoked by revoke_all" apart from "rejected by the clock".
    """
    return int(
        await db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.replaced_by.is_(None),
            )
        )
        or 0
    )


async def _count_sessions(db: AsyncSession, user_id: UUID) -> int:
    """Every session row of the account (the rotation-chain length)."""
    return int(
        await db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.user_id == user_id)
        )
        or 0
    )


@pytest.mark.integration
async def test_login_success_creates_refresh_session_and_tokens(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    user = await _seed_user(db_session)

    tokens = await _make_service(clock).login_student(db_session, _USERNAME, _PASSWORD)

    assert isinstance(tokens, SessionTokens)
    assert tokens.access_token
    assert tokens.refresh_token
    # The refresh token exists only in the return value; the row keeps its
    # SHA-256 digest (spec §5.6: server-side session, nothing reversible).
    row = await _session_row(db_session, tokens.refresh_token)
    assert row is not None
    assert row.user_id == user.id
    assert row.revoked_at is None
    assert row.replaced_by is None
    assert row.expires_at == _T0 + timedelta(days=_REFRESH_TTL_DAYS)
    # Only the digest — never the token — is persisted anywhere.
    assert tokens.refresh_token not in row.refresh_token_hash

    # The access token is a real, decodable JWT bound to user and session.
    claims = AccessTokenCodec(
        secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
    ).decode(tokens.access_token)
    assert claims.sub == str(user.id)
    assert claims.sid == str(row.id)
    assert claims.role == Role.STUDENT.value


@pytest.mark.integration
async def test_login_strips_outer_whitespace_from_username(
    db_session: AsyncSession,
) -> None:
    # Spec §5.2 login rule: leading/trailing whitespace may be removed, but
    # inner characters must never be rewritten.
    await _seed_user(db_session)

    tokens = await _make_service(FrozenClock(_T0)).login_student(
        db_session, f"  {_USERNAME}\t", _PASSWORD
    )

    assert await _session_row(db_session, tokens.refresh_token) is not None


@pytest.mark.integration
async def test_login_wrong_password_fails_without_session(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, _USERNAME, "wrong-horse-battery"
        )

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    assert await _count_unrevoked_sessions(db_session, user.id) == 0


@pytest.mark.integration
async def test_login_unknown_user_uniform_failure(
    db_session: AsyncSession,
) -> None:
    # Unknown user and wrong password must be indistinguishable: same code,
    # same status, same message (no user enumeration via the response).
    await _seed_user(db_session)

    with pytest.raises(BusinessError) as unknown_exc:
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, "20990099999", _PASSWORD
        )
    with pytest.raises(BusinessError) as wrong_exc:
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, _USERNAME, "wrong-horse-battery"
        )

    assert unknown_exc.value.code == wrong_exc.value.code
    assert unknown_exc.value.status_code == wrong_exc.value.status_code
    assert unknown_exc.value.message == wrong_exc.value.message


@pytest.mark.integration
async def test_login_unknown_user_still_pays_argon2_verify(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Structural stand-in for the (flaky, skipped) statistical timing test:
    # the unknown-user path must run one Argon2 verification against the
    # timing-shield hash so its cost matches a wrong-password attempt.
    calls: list[str] = []
    real_verify = session_service_module.verify_password

    def counting_verify(password: str, encoded: str) -> bool:
        calls.append(encoded)
        return real_verify(password, encoded)

    monkeypatch.setattr(session_service_module, "verify_password", counting_verify)
    await _seed_user(db_session)

    with pytest.raises(BusinessError):
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, "20990099999", _PASSWORD
        )

    # Exactly one Argon2 verify ran, against an Argon2id digest that is not
    # the seeded user's verifier (the shield hash salts independently).
    assert len(calls) == 1
    assert calls[0].startswith("$argon2id$")
    user = await db_session.scalar(select(User).where(User.username == _USERNAME))
    assert user is not None
    assert calls[0] != user.password_hash


@pytest.mark.integration
@pytest.mark.parametrize(
    "status",
    [UserStatus.SUSPENDED, UserStatus.BANNED, UserStatus.PENDING_PHONE],
)
async def test_login_rejected_when_not_active(
    db_session: AsyncSession, status: UserStatus
) -> None:
    # Correct password, non-ACTIVE account: the status is only revealed
    # after the password proved correct (no status oracle for strangers).
    user = await _seed_user(db_session, status=status)

    with pytest.raises(BusinessError) as exc_info:
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, _USERNAME, _PASSWORD
        )

    assert exc_info.value.code == ErrorCode.ACCOUNT_NOT_ACTIVE
    assert exc_info.value.status_code == 403
    assert exc_info.value.message == _ACCOUNT_NOT_ACTIVE_MESSAGE
    assert await _count_unrevoked_sessions(db_session, user.id) == 0


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.TEACHER, Role.ADMIN])
async def test_login_student_rejects_staff_roles_uniformly(
    db_session: AsyncSession, role: Role
) -> None:
    # Whole-branch review fix (spec §5.6, §33.4): /api/v1/auth/login is the
    # STUDENT password-only door. A TEACHER/ADMIN account presenting the
    # CORRECT password must draw the same uniform AUTHENTICATION_REQUIRED as
    # a wrong password (reverse mirror of staff_service's STUDENT check) —
    # otherwise a staff account opens a 2FA-less session and reaches the
    # password-only staff self-service endpoints (/me/email, /me/password).
    staff = await _seed_user(db_session, username="teacher@school.edu", role=role)

    with pytest.raises(BusinessError) as staff_exc:
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, "teacher@school.edu", _PASSWORD
        )
    with pytest.raises(BusinessError) as wrong_exc:
        await _make_service(FrozenClock(_T0)).login_student(
            db_session, "teacher@school.edu", "wrong-horse-battery"
        )

    assert staff_exc.value.code == wrong_exc.value.code
    assert staff_exc.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert staff_exc.value.status_code == wrong_exc.value.status_code == 401
    assert staff_exc.value.message == wrong_exc.value.message
    assert await _count_unrevoked_sessions(db_session, staff.id) == 0

    # The student door itself stays open for students.
    student = await _seed_user(db_session)
    tokens = await _make_service(FrozenClock(_T0)).login_student(
        db_session, _USERNAME, _PASSWORD
    )
    assert await _session_row(db_session, tokens.refresh_token) is not None
    assert await _count_unrevoked_sessions(db_session, student.id) == 1


@pytest.mark.integration
async def test_rotation_chain_reuse_of_old_token_fails(
    db_session: AsyncSession,
) -> None:
    # The §5.6 replay story: login -> A; rotate A -> B; reuse A must fail
    # (the row's replaced_by marks it consumed) while B still rotates.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    user = await _seed_user(db_session)

    tokens_a = await service.login_student(db_session, _USERNAME, _PASSWORD)
    tokens_b = await service.rotate_refresh(db_session, tokens_a.refresh_token)

    assert tokens_b.refresh_token != tokens_a.refresh_token

    row_a = await _session_row(db_session, tokens_a.refresh_token)
    row_b = await _session_row(db_session, tokens_b.refresh_token)
    assert row_a is not None and row_b is not None
    # Old session revoked and linked to its successor, atomically.
    assert row_a.revoked_at is not None
    assert row_a.replaced_by == row_b.id
    # Successor is live and carries its own expiry window.
    assert row_b.revoked_at is None
    assert row_b.replaced_by is None
    assert row_b.user_id == user.id
    assert row_b.expires_at == _T0 + timedelta(days=_REFRESH_TTL_DAYS)
    # New access token is bound to the successor session.
    claims_b = AccessTokenCodec(
        secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
    ).decode(tokens_b.access_token)
    assert claims_b.sid == str(row_b.id)

    live = await _count_unrevoked_sessions(db_session, user.id)
    # Strict rotate-once (this service runs grace=0): reuse is the plain
    # §5.6 rejection; the grace-window tests above cover in-window
    # replay under an explicitly enabled window.
    with pytest.raises(BusinessError) as reuse_exc:
        await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert reuse_exc.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert reuse_exc.value.status_code == 401
    # A failed reuse created nothing and revoked nothing new.
    assert await _count_unrevoked_sessions(db_session, user.id) == live

    # The successor keeps rotating: B -> C.
    _advance(clock, minutes=1)
    tokens_c = await service.rotate_refresh(db_session, tokens_b.refresh_token)
    assert tokens_c.refresh_token not in {
        tokens_a.refresh_token,
        tokens_b.refresh_token,
    }


@pytest.mark.integration
async def test_replay_within_grace_reissues_the_same_live_generation(
    db_session: AsyncSession,
) -> None:
    """Stable-successor replay (PR #10 rework, review P0-1).

    Two clients cold-start on one refresh token; the loser's replay must
    resolve to the SAME live generation the winner received — re-issuing
    it, not rotating the tip again (which would instantly invalidate the
    winner's access token and cookie: find_with_live_session requires
    replaced_by IS NULL). N replays = zero additional rotations.
    """
    clock = FrozenClock(_T0)
    service = _make_grace_service(clock)
    user = await _seed_user(db_session)

    tokens_a = await service.login_student(db_session, _USERNAME, _PASSWORD)
    tokens_b = await service.rotate_refresh(db_session, tokens_a.refresh_token)
    rows_after_rotation = await _count_sessions(db_session, user.id)
    assert rows_after_rotation == 2  # A retired + B live

    # The losing tab replays A: it receives B's OWN refresh secret and a
    # fresh access token bound to the still-live B row. No rotation.
    replayed = await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert replayed.refresh_token == tokens_b.refresh_token
    claims = AccessTokenCodec(
        secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
    ).decode(replayed.access_token)
    row_b = await _session_row(db_session, tokens_b.refresh_token)
    assert row_b is not None
    assert claims.sid == str(row_b.id)
    assert row_b.revoked_at is None and row_b.replaced_by is None

    # N tabs, one rotation: further replays add no rows, retire nothing.
    for _ in range(3):
        again = await service.rotate_refresh(db_session, tokens_a.refresh_token)
        assert again.refresh_token == tokens_b.refresh_token
    assert await _count_sessions(db_session, user.id) == rows_after_rotation
    assert await _count_unrevoked_sessions(db_session, user.id) == 1

    # A later normal rotation retires B; the stale A replay now resolves
    # THROUGH the envelope chain to the new live generation C.
    tokens_c = await service.rotate_refresh(db_session, tokens_b.refresh_token)
    chained = await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert chained.refresh_token == tokens_c.refresh_token
    assert await _count_unrevoked_sessions(db_session, user.id) == 1

    # Outside the window the same replay is the §5.6 rejection again.
    _advance(clock, seconds=31)
    live = await _count_unrevoked_sessions(db_session, user.id)
    with pytest.raises(BusinessError) as stale:
        await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert stale.value.status_code == 401
    assert await _count_unrevoked_sessions(db_session, user.id) == live


@pytest.mark.integration
async def test_presented_expiry_precedes_grace(
    db_session: AsyncSession,
) -> None:
    """Review P1: the presented credential's own expiry is checked
    before any grace logic — an expired-but-recently-retired token
    never replays even while a live successor exists."""
    clock = FrozenClock(_T0)
    service = _make_grace_service(clock)
    await _seed_user(db_session)

    tokens_a = await service.login_student(db_session, _USERNAME, _PASSWORD)
    await service.rotate_refresh(db_session, tokens_a.refresh_token)

    _advance(clock, days=_REFRESH_TTL_DAYS + 1)  # A itself long expired
    with pytest.raises(BusinessError) as expired:
        await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert expired.value.status_code == 401


@pytest.mark.integration
async def test_grace_replay_fails_when_lineage_is_logged_out(
    db_session: AsyncSession,
) -> None:
    """Grace never resurrects a logged-out lineage — neither replaying
    the retired predecessor nor presenting the revoked tip itself."""
    clock = FrozenClock(_T0)
    service = _make_grace_service(clock)
    await _seed_user(db_session)

    tokens_a = await service.login_student(db_session, _USERNAME, _PASSWORD)
    tokens_b = await service.rotate_refresh(db_session, tokens_a.refresh_token)
    await service.revoke_session(db_session, tokens_b.refresh_token)

    with pytest.raises(BusinessError) as tip:
        await service.rotate_refresh(db_session, tokens_b.refresh_token)
    assert tip.value.status_code == 401
    with pytest.raises(BusinessError) as predecessor:
        await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert predecessor.value.status_code == 401


@pytest.mark.integration
async def test_logout_from_stale_generation_kills_the_live_tip(
    db_session: AsyncSession,
) -> None:
    """Review P0-2: a client holding a retired generation (the normal
    state of a raced cookie jar) must have its logout revoke the LIVE
    lineage tip — never a silent no-op that keeps the account signed
    in elsewhere."""
    clock = FrozenClock(_T0)
    service = _make_grace_service(clock)
    user = await _seed_user(db_session)

    tokens_a = await service.login_student(db_session, _USERNAME, _PASSWORD)
    tokens_b = await service.rotate_refresh(db_session, tokens_a.refresh_token)

    # Logout presents the STALE generation A.
    await service.revoke_session(db_session, tokens_a.refresh_token)

    assert await _count_unrevoked_sessions(db_session, user.id) == 0
    with pytest.raises(BusinessError) as dead:
        await service.rotate_refresh(db_session, tokens_b.refresh_token)
    assert dead.value.status_code == 401


@pytest.mark.integration
async def test_logout_walks_deep_rotation_chains_without_a_cap(
    db_session: AsyncSession,
) -> None:
    """Review P1 regression: the logout walk must be unbounded.

    A lineage earns one row per refresh; with the 15-minute access TTL
    a day-old session has dozens of generations. A hop cap would fail
    OPEN — the truncated walk lands on a retired row and logout
    silently no-ops while the live tip stays signed in (reproduced on
    PG with 12 generations before the fix).
    """
    clock = FrozenClock(_T0)
    service = _make_service(clock)  # grace=0: pure rotation chain
    user = await _seed_user(db_session)

    tokens = await service.login_student(db_session, _USERNAME, _PASSWORD)
    first_refresh = tokens.refresh_token
    for _ in range(12):  # comfortably past any sane hop bound
        tokens = await service.rotate_refresh(db_session, tokens.refresh_token)

    # Logging out with the OLDEST credential must still reach the tip.
    await service.revoke_session(db_session, first_refresh)
    assert await _count_unrevoked_sessions(db_session, user.id) == 0


@pytest.mark.integration
async def test_replay_fail_closed_shapes(
    db_session: AsyncSession,
) -> None:
    """Every fail-closed envelope shape refuses replay with a uniform
    401: rows retired while the window was OFF (no envelope material),
    legacy-shaped rows (replaced_at NULL), and tampered envelope
    ciphertext (InvalidToken)."""
    clock = FrozenClock(_T0)
    strict = _make_service(clock)  # grace=0
    await _seed_user(db_session)

    tokens_a = await strict.login_student(db_session, _USERNAME, _PASSWORD)
    tokens_b = await strict.rotate_refresh(db_session, tokens_a.refresh_token)

    graced = _make_grace_service(clock)

    # Shape 1: row_a was retired under grace=0 — no envelope material.
    row_a = await _session_row(db_session, tokens_a.refresh_token)
    assert row_a is not None
    assert row_a.replay_envelope is None
    with pytest.raises(BusinessError) as no_envelope:
        await graced.rotate_refresh(db_session, tokens_a.refresh_token)
    assert no_envelope.value.status_code == 401

    # Shape 2: legacy shape — retirement timestamp missing entirely.
    row_a.replaced_at = None
    await db_session.flush()
    with pytest.raises(BusinessError) as legacy:
        await graced.rotate_refresh(db_session, tokens_a.refresh_token)
    assert legacy.value.status_code == 401

    # Shape 3: a well-formed retirement with a GARBAGE envelope.
    tokens_c = await graced.rotate_refresh(db_session, tokens_b.refresh_token)
    row_b = await _session_row(db_session, tokens_b.refresh_token)
    assert row_b is not None and row_b.replay_envelope
    row_b.replay_envelope = Fernet(_REPLAY_KEY).encrypt(b"tampered").decode()
    await db_session.flush()
    with pytest.raises(BusinessError) as tampered:
        await graced.rotate_refresh(db_session, tokens_b.refresh_token)
    assert tampered.value.status_code == 401
    # The live generation is untouched by the refusal.
    tokens_d = await graced.rotate_refresh(db_session, tokens_c.refresh_token)
    assert tokens_d.refresh_token


@pytest.mark.integration
async def test_refresh_vs_logout_race_ends_with_zero_live_sessions(
    db_engine: AsyncEngine,
) -> None:
    """Review-mandated concurrency: one client refreshes while another
    logs out, both presenting the same initial token. Regardless of
    which transaction wins the row lock, the final lineage has ZERO
    live sessions. Real commits on independent connections."""
    username = "30990099997"
    clock = FrozenClock(_T0)
    service = _make_grace_service(clock)
    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(_PASSWORD),
                nickname="登出竞态同学",
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()
    try:
        async with AsyncSession(db_engine) as session:
            tokens = await service.login_student(session, username, _PASSWORD)

        async def _rotate() -> bool:
            async with AsyncSession(db_engine) as session:
                try:
                    await service.rotate_refresh(session, tokens.refresh_token)
                    return True
                except BusinessError:
                    return False

        async def _logout() -> None:
            async with AsyncSession(db_engine) as session:
                await service.revoke_session(session, tokens.refresh_token)

        await asyncio.gather(_rotate(), _logout())

        async with AsyncSession(db_engine) as verifier:
            user_row = await verifier.scalar(
                select(User).where(User.username == username)
            )
            assert user_row is not None
            assert await _count_unrevoked_sessions(verifier, user_row.id) == 0
    finally:
        async with AsyncSession(db_engine) as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is not None:
                await session.execute(
                    delete(UserSession).where(UserSession.user_id == user.id)
                )
                await session.delete(user)
            await session.commit()


@pytest.mark.integration
async def test_grace_disabled_at_zero_seconds(
    db_session: AsyncSession,
) -> None:
    """REFRESH_GRACE_SECONDS=0 (the default until the amendment ruling)
    restores strict rotate-once semantics and writes no envelopes."""
    clock = FrozenClock(_T0)
    service = SessionService(
        clock=clock,
        access_codec=AccessTokenCodec(
            secret=_ACCESS_SECRET, ttl_minutes=_ACCESS_TTL_MINUTES
        ),
        refresh_token_ttl_days=_REFRESH_TTL_DAYS,
    )
    await _seed_user(db_session)

    tokens_a = await service.login_student(db_session, _USERNAME, _PASSWORD)
    await service.rotate_refresh(db_session, tokens_a.refresh_token)

    row_a = await _session_row(db_session, tokens_a.refresh_token)
    assert row_a is not None
    assert row_a.replay_envelope is None  # no envelope material at rest

    with pytest.raises(BusinessError) as exc_info:
        await service.rotate_refresh(db_session, tokens_a.refresh_token)
    assert exc_info.value.status_code == 401


@pytest.mark.integration
async def test_rotate_unknown_token_fails(
    db_session: AsyncSession,
) -> None:
    await _seed_user(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await _make_service(FrozenClock(_T0)).rotate_refresh(
            db_session, "no-such-refresh-token-anywhere-0123456789"
        )

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.integration
async def test_expired_refresh_fails(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    await _seed_user(db_session)
    tokens = await service.login_student(db_session, _USERNAME, _PASSWORD)

    # Business time moves past the refresh window: FrozenClock, no sleep.
    _advance(clock, days=_REFRESH_TTL_DAYS, seconds=1)

    with pytest.raises(BusinessError) as exc_info:
        await service.rotate_refresh(db_session, tokens.refresh_token)

    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    assert exc_info.value.status_code == 401
    # Expiry is clock-driven, not a revoke: the row stays untouched (audit
    # trail), but nothing new can descend from it.
    row = await _session_row(db_session, tokens.refresh_token)
    assert row is not None
    assert row.revoked_at is None
    assert row.replaced_by is None


@pytest.mark.integration
async def test_rotate_within_expiry_boundary_succeeds(
    db_session: AsyncSession,
) -> None:
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    await _seed_user(db_session)
    tokens = await service.login_student(db_session, _USERNAME, _PASSWORD)

    _advance(clock, days=_REFRESH_TTL_DAYS, seconds=-1)
    rotated = await service.rotate_refresh(db_session, tokens.refresh_token)

    assert rotated.refresh_token != tokens.refresh_token


@pytest.mark.integration
async def test_revoke_all_kills_live_sessions(
    db_session: AsyncSession,
) -> None:
    # The §5.6 "password change/reset SHOULD revoke old refresh sessions"
    # mechanism: every live session dies, replaced or not.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    user = await _seed_user(db_session)

    first = await service.login_student(db_session, _USERNAME, _PASSWORD)
    second = await service.login_student(db_session, _USERNAME, _PASSWORD)
    rotated = await service.rotate_refresh(db_session, first.refresh_token)
    assert await _count_unrevoked_sessions(db_session, user.id) == 2

    await service.revoke_all(db_session, user.id)

    assert await _count_unrevoked_sessions(db_session, user.id) == 0
    for dead_token in (rotated.refresh_token, second.refresh_token):
        with pytest.raises(BusinessError) as exc_info:
            await service.rotate_refresh(db_session, dead_token)
        assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED

    # Revocation does not block fresh logins.
    fresh = await service.login_student(db_session, _USERNAME, _PASSWORD)
    assert await _count_unrevoked_sessions(db_session, user.id) == 1
    assert fresh.refresh_token


@pytest.mark.integration
async def test_revoke_session_logs_out_only_the_presented_token(
    db_session: AsyncSession,
) -> None:
    # The logout use case (Task 9): revoke exactly the session whose refresh
    # token was presented; every other device stays signed in. Idempotent by
    # design — an unknown or already-revoked token is a no-op success, so a
    # double-clicked logout or a stale cookie never errors.
    clock = FrozenClock(_T0)
    service = _make_service(clock)
    user = await _seed_user(db_session)
    mine = await service.login_student(db_session, _USERNAME, _PASSWORD)
    other = await service.login_student(db_session, _USERNAME, _PASSWORD)

    await service.revoke_session(db_session, mine.refresh_token)

    assert await _count_unrevoked_sessions(db_session, user.id) == 1
    with pytest.raises(BusinessError) as exc_info:
        await service.rotate_refresh(db_session, mine.refresh_token)
    assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    other_rotated = await service.rotate_refresh(db_session, other.refresh_token)
    assert other_rotated.refresh_token

    # Replay and unknown tokens are silent no-ops.
    await service.revoke_session(db_session, mine.refresh_token)
    await service.revoke_session(db_session, "never-issued-token")
    assert await _count_unrevoked_sessions(db_session, user.id) == 1


@pytest.mark.integration
async def test_concurrent_rotation_of_one_refresh_token_converges_one_lineage(
    db_engine: AsyncEngine,
) -> None:
    # T7 review carry-forward, grace-window edition: two connections
    # present the SAME refresh token concurrently; the FOR UPDATE row
    # lock still serializes them, but the rotation grace window lets
    # the second presentation resume the lineage tip instead of dying
    # to a logged-out render (the cross-tab cold-start race). The new
    # invariants: BOTH presenters walk away with tokens, exactly ONE
    # unreplaced row remains, and past the window the original token is
    # the plain §5.6 rejection again. Real commits on independent
    # sessions (the rollback harness serializes everything through one
    # connection and cannot prove a race).
    username = "30990099998"
    clock = FrozenClock(_T0)
    service = _make_grace_service(clock)
    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(_PASSWORD),
                nickname="并发轮换同学",
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()
    try:
        async with AsyncSession(db_engine) as session:
            tokens = await service.login_student(session, username, _PASSWORD)
        refresh = tokens.refresh_token

        async def _rotate() -> SessionTokens | BusinessError:
            async with AsyncSession(db_engine, expire_on_commit=False) as session:
                try:
                    return await service.rotate_refresh(session, refresh)
                except BusinessError as exc:
                    return exc

        results = await asyncio.gather(_rotate(), _rotate())

        winners = [result for result in results if isinstance(result, SessionTokens)]
        losers = [result for result in results if isinstance(result, BusinessError)]
        # Review P0-1 invariant: BOTH concurrent presenters win AND they
        # converge on ONE live generation — the loser's replay re-issues
        # the winner's refresh secret (stable successor), so nobody's
        # freshly issued credentials are invalidated by the other's.
        assert len(winners) == 2
        assert losers == []
        assert winners[0].refresh_token == winners[1].refresh_token
        async with AsyncSession(db_engine) as verifier:
            user_row = await verifier.scalar(
                select(User).where(User.username == username)
            )
            assert user_row is not None
            # Plain value: the rotations below commit this session, which
            # expires ORM attributes (expire_on_commit) — a later lazy
            # refresh of user_row.id would be sync IO (MissingGreenlet).
            user_id = user_row.id
            assert await _count_unrevoked_sessions(verifier, user_id) == 1
            # Exactly one rotation happened for the two-presenter wave.
            assert await _count_sessions(verifier, user_id) == 2
            # In-window, EVERY holder can still make progress — both
            # access tokens name the one live session row.
            for winner in winners:
                fresh = await service.rotate_refresh(verifier, winner.refresh_token)
                assert fresh.refresh_token

        # Past the window, the original token is rejected for good.
        _advance(clock, seconds=31)
        async with AsyncSession(db_engine) as verifier:
            with pytest.raises(BusinessError):
                await service.rotate_refresh(verifier, refresh)
    finally:
        async with AsyncSession(db_engine) as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is not None:
                await session.execute(
                    delete(UserSession).where(UserSession.user_id == user.id)
                )
                await session.delete(user)
            await session.commit()


@pytest.mark.integration
async def test_login_and_rotation_never_log_secrets(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    # backend-engineering §15: no password, no access token, no refresh
    # token in any log line — asserted over the full login+rotate flow.
    service = _make_service(FrozenClock(_T0))
    await _seed_user(db_session)

    with caplog.at_level(logging.INFO):
        tokens = await service.login_student(db_session, _USERNAME, _PASSWORD)
        rotated = await service.rotate_refresh(db_session, tokens.refresh_token)
        with contextlib.suppress(BusinessError):
            await service.rotate_refresh(db_session, tokens.refresh_token)

    secret_material = {
        _PASSWORD,
        tokens.access_token,
        tokens.refresh_token,
        rotated.access_token,
        rotated.refresh_token,
        hash_refresh_token(tokens.refresh_token),
    }
    for record in caplog.records:
        message = record.getMessage()
        for secret in secret_material:
            assert secret not in message


@pytest.mark.integration
async def test_login_rejects_out_of_band_password_uniformly(
    db_session: AsyncSession,
) -> None:
    # A 9-character or 129-character password can never be correct: the
    # band rejection maps to the same uniform auth failure as a wrong
    # password (ValueError never escapes the login boundary).
    await _seed_user(db_session)

    for password in ("short", "a" * 129):
        with pytest.raises(BusinessError) as exc_info:
            await _make_service(FrozenClock(_T0)).login_student(
                db_session, _USERNAME, password
            )
        assert exc_info.value.code == ErrorCode.AUTHENTICATION_REQUIRED
        assert exc_info.value.status_code == 401


@pytest.mark.integration
async def test_login_and_rotate_work_on_default_expiring_session(
    db_engine: AsyncEngine,
) -> None:
    # Regression: post-commit log lines must not touch ORM attributes. A
    # default AsyncSession expires every instance on commit (the production
    # dependency happens to use expire_on_commit=False; this service may
    # not rely on that). Real commits on a dedicated connection; rows are
    # cleaned up because the rollback harness cannot see them.
    username = "30990099999"
    service = _make_service(FrozenClock(_T0))
    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password(_PASSWORD),
                nickname="过期会话同学",
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()
    try:
        async with AsyncSession(db_engine) as session:
            tokens = await service.login_student(session, username, _PASSWORD)
        async with AsyncSession(db_engine) as session:
            rotated = await service.rotate_refresh(session, tokens.refresh_token)
        assert rotated.refresh_token != tokens.refresh_token
    finally:
        async with AsyncSession(db_engine) as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is not None:
                await session.execute(
                    delete(UserSession).where(UserSession.user_id == user.id)
                )
                await session.delete(user)
            await session.commit()
