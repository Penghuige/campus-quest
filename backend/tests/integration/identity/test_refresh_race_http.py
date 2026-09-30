"""HTTP-level regression for the concurrent cold-start refresh race
(tester defect #1, PR #10 review P1): service-level "2 x 200" can be
green while the product still logs users out. These tests drive the
real routers with the real access-token liveness rule
(``find_with_live_session`` requires ``replaced_by IS NULL``) and prove
the whole matrix:

- two cold-start clients on one initial refresh token, response order
  intentionally inverted (the loser's replay arrives after the
  winner's rotation landed);
- EVERY returned access token is exercised after both refreshes settle;
- the final browser refresh credential still refreshes AFTER the grace
  window has elapsed;
- an explicit logout from the stale generation leaves no live lineage
  and a dead access token;
- the whole two-client wave performs exactly ONE rotation (one new
  session row).

Runs the app with the grace window enabled via a settings override —
production default stays 0 until the §5.6 amendment ruling.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from test_identity_api import (  # noqa: F401  (no-package layout: pytest
    api_app,  # imports test modules as top-level names; a package-path
    api_clock,  # import would register the models twice)
    api_email,
    api_redis,
    api_sms,
    client,
    fake_limiter,
)
from test_sessions import _count_sessions

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User, UserSession

_STUDENT = "20250077701"
_PASSWORD = "correct-horse-battery"
_T0 = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def grace_app(api_app: FastAPI) -> FastAPI:  # noqa: F811
    """The real app with REFRESH_GRACE_SECONDS=30 (settings override).

    ``model_copy`` keeps every other deployment setting identical; only
    the grace window (and its Fernet envelope crypt, derived by the
    provider from the existing key) switches on.
    """
    settings = get_settings().model_copy(update={"refresh_grace_seconds": 30})
    api_app.dependency_overrides[get_settings] = lambda: settings
    return api_app


@pytest.fixture
def grace_clock(api_clock: FrozenClock) -> FrozenClock:  # noqa: F811
    return api_clock


async def _seed(db: AsyncSession) -> User:
    user = User(
        username=_STUDENT,
        password_hash=hash_password(_PASSWORD),
        nickname="竞态同学",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    from datetime import timedelta

    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.integration
async def test_two_client_cold_start_race_matrix(
    db_session: AsyncSession,
    grace_app: FastAPI,
    grace_clock: FrozenClock,
) -> None:
    user = await _seed(db_session)
    transport = httpx.ASGITransport(app=grace_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        # Both clients cold-start from the SAME initial credential.
        # Login delivers the refresh secret as the rotation cookie; the
        # body form below then carries it explicitly (no ambient cookie
        # CSRF surface).
        login = await http.post(
            "/api/v1/auth/login",
            json={"username": _STUDENT, "password": _PASSWORD},
        )
        assert login.status_code == 200, login.text
        initial_refresh = http.cookies.get("refresh_token")
        assert initial_refresh
        rows_before = await _count_sessions(db_session, user.id)

        # Client 1 (the eventual winner) rotates first; the body form
        # carries the credential explicitly, no ambient cookies. The
        # rotation response delivers the next refresh secret as the
        # cookie (body-refresh responses are cookie-only).
        winner = await http.post(
            "/api/v1/auth/refresh", json={"refresh_token": initial_refresh}
        )
        assert winner.status_code == 200, winner.text
        winner_refresh = http.cookies.get("refresh_token")
        assert winner_refresh and winner_refresh != initial_refresh

        # Client 2 replays the SAME initial credential AFTER the
        # winner's rotation landed — the inverted-order loser. Under
        # the stable-successor grace it re-issues the winner's live
        # generation instead of dying (and instead of retiring the
        # winner's credentials with a second rotation).
        loser = await http.post(
            "/api/v1/auth/refresh", json={"refresh_token": initial_refresh}
        )
        assert loser.status_code == 200, loser.text
        assert http.cookies.get("refresh_token") == winner_refresh, (
            "the loser must adopt the winner's live generation"
        )

        # Exactly one rotation for the whole two-client wave.
        assert await _count_sessions(db_session, user.id) == rows_before + 1

        # EVERY access token issued by the wave is usable after both
        # refreshes settle (the old tip-rotation design killed the
        # winner's token here).
        for tokens in (winner.json(), loser.json()):
            me = await http.get("/api/v1/me", headers=_bearer(tokens))
            assert me.status_code == 200, me.text

        # Past the grace window the FINAL refresh credential (what the
        # browser's cookie jar settles on) still refreshes normally.
        _advance(grace_clock, seconds=31)
        final = await http.post(
            "/api/v1/auth/refresh", json={"refresh_token": winner_refresh}
        )
        assert final.status_code == 200, final.text

        # A stale generation's logout must kill the LIVE lineage — no
        # silent no-op while a later generation keeps the account in.
        logout = await http.post(
            "/api/v1/auth/logout", json={"refresh_token": initial_refresh}
        )
        assert logout.status_code == 204, logout.text

        dead = await http.get("/api/v1/me", headers=_bearer(final.json()))
        assert dead.status_code == 401
        from sqlalchemy import select as sa_select

        live_rows = (
            await db_session.execute(
                sa_select(UserSession).where(
                    UserSession.user_id == user.id,
                    UserSession.revoked_at.is_(None),
                    UserSession.replaced_by.is_(None),
                )
            )
        ).all()
        assert live_rows == []
