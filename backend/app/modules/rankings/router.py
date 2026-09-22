# backend/app/modules/rankings/router.py
"""Ranking board and growth HTTP APIs: thin routes + the module's
composition root (spec §17 boards/我的附近, §19 growth page, §28 URL
shapes, §40 privacy; plan 05 task 8; backend-engineering §3/§9/§16).

Every route is thin — resolve the actor guard, derive the period from
the BUSINESS clock, call one service, serialize an explicit DTO — and
owns no persistence logic. The design decisions that live here:

Endpoints
---------

Student surfaces (``require_active_student_actor`` — spec §4.1: the
leaderboard-and-growth strip is Student territory; staff analytics
belong to Plan 08's admin surfaces, and a 403 pins that boundary
instead of an empty answer):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/rankings/daily`` — today's board (BUSINESS_TIMEZONE
             natural day): top N + the caller's own rank/score.
GET          ``/rankings/monthly`` — the current business month's board,
             same shape.
GET          ``/rankings/all`` — the all-time board, same shape.
GET          ``/rankings/around-me?period=&radius=`` — the caller's
             neighborhood on one board, numbered with GLOBAL ranks.
GET          ``/growth/me`` — the §19 personal growth profile.
===========  =========================================================

- **Privacy is pinned by the DTO, not by filtering** (spec §17/§40):
  ``RankingEntryResponse`` enumerates EXACTLY nickname / display_honor
  / score / rank — the RankingEntry shape — so the student number,
  phone, email, and the ZSET member (user id) have no field to leak
  through; the "my" strip carries only numbers.
- **The period is a server decision.** ``daily``/``monthly`` address
  the CURRENT business day/month derived from the injectable clock —
  no client-chosen historical date parameter in V1 (the §17.2 period
  semantics stay server-owned); ``around-me`` picks a board by name
  (``daily|monthly|all``, pattern-validated) because that is a view
  choice, not a period computation.
- Typed exceptions: every rankings-module error is a ``ValueError``
  only on programmer input (limit/radius bounds are Query-validated
  first); Redis outages surface through the generic handler. No
  module-specific envelope mapping is needed.
- **Providers are the module composition root.** The Redis client is
  process-cached (the tasks-router seam: ``dependency_overrides`` must
  be able to retarget it in tests); the services are assembled per
  request from the injected clock/settings; the honor/growth wiring
  reuses the identity directory for nickname enrichment, never
  identity ORM models (interfaces.md "Identity directory").
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_student_actor,
)
from app.modules.identity.directory import SqlAlchemyUserDirectory
from app.modules.identity.events import Actor
from app.modules.rankings.growth_service import GrowthService
from app.modules.rankings.periods import (
    RankingPeriod,
    business_day,
    business_month,
)
from app.modules.rankings.service import RankingService

# --- pagination / window bounds (the documented offset choice) ----------------------

DEFAULT_BOARD_LIMIT = 20
MAX_BOARD_LIMIT = 50
DEFAULT_AROUND_RADIUS = 5
MAX_AROUND_RADIUS = 50

_PERIOD_PATTERN = r"^(daily|monthly|all)$"


# --- transport DTOs (explicit field sets; privacy by construction) ------------------


class RankingEntryResponse(BaseModel):
    """One public leaderboard row — EXACTLY the spec §17/§40 shape; any
    additional field (student number, contact, user id) is unrepresentable
    because the model has no field for it."""

    model_config = ConfigDict(extra="forbid")

    nickname: str
    display_honor: str | None
    score: int
    rank: int


class BoardResponse(BaseModel):
    """One board read: the top-N entries plus the caller's own standing
    (numbers only; ``null`` while the caller holds no score)."""

    model_config = ConfigDict(extra="forbid")

    entries: list[RankingEntryResponse]
    my_rank: int | None = None
    my_score: int | None = None


class AroundMeResponse(BaseModel):
    """The caller's neighborhood, numbered with GLOBAL board ranks — a
    slice of the board, never a re-ranked mini-board."""

    model_config = ConfigDict(extra="forbid")

    entries: list[RankingEntryResponse]


class OwnedHonorResponse(BaseModel):
    """One honor the caller owns (the §19 已获得荣誉 row)."""

    model_config = ConfigDict(extra="forbid")

    honor_id: UUID
    name: str
    honor_type: str
    period: str | None
    granted_at: datetime


class GrowthResponse(BaseModel):
    """The §19 growth page answer; every figure is the caller's own."""

    model_config = ConfigDict(extra="forbid")

    month_points: int
    month_rank: int | None
    total_earned_points: int
    completed_count: int
    on_time_count: int
    on_time_ratio: float
    current_streak: int
    best_month_rank: int | None
    honors: list[OwnedHonorResponse]


# --- provider dependencies (module composition root) --------------------------------


@lru_cache
def get_rankings_redis() -> aioredis.Redis:
    """Process-wide Redis client for the ranking boards.

    ``decode_responses=True`` (the projection/read-service ruling);
    tests override this dependency to point at the flushed test
    database — the tasks-router seam, so a direct call inside a provider
    cannot dodge ``dependency_overrides``.
    """
    return aioredis.from_url(get_settings().redis_url, decode_responses=True)


