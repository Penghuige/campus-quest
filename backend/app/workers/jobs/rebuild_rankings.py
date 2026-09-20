# backend/app/workers/jobs/rebuild_rankings.py
"""Ranking rebuild job (spec §17.3 Redis 是投影, §39; interfaces.md
"Ranking projection"; docs/quality/backend-engineering.md §12 worker
rules; plan 05 task 6).

An orchestration shell, nothing more: ``request_id`` in -> construct
dependencies -> call ``RankingRedisProjection.rebuild_all`` -> JSON
summary out. Every aggregation rule lives in the rankings module; this
module must never grow an alternate version of it (design §3).

The job is the Redis-loss recovery path (spec §45 acceptance: Redis
丢失后排行榜可从 PostgreSQL 重建) and the healer for missed projection
enqueues. Idempotency is inherited: a rebuild recomputes absolute
scores from PostgreSQL, so running it twice writes the same boards.

Import discipline (pinned by tests/workers/test_ranking_rebuild.py):
importing this module stays lazy and database-free — the projection and
the session/redis sources are imported/built INSIDE
``run_rebuild_all_rankings``. The default per-job engine is created and
disposed per invocation because each job runs ``asyncio.run`` on a
fresh event loop (the plan-04 ruling on pooled connections vs dead
loops).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from celery import shared_task  # type: ignore[import-untyped]

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.rankings.redis_projection import RankingRedisProjection

logger = logging.getLogger(__name__)


def _default_session_source(settings: Settings) -> Any:
    """A ``() -> async context manager`` yielding one ``AsyncSession``.

    A fresh engine per job invocation, disposed afterward: each job's
    ``asyncio.run`` uses a fresh event loop, and pooled asyncpg
    connections bound to a dead loop are unusable.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import create_db_engine

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        engine = create_db_engine(settings)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                yield session
        finally:
            await engine.dispose()

    # The callable itself (not a context-manager instance): the shell
    # enters `async with session_source() as session`, so the source
    # must be callable and return the context manager.
    return _session


def _default_redis_source(settings: Settings) -> Any:
    """A ``() -> async context manager`` yielding one Redis client."""

    import redis.asyncio as aioredis

    @asynccontextmanager
    async def _redis() -> AsyncIterator[Redis]:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    return _redis


def run_rebuild_all_rankings(
    *,
    request_id: str,
    settings: Settings | None = None,
    session_source: Any = None,
    redis_source: Any = None,
    projection: RankingRedisProjection | None = None,
) -> dict[str, Any]:
    """Construct the dependencies and call the rebuild (the §12 shell).

    Every dependency is injectable; each ``None`` falls back to the
    worker default (settings-derived engine/Redis, default projection).
    Returns a JSON-serializable summary — row data never travels
    through job results (§15).
    """
    from app.modules.rankings.redis_projection import RankingRedisProjection

    if settings is None:
        settings = get_settings()
    if session_source is None:
        session_source = _default_session_source(settings)
    if redis_source is None:
        redis_source = _default_redis_source(settings)
    if projection is None:
        projection = RankingRedisProjection()

    async def _call() -> dict[str, Any]:
        async with session_source() as session, redis_source() as redis:
            return await projection.rebuild_all(session, redis)

    summary = asyncio.run(_call())
    return {
        "request_id": request_id,
        "keys_rebuilt": summary["keys_rebuilt"],
        "members_written": summary["members_written"],
        "stale_keys_deleted": summary["stale_keys_deleted"],
    }


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.rebuild_all_rankings",
)
def rebuild_all_rankings_job(self: Any, request_id: str) -> dict[str, Any]:
    """Rebuild every ranking board from PostgreSQL (spec §17.3).

    Parameters are correlation strings only; dependencies are
    constructed per invocation inside ``run_rebuild_all_rankings``.
    No autoretry: a rebuild is a scheduled/operator-triggered heal, and
    a failure should surface (the boards simply stay as they were)
    rather than loop against a broken dependency.
    """
    job_id = self.request.id
    logger.info(
        "rebuild_all_rankings.start",
        extra={"request_id": request_id, "job_id": job_id},
    )
    result = run_rebuild_all_rankings(request_id=request_id)
    logger.info(
        "rebuild_all_rankings.end",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "keys_rebuilt": len(result["keys_rebuilt"]),
            "members_written": result["members_written"],
            "stale_keys_deleted": len(result["stale_keys_deleted"]),
        },
    )
    return result
