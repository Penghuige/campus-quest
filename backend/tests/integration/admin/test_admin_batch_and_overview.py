# backend/tests/integration/admin/test_admin_batch_and_overview.py
"""Wave 2a §3.5 + §4.1: the batch status action and the console overview.

The batch endpoint runs ONE status action over up to 100 ids, one row
at a time: every row is its own audited transition, a failing row
answers {ok:false, reason:<error code>} and never breaks its
neighbours, and the actor's own id is refused row-wise (a self-suspend
would lock the console's operator out mid-flight). The overview is the
console's one-read aggregate: fixed key sets (zero counts present),
governance facts only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import get_rate_limiter
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession, api_clock: FrozenClock, fake_limiter: FakeRateLimiter
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_staff(
    db: AsyncSession, clock: FrozenClock, *, role: Role, status: UserStatus
) -> User:
    username = f"batch-{uuid4().hex[:8]}@campus.example.edu"
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=username.split("@")[0],
        email_normalized=username,
        email_verified_at=clock.now(),
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    if role is not Role.STUDENT:
        db.add(
            TotpCredential(
                user_id=user.id,
                secret_encrypted=b"test-stand-in-secret",
                confirmed_at=clock.now(),
            )
        )
    await db.flush()
    return user


async def _admin_headers(
    db: AsyncSession, clock: FrozenClock
) -> tuple[User, dict[str, str]]:
    admin = await _seed_staff(db, clock, role=Role.ADMIN, status=UserStatus.ACTIVE)
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=admin, now=clock.now())
    await db.commit()
    return admin, {"Authorization": f"Bearer {tokens.access_token}"}


async def test_batch_suspends_each_row_with_per_row_results(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, headers = await _admin_headers(db_session, api_clock)
    a = await _seed_staff(
        db_session, api_clock, role=Role.TEACHER, status=UserStatus.ACTIVE
    )
    b = await _seed_staff(
        db_session, api_clock, role=Role.TEACHER, status=UserStatus.ACTIVE
    )
    unknown = uuid4()
    await db_session.commit()

    response = await client.post(
        "/api/v1/admin/users/batch",
        json={"action": "suspend", "user_ids": [str(a.id), str(b.id), str(unknown)]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    results = {r["user_id"]: r for r in response.json()["results"]}
    assert results[str(a.id)] == {"user_id": str(a.id), "ok": True}
    assert results[str(b.id)]["ok"] is True
    assert results[str(unknown)] == {
        "user_id": str(unknown),
        "ok": False,
        "reason": "NOT_FOUND",
    }


async def test_batch_refuses_the_actor_row_and_invalid_transitions(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)
    banned = await _seed_staff(
        db_session, api_clock, role=Role.TEACHER, status=UserStatus.BANNED
    )
    await db_session.commit()

    response = await client.post(
        "/api/v1/admin/users/batch",
        json={"action": "suspend", "user_ids": [str(admin.id), str(banned.id)]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    results = {r["user_id"]: r for r in response.json()["results"]}
    assert results[str(admin.id)]["ok"] is False
    assert results[str(admin.id)]["reason"] == "PERMISSION_DENIED"
    assert results[str(banned.id)]["ok"] is False
    assert results[str(banned.id)]["reason"] == "CONFLICT"


async def test_batch_caps_at_one_hundred_ids(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, headers = await _admin_headers(db_session, api_clock)
    await db_session.commit()

    response = await client.post(
        "/api/v1/admin/users/batch",
        json={"action": "suspend", "user_ids": [str(uuid4()) for _ in range(101)]},
        headers=headers,
    )

    assert response.status_code == 422, response.text


async def test_overview_answers_fixed_key_aggregates(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, headers = await _admin_headers(db_session, api_clock)
    await db_session.commit()

    response = await client.get("/api/v1/admin/overview", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "users",
        "tasks",
        "pending_submissions",
        "pending_redemptions",
        "today",
    }
    assert set(body["users"]) == {"total", "by_role", "by_status"}
    for key in ("STUDENT", "TEACHER", "ADMIN"):
        assert key in body["users"]["by_role"]
    for key in ("ACTIVE", "PENDING_APPROVAL", "SUSPENDED", "BANNED"):
        assert key in body["users"]["by_status"]
    assert set(body["today"]) == {"new_users", "active_users"}
    assert isinstance(body["users"]["total"], int)
    assert body["users"]["total"] >= 1
