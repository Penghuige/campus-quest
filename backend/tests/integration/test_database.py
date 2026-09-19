# backend/tests/integration/test_database.py
"""Integration tests for the persistence foundation on real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from db_guard import require_test_database


@pytest.mark.integration
async def test_database_executes_select(db_session: AsyncSession) -> None:
    value = await db_session.scalar(text("select 1"))
    assert value == 1


@pytest.mark.integration
async def test_db_session_commits_stay_inside_the_test_transaction(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    await db_session.execute(text("create table leak_probe(id integer primary key)"))
    await db_session.execute(text("insert into leak_probe (id) values (1)"))
    await db_session.commit()

    assert await db_session.scalar(text("select count(*) from leak_probe")) == 1

    async with db_engine.connect() as independent:
        visible = await independent.scalar(
            text("select to_regclass('public.leak_probe')")
        )
    assert visible is None


@pytest.mark.integration
def test_guard_refuses_database_without_test_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://test:test@localhost:15432/campusquest"
    )
    database_url = Settings().database_url

    with pytest.raises(RuntimeError, match="campusquest_test"):
        require_test_database(database_url)
