# backend/app/modules/rankings/repository.py
"""PostgreSQL ledger aggregation for rankings (spec §17/§17.1/§17.3).

PostgreSQL is the ONLY source of truth for ranking scores: every Redis
member's score must equal one of these aggregates. The queries are pure
SQL — SUM of ``amount`` over ``affects_ranking`` rows inside a UTC
instant range — because period boundaries are computed Python-side
(see ``periods.py`` for the documented zoneinfo choice).

Range shape: half-open ``[starts_at, ends_at)`` in UTC, either bound
``None`` meaning unbounded (``ranking:all`` passes both as ``None``).
``ranking_effective_at`` is NOT NULL exactly when ``affects_ranking``
(the ledger's coherence CHECK), so the range predicates never trip on
NULLs. A user with no rows in range scores 0 (COALESCE), which is a
representable board position after a full reversal (spec §17.2).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.points.models import PointsLedger

__all__ = ["RankingRepository"]


def _range_filters(
    starts_at: datetime | None, ends_at: datetime | None
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [PointsLedger.affects_ranking.is_(True)]
    if starts_at is not None:
        filters.append(PointsLedger.ranking_effective_at >= starts_at)
    if ends_at is not None:
        filters.append(PointsLedger.ranking_effective_at < ends_at)
    return filters


class RankingRepository:
    """The authoritative aggregates behind every Redis score."""

    async def user_score(
        self,
        session: AsyncSession,
        user_id: UUID,
        starts_at: datetime | None,
        ends_at: datetime | None,
    ) -> int:
        """One user's ranking score in a period (0 when they have no
        ranking-affecting rows there)."""
        stmt = select(func.coalesce(func.sum(PointsLedger.amount), 0)).where(
            PointsLedger.user_id == user_id,
            *_range_filters(starts_at, ends_at),
        )
        total = await session.scalar(stmt)
        return int(total) if total is not None else 0

    async def range_scores(
        self,
        session: AsyncSession,
        starts_at: datetime | None,
        ends_at: datetime | None,
    ) -> dict[UUID, int]:
        """Every user's score in a period — the member map a full-key
        rebuild writes (users absent from the ledger are absent here)."""
        stmt = (
            select(PointsLedger.user_id, func.sum(PointsLedger.amount))
            .where(*_range_filters(starts_at, ends_at))
            .group_by(PointsLedger.user_id)
        )
        rows = (await session.execute(stmt)).all()
        return {user_id: int(total) for user_id, total in rows}

    async def distinct_effective_at(self, session: AsyncSession) -> list[datetime]:
        """Every distinct ranking attribution instant — the rebuild
        derives the set of business days/months that ever had traffic."""
        stmt = (
            select(PointsLedger.ranking_effective_at)
            .where(PointsLedger.affects_ranking.is_(True))
            .distinct()
        )
        rows = (await session.execute(stmt)).all()
        return [row[0] for row in rows if row[0] is not None]
