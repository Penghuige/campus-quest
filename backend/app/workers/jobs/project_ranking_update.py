# backend/app/workers/jobs/project_ranking_update.py
"""Incremental ranking projection job (spec §17.3, §32 ranking-projection
idempotency; interfaces.md "Ranking projection"; backend-engineering
§12; plan 05 task 6).

An orchestration shell: ``user_id`` + ``ranking_effective_at`` (the
changed entry's period attribution) + ``request_id`` in -> construct
dependencies -> call ``RankingRedisProjection.apply_ranking_update``
(the recompute-and-ZADD that makes retries converge) -> JSON summary
out.

Trigger discipline (Core Primitives outbox direction): producers call
``CeleryRankingDispatcher.enqueue_ranking_update`` AFTER the ledger
transaction commits — never inside it. A failed or missed enqueue is
healed by ``rebuild_all_rankings``, never by compensating business
writes. Celery retries replay the original arguments, and because the
projection recomputes absolute scores from PostgreSQL, a replay
converges instead of double-counting — which is what makes the bounded
autoretry below safe.

Retry policy: Redis/OS transients retry with backoff, at most
``max_retries`` times; after that the job fails and the boards heal at
the next rebuild.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from celery import shared_task  # type: ignore[import-untyped]
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    from app.modules.rankings.redis_projection import RankingRedisProjection

logger = logging.getLogger(__name__)

#: Redis/OS transients (the broker/cache being briefly unavailable);
#: content-level outcomes are results, never exceptions.
_RETRYABLE_TRANSIENTS = (OSError, RedisError)

_MAX_RETRIES = 5


def run_ranking_projection(
    user_id: str,
    ranking_effective_at: str,
    *,
    request_id: str,
    session_source: Any = None,
    redis_source: Any = None,
    settings: Settings | None = None,
    projection: RankingRedisProjection | None = None,
) -> dict[str, Any]:
    """Construct the dependencies and call the projection (the §12
    shell); every dependency is injectable for database-free tests."""
    from app.modules.rankings.redis_projection import RankingRedisProjection
    from app.workers.jobs.rebuild_rankings import (
        _default_redis_source,
        _default_session_source,
    )

    parsed_effective_at = datetime.fromisoformat(ranking_effective_at)
    if parsed_effective_at.tzinfo is None:
        raise ValueError(
            "ranking_effective_at must be timezone-aware ISO text "
            "(persisted UTC instants)"
        )
    parsed_user_id = UUID(user_id)

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
            return await projection.apply_ranking_update(
                session, redis, parsed_user_id, parsed_effective_at
            )

    summary = asyncio.run(_call())
    return {
        "user_id": user_id,
        "request_id": request_id,
        "updated_keys": summary["updated_keys"],
        "scores": summary["scores"],
    }


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.project_ranking_update",
    autoretry_for=_RETRYABLE_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def project_ranking_update_job(
    self: Any, user_id: str, ranking_effective_at: str, request_id: str
) -> dict[str, Any]:
    """Recompute one user's affected ranking boards from PostgreSQL.

    Parameters are IDs/correlation strings only; retries replay them
    under the same request_id, and the recompute converges.
    """
    job_id = self.request.id
    logger.info(
        "project_ranking_update.start",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "user_id": user_id,
            "ranking_effective_at": ranking_effective_at,
        },
    )
    result = run_ranking_projection(
        user_id, ranking_effective_at, request_id=request_id
    )
    logger.info(
        "project_ranking_update.end",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "user_id": user_id,
            "updated_keys": result["updated_keys"],
        },
    )
    return result


class CeleryRankingDispatcher:
    """The production ``RankingUpdateDispatcher`` over
    ``project_ranking_update_job.delay`` (the post-commit trigger).

    The port itself is frozen in
    ``app.modules.rankings.redis_projection``; this is the workers-side
    binding the composition root hands to the ledger-writing service.
    The job module import stays INSIDE the call (mirroring the
    submissions dispatcher): importing it at consumer module load would
    drag the rankings module into every importer for a dispatch that
    may never fire.
    """

    def enqueue_ranking_update(
        self,
        user_id: UUID,
        ranking_effective_at: datetime,
        request_id: str | None = None,
    ) -> None:
        if ranking_effective_at.tzinfo is None:
            raise ValueError(
                "ranking_effective_at must be timezone-aware (persisted UTC instants)"
            )
        project_ranking_update_job.delay(
            str(user_id),
            ranking_effective_at.isoformat(),
            request_id if request_id is not None else uuid4().hex,
        )
