# backend/tests/integration/test_transaction_owner.py
"""The request-scoped session never commits implicitly (PR review fix).

Regression pair for the single-transaction-owner rule (backend-engineering
§5): `get_db_session` must NOT commit on teardown — a route that mutates ORM
state without an explicit service commit loses the change when the request
ends, while the same route committing explicitly (the service pattern every
module follows) persists. Both halves run against the real engine and the
REAL `get_db_session` dependency over a real HTTP round trip (ASGI
transport); only the session maker is pointed at the test engine, so the
behavior under test is the production dependency's own.

Rows are created and removed through dedicated sessions: the rollback
harness cannot see rows committed on other connections (the pattern of the
concurrency suites in tests/integration/identity).
"""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.db.session as db_session_module
from app.core.security import hash_password
from app.db.session import get_db_session
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User, UserSession

_USERNAME = "30990070001"
_PASSWORD = "correct-horse-battery"
_SEED_NICKNAME = "事务归属同学"
_SILENT_NICKNAME = "未提交的昵称"
_COMMITTED_NICKNAME = "已提交的昵称"


async def _seed_user(db_engine: AsyncEngine) -> int:
    # expire_on_commit=False so the id is readable after commit (rows on this
    # dedicated connection escape the rollback harness and need cleanup).
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        user = User(
            username=_USERNAME,
            password_hash=hash_password(_PASSWORD),
            nickname=_SEED_NICKNAME,
            role=Role.STUDENT.value,
            status=UserStatus.ACTIVE.value,
        )
        session.add(user)
        await session.commit()
        assert user.id is not None
        return user.id


async def _persisted_nickname(db_engine: AsyncEngine, user_id: int) -> str:
    async with AsyncSession(db_engine) as verifier:
        user = await verifier.get(User, user_id)
        assert user is not None
        return user.nickname


async def _cleanup(db_engine: AsyncEngine, user_id: int) -> None:
    async with AsyncSession(db_engine) as session:
        await session.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@pytest.mark.integration
async def test_uncommitted_mutation_is_discarded_when_the_request_ends(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression the review fix pins: an endpoint that mutates ORM state
    # WITHOUT committing must not have its change persisted by the request
    # dependency's teardown. Under the old auto-commit teardown this test
    # failed (the nickname leaked into the database).
    monkeypatch.setattr(
        db_session_module,
        "get_async_session_maker",
        lambda: async_sessionmaker(db_engine),
    )
    app = FastAPI()

    @app.post("/silent-mutation")
    async def silent_mutation(
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, bool]:
        # Deliberately service-less: mutate and return without commit.
        user = await db.get(User, user_id)
        assert user is not None
        user.nickname = _SILENT_NICKNAME
        return {"ok": True}

    user_id = await _seed_user(db_engine)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/silent-mutation")
        assert response.status_code == 200, response.text

        assert await _persisted_nickname(db_engine, user_id) == _SEED_NICKNAME
    finally:
        await _cleanup(db_engine, user_id)


@pytest.mark.integration
async def test_explicitly_committed_mutation_persists_after_the_request(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control half: the same dependency hands the SAME route a session on
    # which an explicit commit (the service pattern) persists — proving the
    # discard above is the missing commit, not a broken wiring.
    monkeypatch.setattr(
        db_session_module,
        "get_async_session_maker",
        lambda: async_sessionmaker(db_engine),
    )
    app = FastAPI()

    @app.post("/committed-mutation")
    async def committed_mutation(
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, bool]:
        user = await db.get(User, user_id)
        assert user is not None
        user.nickname = _COMMITTED_NICKNAME
        await db.commit()
        return {"ok": True}

    user_id = await _seed_user(db_engine)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/committed-mutation")
        assert response.status_code == 200, response.text

        assert await _persisted_nickname(db_engine, user_id) == _COMMITTED_NICKNAME
    finally:
        await _cleanup(db_engine, user_id)