RedisDep = Annotated[aioredis.Redis, Depends(get_rankings_redis)]
ClockDep = Annotated[Clock, Depends(get_business_clock)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_ranking_service(settings: AppSettings) -> RankingService:
    return RankingService(
        directory=SqlAlchemyUserDirectory(),
        tz=ZoneInfo(settings.business_timezone),
    )


def get_growth_service(clock: ClockDep, settings: AppSettings) -> GrowthService:
    return GrowthService(clock=clock, tz=ZoneInfo(settings.business_timezone))


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
StudentActor = Annotated[Actor, Depends(require_active_student_actor)]
RankingServiceDep = Annotated[RankingService, Depends(get_ranking_service)]
GrowthServiceDep = Annotated[GrowthService, Depends(get_growth_service)]

BoardLimit = Annotated[int, Query(ge=1, le=MAX_BOARD_LIMIT)]
AroundRadius = Annotated[int, Query(ge=0, le=MAX_AROUND_RADIUS)]
BoardPeriod = Annotated[str, Query(pattern=_PERIOD_PATTERN)]

router = APIRouter()


# --- student surfaces (spec §17, §19, §28, §42) ---------------------------------------


def _entry_dto(entry: Any) -> RankingEntryResponse:
    return RankingEntryResponse(
        nickname=entry.nickname,
        display_honor=entry.display_honor,
        score=entry.score,
        rank=entry.rank,
    )


async def _read_board(
    actor: Actor,
    db: AsyncSession,
    redis: aioredis.Redis,
    rankings: RankingService,
    period: RankingPeriod,
    limit: int,
) -> BoardResponse:
    entries = await rankings.top(db, redis, period, limit)
    my_rank, my_score = await rankings.my_standing(redis, actor.user_id, period)
    return BoardResponse(
        entries=[_entry_dto(entry) for entry in entries],
        my_rank=my_rank,
        my_score=my_score,
    )


@router.get("/rankings/daily", response_model=BoardResponse)
async def daily_board(
    actor: StudentActor,
    db: DbSession,
    redis: RedisDep,
    rankings: RankingServiceDep,
    clock: ClockDep,
    settings: AppSettings,
    limit: BoardLimit = DEFAULT_BOARD_LIMIT,
) -> BoardResponse:
    """Today's board in BUSINESS_TIMEZONE: top N plus the caller's own
    rank and score."""
    period = RankingPeriod.daily(
        business_day(clock.now(), ZoneInfo(settings.business_timezone))
    )
    return await _read_board(actor, db, redis, rankings, period, limit)


@router.get("/rankings/monthly", response_model=BoardResponse)
async def monthly_board(
    actor: StudentActor,
    db: DbSession,
    redis: RedisDep,
    rankings: RankingServiceDep,
    clock: ClockDep,
    settings: AppSettings,
    limit: BoardLimit = DEFAULT_BOARD_LIMIT,
) -> BoardResponse:
    """The current business month's board: top N plus the caller's own
    rank and score."""
    period = RankingPeriod.monthly(
        business_month(clock.now(), ZoneInfo(settings.business_timezone))
    )
    return await _read_board(actor, db, redis, rankings, period, limit)


@router.get("/rankings/all", response_model=BoardResponse)
async def all_time_board(
    actor: StudentActor,
    db: DbSession,
    redis: RedisDep,
    rankings: RankingServiceDep,
    limit: BoardLimit = DEFAULT_BOARD_LIMIT,
) -> BoardResponse:
    """The all-time board: top N plus the caller's own rank and score."""
    return await _read_board(
        actor, db, redis, rankings, RankingPeriod.all_time(), limit
    )


@router.get("/rankings/around-me", response_model=AroundMeResponse)
async def around_me(
    actor: StudentActor,
    db: DbSession,
    redis: RedisDep,
    rankings: RankingServiceDep,
    clock: ClockDep,
    settings: AppSettings,
    period: BoardPeriod = "all",
    radius: AroundRadius = DEFAULT_AROUND_RADIUS,
) -> AroundMeResponse:
    """The caller's neighborhood on one named board (``daily`` /
    ``monthly`` / ``all``), numbered with GLOBAL ranks. A caller with no
    score on the board gets an empty window."""
    tz = ZoneInfo(settings.business_timezone)
    now = clock.now()
    if period == "daily":
        target: RankingPeriod = RankingPeriod.daily(business_day(now, tz))
    elif period == "monthly":
        target = RankingPeriod.monthly(business_month(now, tz))
    else:
        target = RankingPeriod.all_time()
    entries = await rankings.around_me(db, redis, actor.user_id, target, radius)
    return AroundMeResponse(entries=[_entry_dto(entry) for entry in entries])


@router.get("/growth/me", response_model=GrowthResponse)
async def my_growth(
    actor: StudentActor,
    db: DbSession,
    redis: RedisDep,
    growth: GrowthServiceDep,
) -> GrowthResponse:
    """The §19 personal growth profile: month points/rank, total earned,
    completed count, on-time ratio (final valid lock judgment), current
    streak, best historical monthly rank, and owned honors."""
    profile = await growth.growth_profile(db, redis, actor.user_id)
    return GrowthResponse(
        month_points=profile.month_points,
        month_rank=profile.month_rank,
        total_earned_points=profile.total_earned_points,
        completed_count=profile.completed_count,
        on_time_count=profile.on_time_count,
        on_time_ratio=profile.on_time_ratio,
        current_streak=profile.current_streak,
        best_month_rank=profile.best_month_rank,
        honors=[
            OwnedHonorResponse(
                honor_id=honor.honor_id,
                name=honor.name,
                honor_type=honor.honor_type,
                period=honor.period,
                granted_at=honor.granted_at,
            )
            for honor in profile.honors
        ],
    )
