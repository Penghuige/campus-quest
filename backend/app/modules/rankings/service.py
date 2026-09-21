# backend/app/modules/rankings/service.py
"""The ranking read service (spec §17; interfaces.md "Ranking
projection"; plan 05 task 6).

Reads the Redis projection and enriches each member with display facts
through the frozen ``UserDirectory`` port — never identity ORM models.
The public entry is privacy-pinned (spec §17/§40): EXACTLY nickname /
display_honor / score / rank. ``user_id`` is internal machinery (the
ZSET member, and ``around_me``'s lookup key) and never appears on an
entry, so the student number / phone / email have no field to leak
through.

Rank semantics: positional ranks (1, 2, 3, ...) by score descending;
ties keep Redis's deterministic order (score desc, then member order)
and split positions rather than sharing one. ``around_me`` numbers
entries with their GLOBAL ranks — the window is a slice of the board,
not a re-ranked mini-board.

Resources follow the cross-module port shape (``session`` in, answers
out; the Redis client rides along per call): the frozen interface is
``top(period, limit)`` / ``around_me(user_id, period, radius)`` over
constructor-injected dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.identity.directory import UserDirectory
from app.modules.rankings.periods import RankingPeriod

__all__ = ["RankingEntry", "RankingService"]


def _scored_rows(raw: Any) -> list[tuple[str, float]]:
    """The typed seam over redis-py's ``ResponseT`` union: a WITHSCORES
    ``zrevrange`` on a ``decode_responses=True`` client is a list of
    (member string, score number) pairs (same ruling as the rate
    limiter's ``int()`` conversions)."""
    return cast("list[tuple[str, float]]", raw)


@dataclass(frozen=True, slots=True)
class RankingEntry:
    """One leaderboard row — the whole public shape (spec §17/§40).

    Deliberately just these four fields: there is no id field to forget
    to strip, and the API layer serializes exactly what is here.
    """

    nickname: str
    display_honor: str | None
    score: int
    rank: int


class RankingService:
    """``top`` / ``around_me`` over the Redis boards + the directory."""

    def __init__(self, directory: UserDirectory, tz: ZoneInfo | None = None) -> None:
        self._directory = directory
        self._tz = tz if tz is not None else ZoneInfo(get_settings().business_timezone)

    async def top(
        self,
        session: AsyncSession,
        redis: Redis,
        period: RankingPeriod,
        limit: int,
    ) -> list[RankingEntry]:
        """The highest-scoring ``limit`` entries of one board, rank 1
        first."""
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        rows = _scored_rows(
            await redis.zrevrange(period.redis_key(), 0, limit - 1, withscores=True)
        )
        entries: list[RankingEntry] = []
        for position, (member, score) in enumerate(rows):
            entry = await self._entry_for(session, member, score, position + 1)
            if entry is not None:
                entries.append(entry)
        return entries

    async def around_me(
        self,
        session: AsyncSession,
        redis: Redis,
        user_id: UUID,
        period: RankingPeriod,
        radius: int,
    ) -> list[RankingEntry]:
        """The caller's neighborhood on one board: up to ``radius``
        entries above and below, numbered with GLOBAL ranks.

        A user with no score in the period (nothing ranked yet) yields
        ``[]`` — there is no position to center on.
        """
        if radius < 0:
            raise ValueError(f"radius must be >= 0, got {radius}")
        key = period.redis_key()
        member = str(user_id)
        # The typed seam over redis-py's ResponseT union (same ruling as
        # the rate limiter's int()): a WITHSCORES zrevrange on this
        # client is member-string/score-number pairs.
        rank = cast("int | None", await redis.zrevrank(key, member))
        if rank is None:
            return []
        start = max(0, rank - radius)
        rows = _scored_rows(
            await redis.zrevrange(key, start, rank + radius, withscores=True)
        )
        entries: list[RankingEntry] = []
        for offset, (row_member, score) in enumerate(rows):
            entry = await self._entry_for(
                session, row_member, score, start + offset + 1
            )
            if entry is not None:
                entries.append(entry)
        return entries

    async def my_standing(
        self, redis: Redis, user_id: UUID, period: RankingPeriod
    ) -> tuple[int | None, int | None]:
        """The caller's own ``(rank, score)`` on one board (1-based
        rank; both ``None`` when they hold no score there).

        The boards' companion read for the "my rank" strip the growth
        page and the top-N responses carry: same keys, same projection,
        no nickname enrichment — the caller already knows who they are.
        Number-only by construction, so no §17/§40 field can leak.
        """
        member = str(user_id)
        rank = cast("int | None", await redis.zrevrank(period.redis_key(), member))
        # ``zscore`` is already typed ``float | None`` by redis-py here
        # (unlike ``zrevrank``'s wider union), so no cast seam is needed.
        score = await redis.zscore(period.redis_key(), member)
        return (
            int(rank) + 1 if rank is not None else None,
            int(score) if score is not None else None,
        )

    async def _entry_for(
        self, session: AsyncSession, member: str, score: float, rank: int
    ) -> RankingEntry | None:
        """Enrich one ZSET member; ``None` skips it (FK makes a missing
        account near-impossible — a skip keeps a corrupt row from
        breaking the whole board instead of guessing a nickname)."""
        profile = await self._directory.get_display_profile(session, UUID(member))
        if profile is None:
            return None
        return RankingEntry(
            nickname=profile.nickname,
            display_honor=profile.display_honor_title,
            score=int(score),
            rank=rank,
        )
