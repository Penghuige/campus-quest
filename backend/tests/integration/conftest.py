# backend/tests/integration/conftest.py
"""Fixtures for integration tests against a real PostgreSQL instance.

Real PostgreSQL only: when the configured test database is unreachable the
suite fails with startup instructions instead of skipping silently. Each test
runs inside an outer transaction that is rolled back at teardown, and every
session-level commit releases only a savepoint, so committed rows never leak
between tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from db_guard import require_test_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.session import create_db_engine

_TEST_STACK_HINT = (
    "docker compose -f infra/docker-compose.yml up -d (from the repository root)"
)


async def _probe_connectivity(database_url: str) -> None:
    engine = create_async_engine(
        database_url, poolclass=NullPool, connect_args={"timeout": 5}
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def require_integration_database() -> None:
    """Fail the suite clearly when the test database is wrong or unreachable."""
    settings = get_settings()
    try:
        require_test_database(settings.database_url)
    except RuntimeError as exc:
        pytest.fail(str(exc))
    try:
        asyncio.run(_probe_connectivity(settings.database_url))
    except Exception as exc:
        pytest.fail(
            f"Integration database unreachable at {settings.database_url}: {exc!r}. "
            f"Start the local dependency stack first: {_TEST_STACK_HINT}"
        )


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Engine built through the production factory; one per test so pooled
    asyncpg connections never cross pytest-asyncio event loops."""
    engine = create_db_engine(get_settings())
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _loop_local_identity_redis() -> AsyncIterator[None]:
    """Give every test a FRESH process-wide identity Redis client.

    ``get_identity_redis`` is ``lru_cache``d for production (one event
    loop per process), but pytest-asyncio gives every test its own loop.
    The zero-override suites (composition smoke, several API tests) hit
    routes whose dependency graph resolves the real client — since the
    approval lockout wired ``guard.reset`` into the login success path
    (#95), a plain student login in ANY no-override test binds the
    cached client to that test's loop; the composition smoke running on
    a later loop then dies with "Future attached to a different loop"
    (CI, deterministic). Closing and re-caching per test keeps every
    binding loop-local; production never runs two loops.
    """
    from app.modules.identity.providers import get_identity_redis

    try:
        client = get_identity_redis()
        await client.aclose()
    except Exception:
        pass  # never created this process yet — nothing to close
    get_identity_redis.cache_clear()
    yield
    try:
        client = get_identity_redis()
        await client.aclose()
    except Exception:
        pass
    get_identity_redis.cache_clear()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session joined to an outer transaction that always rolls back.

    `join_transaction_mode="create_savepoint"` (the SQLAlchemy 2.0 testing
    recipe) makes each session-level commit release only a savepoint, so code
    under test can commit freely while the outer rollback still guarantees no
    rows leak between tests.
    """
    require_test_database(get_settings().database_url)
    connection = await db_engine.connect()
    try:
        transaction = await connection.begin()
        try:
            session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            yield session
            await session.close()
        finally:
            if transaction.is_active:
                await transaction.rollback()
    finally:
        await connection.close()
