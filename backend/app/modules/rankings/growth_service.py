# backend/app/modules/rankings/growth_service.py
"""The personal growth profile (spec §19; plan 05 task 8).

One read service assembling the growth page's whole answer from the
same sources every other surface trusts:

- **Ranking figures come from the authoritative aggregates.** Month
  points and total earned are ``RankingRepository`` ledger sums
  (``affects_ranking`` rows bucketed by ``ranking_effective_at`` in
  BUSINESS_TIMEZONE periods — redemptions and other non-ranking rows
  never count, spec §17.1; a reversal repairs the period it attributes
  to, §17.2). The CURRENT month rank is read from the Redis monthly
  board — the same board ``GET /rankings/monthly`` serves — so the
  growth page and the leaderboard can never disagree about "this
  month's rank".
- **Claim facts share the honor service's §19 judgment.** Completed
  count, on-time count (and the ratio), and the current streak are the
  extracted ``honor_service`` query helpers: a claim is on-time iff its
  FINAL valid reward lock opened from a submission with ``submitted_at
  <= deadline_at``, proxied by ``reward_tier_locked == 100`` (the §9.3
  ladder's on-time arm; the §11.3 re-lock clamp forces a re-locked
  claim to <= 20). An empty shell that was INVALIDATE_REWARD_LOCK'd
  and repaired by a LATE valid submission is therefore LATE — the
  pre-deadline shell never counts (spec §19's explicit ruling).
- **Best historical monthly rank is recomputed from PostgreSQL**, not
  scraped from Redis: historical monthly keys are rebuildable state
  (§17.3), while the ledger is the truth. Every business month with
  ranking traffic is aggregated (``distinct_effective_at`` → months),
  and the user's position in each is the count of strictly higher
  scores plus one. A user absent from a month's ledger has no position
  there. Ties share a position here (count-strictly-greater + 1),
  which can differ from the Redis board's deterministic tie split —
  acceptable for a "best ever" figure and documented by this note.
- **Honors are the honor service's read** (``list_user_honors``): the
  growth page lists exactly what the grant machinery awarded, so no
  second honor vocabulary can drift.
- Read-only end to end: this service writes nothing, commits nothing,
  and touches claims/submissions-derived state only through the
  sanctioned read-only seams (the tasks module's ORM models the way
  ``honor_service`` already reads them).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import get_settings
from app.modules.rankings.honor_service import (
    HonorService,
    OwnedHonor,
    completion_counts,
    current_on_time_streak,
)
from app.modules.rankings.periods import business_month, month_bounds, monthly_key
from app.modules.rankings.repository import RankingRepository

__all__ = ["GrowthProfile", "GrowthService"]


@dataclass(frozen=True, slots=True)
class GrowthProfile:
    """The §19 growth page answer.

    ``month_rank``/``best_month_rank`` are 1-based positional ranks;
    ``None`` means the user holds no position (no ranking score in the
    current month / no historical month at all). ``on_time_ratio`` is
    ``on_time_count / completed_count`` and ``0.0`` for a user with no
    completions yet.
    """

    month_points: int
    month_rank: int | None
    total_earned_points: int
    completed_count: int
    on_time_count: int
    on_time_ratio: float
    current_streak: int
    best_month_rank: int | None
    honors: list[OwnedHonor]


class GrowthService:
    """``growth_profile`` over the ledger aggregates, the Redis monthly
    board, the shared §19 claim facts, and the honor read."""

    def __init__(
        self,
        *,
        clock: Clock,
        repository: RankingRepository | None = None,
        honors: HonorService | None = None,
        tz: ZoneInfo | None = None,
    ) -> None:
        self._clock = clock
        self._repository = repository if repository is not None else RankingRepository()
        self._honors = honors if honors is not None else HonorService()
        self._tz = tz if tz is not None else ZoneInfo(get_settings().business_timezone)

    async def growth_profile(
        self, session: AsyncSession, redis: Redis, user_id: UUID
    ) -> GrowthProfile:
        """Assemble the whole §19 profile; read-only on every store."""
        now = self._clock.now()
        month = business_month(now, self._tz)
        month_start, month_end = month_bounds(month, self._tz)

        month_points = await self._repository.user_score(
            session, user_id, month_start, month_end
        )
        total_earned = await self._repository.user_score(session, user_id, None, None)
        completed, on_time = await completion_counts(session, user_id)
        honors = await self._honors.list_user_honors(session, user_id)

        return GrowthProfile(
            month_points=month_points,
            month_rank=await self._month_rank(redis, user_id, month),
            total_earned_points=total_earned,
            completed_count=completed,
            on_time_count=on_time,
            on_time_ratio=(on_time / completed) if completed else 0.0,
            current_streak=await current_on_time_streak(session, user_id),
            best_month_rank=await self._best_month_rank(session, user_id),
            honors=honors,
        )

    async def _month_rank(self, redis: Redis, user_id: UUID, month: date) -> int | None:
        """The user's 1-based position on the CURRENT monthly Redis board
        (``None`` when they hold no score there). This is deliberately
        the same projection the monthly leaderboard serves, so the two
        surfaces quote one number."""
        # The typed seam over redis-py's ResponseT union (the ranking
        # service's ruling): zrevrank is int | None at runtime.
        zero_based = cast(
            "int | None", await redis.zrevrank(monthly_key(month), str(user_id))
        )
        if zero_based is None:
            return None
        return int(zero_based) + 1

    async def _best_month_rank(
        self, session: AsyncSession, user_id: UUID
    ) -> int | None:
        """The user's best 1-based position across every business month
        the ledger has traffic for, recomputed from PostgreSQL (spec
        §17.3: Redis is a projection; the ledger is the rebuildable
        truth). See the module docstring for the tie-position note."""
        instants = await self._repository.distinct_effective_at(session)
        months = sorted({business_month(instant, self._tz) for instant in instants})
        best: int | None = None
        for month in months:
            start, end = month_bounds(month, self._tz)
            scores = await self._repository.range_scores(session, start, end)
            mine = scores.get(user_id)
            if mine is None:
                continue  # no position in this month
            rank = sum(1 for score in scores.values() if score > mine) + 1
            if best is None or rank < best:
                best = rank
        return best
