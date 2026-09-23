# backend/tests/e2e/conftest.py
"""Session fixtures for the full-system e2e suite (plan 10 task 1).

Two harness decisions, both pinned here so every later e2e module
(tasks 2-9) inherits them without re-deriving:

1. **Committed mode with a NullPool factory.** e2e flows cross real
   transaction boundaries: the API commits its request transaction, the
   worker jobs open their OWN per-job engines (``run_with_session``),
   and a rollback harness (an open outer transaction) would make the
   API's committed rows invisible to nobody but itself — the job's
   separate connection could never see them. So this suite follows the
   composition-smoke / test_validation_worker convention
   (``tests/integration/test_composition_smoke.py``): a NullPool engine
   (fresh connection per checkout, nothing pooled across pytest-asyncio
   event loops — G6) and real commits, with tests cleaning their rows
   in FK order through ``factories.clean_world`` (the cleanup-harness
   precedent) rather than relying on a rollback.

2. **Whole-directory guard.** Every test under ``tests/e2e`` skips
   unless ``CQ_E2E=1`` — the same flag name the frontend Playwright
   specs already use, and the pytest-side analogue of the
   ``CQ_COMPOSITION_SMOKE`` module guards: the plain ``uv run pytest -v``
   run must stay green without the full PG/Redis/MinIO stack. With the
   flag set, a wrong or unreachable stack FAILS the suite loudly
   (never skips silently) through ``require_e2e_stack`` below.

The database guard itself is REUSED from the integration suite
(``tests/integration/db_guard.py`` — importing it also installs the
process-wide test-stack env defaults); it is not re-implemented here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

# db_guard is a sibling module of tests/integration; pytest inserts that
# directory into sys.path only for the integration suite's own run, so
# make the import resolvable here too. Reuse, never rewrite (E1 brief).
_INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "integration"
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from db_guard import require_test_database  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402

_TEST_STACK_HINT = (
    "docker compose -f infra/docker-compose.yml up -d (from the repository root)"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip every test under tests/e2e unless CQ_E2E=1 (see module
    docstring): one guard for the whole directory, so later task modules
    need no per-file skip boilerplate."""
    if os.environ.get("CQ_E2E") == "1":
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(
        "full-system e2e: set CQ_E2E=1 (with the PG/Redis/MinIO test stack up) to run"
    )
    for item in items:
        if here in Path(item.path).resolve().parents:
            item.add_marker(skip)


async def _probe_postgres(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    finally:
        await engine.dispose()


async def _probe_redis(redis_url: str) -> None:
    client = aioredis.from_url(redis_url)
    try:
        if not await client.ping():
            raise RuntimeError("redis PING returned False")
    finally:
        await client.aclose()


@pytest.fixture(scope="session", autouse=True)
def require_e2e_stack() -> None:
    """Fail the suite clearly when the test stack is wrong or unreachable.

    Real services only: PostgreSQL is the fact source and Redis backs
    the endpoint rate limiters, so both must answer before any e2e test
    runs. MinIO is probed lazily by the flows that presign (task 2+);
    its endpoint/bucket are still pinned to test naming by db_guard's
    env defaults.
    """
    settings = get_settings()
    try:
        require_test_database(settings.database_url)
    except RuntimeError as exc:
        pytest.fail(str(exc))
    try:
        asyncio.run(_probe_postgres(settings.database_url))
    except Exception as exc:
        pytest.fail(
            f"e2e PostgreSQL unreachable at {settings.database_url}: {exc!r}. "
            f"Start the local dependency stack first: {_TEST_STACK_HINT}"
        )
    try:
        asyncio.run(_probe_redis(settings.redis_url))
    except Exception as exc:
        pytest.fail(
            f"e2e Redis unreachable at {settings.redis_url}: {exc!r}. "
            f"Start the local dependency stack first: {_TEST_STACK_HINT}"
        )


@pytest_asyncio.fixture
async def db_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Committed NullPool sessions — the composition-smoke convention.

    One engine per test; NullPool hands every checkout a fresh
    connection so this factory, the app under test, and any per-job
    worker engines never share pooled connections across event loops.
    Rows COMMIT here: cleanup is the test's job (``clean_world``), not a
    rollback.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
