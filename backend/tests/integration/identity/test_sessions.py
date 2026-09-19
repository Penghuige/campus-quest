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

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
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
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="测试同学",
        role=Role.STUDENT,
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
