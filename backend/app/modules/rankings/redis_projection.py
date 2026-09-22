# backend/app/modules/rankings/redis_projection.py
"""The Redis sorted-set ranking projection (spec §17.3; interfaces.md
"Ranking projection"; plan 05 task 6).

Redis is a rebuildable projection, never a second source of truth: every
score this module writes is RECOMPUTED from the PostgreSQL ledger and
``ZADD``ed as an ABSOLUTE value. ``ZINCRBY`` is banned on purpose — the
projection trigger is a retryable post-commit enqueue (spec §32), and an
increment replayed twice would double-count; a recompute replayed twice
converges. A full Redis loss heals via ``rebuild_all``.

Trigger surface (outbox direction, Core Primitives): the ledger-writing
service calls ``RankingUpdateDispatcher.enqueue_ranking_update`` AFTER
its transaction commits; the Celery job behind that port lands here in
``apply_ranking_update``. This class NEVER enqueues anything itself and
never compensates business writes — a missed update is repaired by
rebuild, not by touching the ledger.

Period attribution (spec §17.2): ``apply_ranking_update`` takes the
entry's ``ranking_effective_at``, so a September-posted reversal of an
August reward (which carries August's effective time) recomputes
August's day/month and all-time — September's own scores stay put.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.rankings.periods import (
    ALL_TIME_KEY,
    business_day,
    business_month,
    daily_key,
    day_bounds,
    month_bounds,
    monthly_key,
)
from app.modules.rankings.repository import RankingRepository

__all__ = [
    "RankingRedisProjection",
    "RankingUpdateDispatcher",
]


@runtime_checkable
class RankingUpdateDispatcher(Protocol):
    """The post-commit handoff of one changed ranking-affecting entry.

    ``enqueue_ranking_update`` is a SYNC publish (a ``.delay`` call in
    the production binding; a list append in fakes), invoked only AFTER
    the ledger transaction committed — never inside it (Core Primitives
    outbox rule: a failed projection must never roll back committed
    business writes). ``request_id`` is the correlation id threaded into
    the job's logs; callers without one get a generated hex.
    """

    def enqueue_ranking_update(
        self,
        user_id: UUID,
        ranking_effective_at: datetime,
        request_id: str | None = None,
    ) -> None: ...


def _affected_periods(
    ranking_effective_at: datetime, tz: ZoneInfo
) -> list[tuple[str, datetime | None, datetime | None]]:
    """The keys a changed entry touches: its business day, business
    month, and all-time, each with the period's UTC instant range."""
    day = business_day(ranking_effective_at, tz)
    month = business_month(ranking_effective_at, tz)
    day_start, day_end = day_bounds(day, tz)
    month_start, month_end = month_bounds(month, tz)
    return [
        (daily_key(day), day_start, day_end),
        (monthly_key(month), month_start, month_end),
        (ALL_TIME_KEY, None, None),
    ]


class RankingRedisProjection:
    """Writes the Redis boards from PostgreSQL aggregates.

    ``repository`` and ``tz`` are injectable (tests pin the timezone;
    production defaults read ``settings.business_timezone`` — lazily, so
    importing this module stays environment-free).
    """

    def __init__(
        self,
        repository: RankingRepository | None = None,
        tz: ZoneInfo | None = None,
    ) -> None:
        self._repository = repository if repository is not None else RankingRepository()
        self._tz = tz if tz is not None else ZoneInfo(get_settings().business_timezone)

    async def apply_ranking_update(
        self,
        session: AsyncSession,
        redis: Redis,
        user_id: UUID,
        ranking_effective_at: datetime,
    ) -> dict[str, Any]:
        """Recompute ONE user's scores for every affected period and
        ``ZADD`` the absolute values (the retry-converging update).

        Reads join the caller's transaction (``session`` in, answers
        out); the caller commits the ledger first, then enqueues the job
        that lands here — so the aggregates this reads are committed
        facts in production, and the test-shaped rollback merely makes
        the read see flushed-but-uncommitted rows, which the absolute
        ZADD tolerates by construction.
        """
        scores: dict[str, int] = {}
        for key, starts_at, ends_at in _affected_periods(
            ranking_effective_at, self._tz
        ):
            scores[key] = await self._repository.user_score(
                session, user_id, starts_at, ends_at
            )
        member = str(user_id)
        for key, score in scores.items():
            # Absolute overwrite: a replay writes the same aggregate
            # again instead of adding a second delta (spec §32).
            await redis.zadd(key, {member: score})
        return {"user_id": member, "updated_keys": sorted(scores), "scores": scores}

    async def rebuild_all(self, session: AsyncSession, redis: Redis) -> dict[str, Any]:
        """Recompute EVERY board from PostgreSQL and rewrite each key
        wholesale — the Redis-loss recovery path (spec §17.3, §39).

        The set of keys is derived from the ledger's distinct
        ``ranking_effective_at`` instants (bucketed to business days and
        months Python-side), so a key that once had traffic is rebuilt
        even when it is currently empty. Daily/monthly keys in the
        ``ranking:`` namespace that the ledger does NOT back are stale
        projection debris and are deleted — after a rebuild, Redis holds
        exactly the PostgreSQL-derived boards, nothing more. Each key is
        rewritten atomically (``DELETE`` + batched ``ZADD`` in one
        transactional pipeline), which also evicts stale members an
        incremental ZADD could never remove.
        """
        instants = await self._repository.distinct_effective_at(session)
        days = sorted({business_day(instant, self._tz) for instant in instants})
        months = sorted({business_month(instant, self._tz) for instant in instants})
        targets: list[tuple[str, datetime | None, datetime | None]] = []
        for day in days:
            start, end = day_bounds(day, self._tz)
            targets.append((daily_key(day), start, end))
        for month in months:
            start, end = month_bounds(month, self._tz)
            targets.append((monthly_key(month), start, end))
        targets.append((ALL_TIME_KEY, None, None))

        keys_rebuilt: list[str] = []
        members_written = 0
        for key, starts_at, ends_at in targets:
            scores = await self._repository.range_scores(session, starts_at, ends_at)
            async with redis.pipeline(transaction=True) as pipe:
                await pipe.delete(key)
                if scores:
                    await pipe.zadd(
                        key, {str(user_id): score for user_id, score in scores.items()}
                    )
                await pipe.execute()
            keys_rebuilt.append(key)
            members_written += len(scores)

        stale_keys_deleted = await self._delete_unbacked_keys(
            redis, {key for key, _, _ in targets}
        )
        return {
            "keys_rebuilt": keys_rebuilt,
            "members_written": members_written,
            "stale_keys_deleted": stale_keys_deleted,
        }

    async def _delete_unbacked_keys(self, redis: Redis, rebuilt: set[str]) -> list[str]:
        """Delete daily/monthly ranking keys the rebuild did not write.

        Scans only this projection's namespace (``ranking:daily:*`` /
        ``ranking:monthly:*``); ``ranking:all`` is always rebuilt, so it
        is never scanned away. Handles bytes or str keys so the caller's
        ``decode_responses`` setting does not matter.
        """

        def _text(key: bytes | str) -> str:
            return key.decode() if isinstance(key, bytes) else key

        stale: list[str] = []
        for pattern in ("ranking:daily:*", "ranking:monthly:*"):
            async for raw_key in redis.scan_iter(match=pattern):
                key = _text(raw_key)
                if key not in rebuilt:
                    stale.append(key)
        if stale:
            await redis.delete(*stale)
        return stale
