# backend/tests/integration/rankings/test_rankings.py
"""Ranking projection and read service against real PostgreSQL + Redis
(spec §17, §17.2/§17.3, §38.6/§38.9 rows, §32 ranking-projection
idempotency, §40 privacy; interfaces.md "Ranking projection").

PostgreSQL is authoritative: every Redis sorted-set score must equal the
``SUM(amount)`` of ``affects_ranking`` ledger rows bucketed by
``ranking_effective_at`` in BUSINESS_TIMEZONE natural periods. Redis runs
on the local test Redis from REDIS_URL (db 0 in the integration env
defaults), flushed around every test; the projection never ``ZINCRBY``s,
so retries converge and a flush is recoverable through ``rebuild_all``.

Privacy pin (spec §17/§40): the public entry carries EXACTLY nickname /
display_honor / score / rank — never the student number, phone, email, or
any user id.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.identity.directory import DisplayProfile
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType
from app.modules.points.models import PointsLedger
from app.modules.rankings.periods import (
    ALL_TIME_KEY,
    RankingPeriod,
    business_day,
    daily_key,
    day_bounds,
    month_bounds,
    monthly_key,
)
from app.modules.rankings.redis_projection import RankingRedisProjection
from app.modules.rankings.repository import RankingRepository
from app.modules.rankings.service import RankingEntry, RankingService

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Asia/Shanghai anchors (UTC+8, no DST): 15:30/16:30 UTC straddles the
# local midnight boundary 16:00 UTC.
_UTC_BEFORE_LOCAL_MIDNIGHT = datetime(2026, 9, 20, 15, 30, tzinfo=UTC)
_UTC_AFTER_LOCAL_MIDNIGHT = datetime(2026, 9, 20, 16, 30, tzinfo=UTC)


def _local(tz: ZoneInfo, *args: int) -> datetime:
    """A timezone-aware instant from business-local wall-clock parts."""
    return datetime(*args, tzinfo=tz)


# --- fixtures ------------------------------------------------------------------------


@pytest_asyncio.fixture
async def rankings_redis() -> AsyncIterator[aioredis.Redis]:
    """Redis client on the dedicated rankings test database, flushed
    around every test (the projection's whole state lives in keys)."""
    url = get_settings().redis_url
    parts = urlsplit(url)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"Ranking integration tests refuse non-local Redis: {url!r}")
    client = aioredis.from_url(url, decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def business_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().business_timezone)


@pytest.fixture
def projection(business_tz: ZoneInfo) -> RankingRedisProjection:
    return RankingRedisProjection(repository=RankingRepository(), tz=business_tz)


class _FakeDirectory:
    """The rankings tests' stand-in for ``UserDirectory``: only the
    display-profile read the service consumes, canned per user."""

    def __init__(self, profiles: dict[UUID, DisplayProfile]) -> None:
        self._profiles = profiles

    async def get_display_profile(
        self, session: AsyncSession, user_id: UUID
    ) -> DisplayProfile | None:
        return self._profiles.get(user_id)


@pytest.fixture
def fake_directory() -> _FakeDirectory:
    return _FakeDirectory({})


# --- seeding helpers -----------------------------------------------------------------

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _student(username: str, nickname: str) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _reward(
    user: User, amount: int, effective_at: datetime, **overrides: Any
) -> PointsLedger:
    fields: dict[str, Any] = {
        "user_id": user.id,
        "ledger_type": LedgerType.ASSIGNMENT_REWARD,
        "amount": amount,
        "source_type": "ASSIGNMENT_CLAIM",
        "source_id": uuid4(),
        "affects_balance": True,
        "affects_ranking": True,
        "ranking_effective_at": effective_at,
    }
    fields.update(overrides)
    return PointsLedger(**fields)


def _redemption(user: User, amount: int) -> PointsLedger:
    """A spend: hits the balance, never the ranking (spec §17.1)."""
    return PointsLedger(
        user_id=user.id,
        ledger_type=LedgerType.REWARD_REDEMPTION,
        amount=amount,
        source_type="REWARD_REDEMPTION",
        source_id=uuid4(),
        affects_balance=True,
        affects_ranking=False,
        ranking_effective_at=None,
    )


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


async def _seed_students(db_session: AsyncSession, count: int) -> list[User]:
    users = [_student(f"2025001000{index}", f"同学{index}") for index in range(count)]
    await _flush(db_session, *users)
    return users


# --- period math (pure; §38.9 timezone matrix) ---------------------------------------


def test_period_helpers_bucket_by_business_timezone(business_tz: ZoneInfo) -> None:
    """UTC 16:30 is the next Asia/Shanghai day (§38.9: UTC 日期与业务日期
    不同的排行榜) and the day/month bounds are exact UTC instant ranges."""
    assert business_day(_UTC_BEFORE_LOCAL_MIDNIGHT, business_tz) == date(2026, 9, 20)
    assert business_day(_UTC_AFTER_LOCAL_MIDNIGHT, business_tz) == date(2026, 9, 21)

    start, end = day_bounds(date(2026, 9, 21), business_tz)
    assert start == datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 16, 0, tzinfo=UTC)

    start, end = month_bounds(date(2026, 9, 1), business_tz)
    assert start == datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 30, 16, 0, tzinfo=UTC)

    assert daily_key(date(2026, 9, 21)) == "ranking:daily:2026-09-21"
    assert monthly_key(date(2026, 9, 1)) == "ranking:monthly:2026-09"
    assert ALL_TIME_KEY == "ranking:all"
    assert RankingPeriod.daily(date(2026, 9, 21)).redis_key() == (
        "ranking:daily:2026-09-21"
    )
    assert RankingPeriod.monthly(date(2026, 9, 21)).redis_key() == (
        "ranking:monthly:2026-09"  # any day of the month names the month
    )
    assert RankingPeriod.all_time().redis_key() == "ranking:all"


def test_period_bucketing_is_dst_safe() -> None:
    """§38.9 DST clause: with America/New_York, 2026-11-02 starts at
    05:00 UTC (EST) while 2026-11-01 started at 04:00 UTC (EDT) — the
    fall-back shifts the business-day boundary by an hour, so any
    hardcoded offset (or ±8 assumption) mis-buckets 04:30 UTC."""
    new_york = ZoneInfo("America/New_York")

    assert day_bounds(date(2026, 11, 1), new_york)[0] == datetime(
        2026, 11, 1, 4, 0, tzinfo=UTC
    )
    assert day_bounds(date(2026, 11, 2), new_york)[0] == datetime(
        2026, 11, 2, 5, 0, tzinfo=UTC
    )
    # 04:30 UTC on 11-02 is still 23:30 local on 11-01 (EST since the
    # fall-back): the previous business day's tail, not 11-02.
    assert business_day(datetime(2026, 11, 2, 4, 30, tzinfo=UTC), new_york) == date(
        2026, 11, 1
    )
    assert business_day(datetime(2026, 11, 2, 5, 30, tzinfo=UTC), new_york) == date(
        2026, 11, 2
    )


# --- projection bucketing (real PostgreSQL + Redis) ----------------------------------


@pytest.mark.integration
async def test_daily_and_monthly_projection_buckets_by_business_day(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
    business_tz: ZoneInfo,
) -> None:
    """Two rewards on the same UTC date but different BUSINESS_TIMEZONE
    days land in different daily keys; the month and all-time keys carry
    the sum; a redemption (affects_ranking=false) never counts (§17.1)."""
    (student,) = await _seed_students(db_session, 1)
    await _flush(
        db_session,
        _reward(student, 100, _UTC_BEFORE_LOCAL_MIDNIGHT),
        _reward(student, 200, _UTC_AFTER_LOCAL_MIDNIGHT),
        _redemption(student, -1000),
    )

    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, _UTC_BEFORE_LOCAL_MIDNIGHT
    )
    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, _UTC_AFTER_LOCAL_MIDNIGHT
    )

    member = str(student.id)
    assert await rankings_redis.zscore("ranking:daily:2026-09-20", member) == 100
    assert await rankings_redis.zscore("ranking:daily:2026-09-21", member) == 200
    assert await rankings_redis.zscore("ranking:monthly:2026-09", member) == 300
    assert await rankings_redis.zscore("ranking:all", member) == 300


@pytest.mark.integration
async def test_month_boundary_splits_by_business_month(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
    business_tz: ZoneInfo,
) -> None:
    """§38.9 month-end pair: local 23:59:59 stays in August, local
    00:00:00 (sixty seconds later in UTC) is already September."""
    (student,) = await _seed_students(db_session, 1)
    august_last_minute = _local(business_tz, 2026, 8, 31, 23, 59, 59)
    september_first_instant = _local(business_tz, 2026, 9, 1, 0, 0, 0)
    await _flush(
        db_session,
        _reward(student, 100, august_last_minute),
        _reward(student, 200, september_first_instant),
    )

    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, august_last_minute
    )
    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, september_first_instant
    )

    member = str(student.id)
    assert await rankings_redis.zscore("ranking:monthly:2026-08", member) == 100
    assert await rankings_redis.zscore("ranking:monthly:2026-09", member) == 200
    assert await rankings_redis.zscore("ranking:daily:2026-08-31", member) == 100
    assert await rankings_redis.zscore("ranking:daily:2026-09-01", member) == 200


@pytest.mark.integration
async def test_reversal_repairs_original_period_only(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
    business_tz: ZoneInfo,
) -> None:
    """§17.2: an August reward reversed in September carries the
    original's ranking_effective_at, so August (day, month) and all-time
    are corrected while September's own reward is untouched."""
    (student,) = await _seed_students(db_session, 1)
    august_effective = _local(business_tz, 2026, 8, 15, 10, 0, 0)
    september_effective = _local(business_tz, 2026, 9, 10, 9, 0, 0)
    original = _reward(student, 200, august_effective)
    await _flush(db_session, original)

    # The reward lands (and is projected) BEFORE the reversal exists.
    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, august_effective
    )
    member = str(student.id)
    assert await rankings_redis.zscore("ranking:monthly:2026-08", member) == 200

    await _flush(
        db_session,
        _reward(student, 50, september_effective),
        PointsLedger(
            user_id=student.id,
            ledger_type=LedgerType.ASSIGNMENT_REWARD_REVERSAL,
            amount=-200,
            source_type="ASSIGNMENT_CLAIM",
            source_id=original.source_id,
            affects_balance=True,
            affects_ranking=True,
            # The reversal re-uses the ORIGINAL's effective time: it
            # repairs the period the reward was credited to.
            ranking_effective_at=august_effective,
            reversal_of_id=original.id,
        ),
    )

    # September's own reward triggers on its effective time; the
    # September-POSTED reversal triggers on August's effective time.
    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, september_effective
    )
    await projection.apply_ranking_update(
        db_session, rankings_redis, student.id, august_effective
    )

    assert await rankings_redis.zscore("ranking:daily:2026-08-15", member) == 0
    assert await rankings_redis.zscore("ranking:monthly:2026-08", member) == 0
    assert await rankings_redis.zscore("ranking:monthly:2026-09", member) == 50
    assert await rankings_redis.zscore("ranking:daily:2026-09-10", member) == 50
    assert await rankings_redis.zscore("ranking:all", member) == 50


@pytest.mark.integration
async def test_projection_retry_converges_to_postgres_aggregate(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
    business_tz: ZoneInfo,
) -> None:
    """§32 ranking-projection idempotency: replaying the same trigger
    twice must equal the PostgreSQL aggregate ONCE — ZADD absolute
    scores, never ZINCRBY."""
    (student,) = await _seed_students(db_session, 1)
    effective = _UTC_AFTER_LOCAL_MIDNIGHT
    await _flush(
        db_session,
        _reward(student, 100, effective),
        _reward(student, 200, effective),
    )

    for _ in range(2):
        await projection.apply_ranking_update(
            db_session, rankings_redis, student.id, effective
        )

    repository = RankingRepository()
    start, end = day_bounds(business_day(effective, business_tz), business_tz)
    expected = await repository.user_score(db_session, student.id, start, end)
    assert expected == 300
    for key in ("ranking:daily:2026-09-21", "ranking:monthly:2026-09", "ranking:all"):
        assert await rankings_redis.zscore(key, str(student.id)) == expected


# --- the read service ----------------------------------------------------------------


async def _seed_scores(
    db_session: AsyncSession,
    projection: RankingRedisProjection,
    rankings_redis: aioredis.Redis,
    scores: list[int],
) -> list[User]:
    """One user per score, all in the same business day/month."""
    users = await _seed_students(db_session, len(scores))
    effective = _UTC_AFTER_LOCAL_MIDNIGHT
    for user, score in zip(users, scores, strict=True):
        await _flush(db_session, _reward(user, score, effective))
        await projection.apply_ranking_update(
            db_session, rankings_redis, user.id, effective
        )
    return users


@pytest.mark.integration
async def test_top_orders_desc_and_enriches_display_profile(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
) -> None:
    """top() returns score-descending entries whose nickname/honor come
    from the directory port, and a non-positive limit is rejected."""
    users = await _seed_scores(db_session, projection, rankings_redis, [300, 200, 100])
    profiles = {
        user.id: DisplayProfile(
            nickname=f"昵称{index}",
            display_honor_title="卷王" if index == 0 else None,
        )
        for index, user in enumerate(users)
    }
    service = RankingService(directory=_FakeDirectory(profiles))

    top = await service.top(
        db_session,
        rankings_redis,
        RankingPeriod.daily(date(2026, 9, 21)),
        limit=2,
    )

    assert [
        (entry.rank, entry.nickname, entry.display_honor, entry.score) for entry in top
    ] == [
        (1, "昵称0", "卷王", 300),
        (2, "昵称1", None, 200),
    ]
    with pytest.raises(ValueError):
        await service.top(db_session, rankings_redis, RankingPeriod.all_time(), limit=0)


@pytest.mark.integration
async def test_around_me_windows_global_ranks(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
) -> None:
    """around_me centers the caller's GLOBAL rank (the window slice, not
    the window, numbers the ranks) and answers [] for a user with no
    score in the period."""
    users = await _seed_scores(db_session, projection, rankings_redis, [300, 200, 100])
    profiles = {
        user.id: DisplayProfile(nickname=f"昵称{index}")
        for index, user in enumerate(users)
    }
    service = RankingService(directory=_FakeDirectory(profiles))
    period = RankingPeriod.monthly(date(2026, 9, 1))

    middle = await service.around_me(
        db_session, rankings_redis, users[1].id, period, radius=1
    )
    assert [(entry.rank, entry.score) for entry in middle] == [
        (1, 300),
        (2, 200),
        (3, 100),
    ]

    top_user = await service.around_me(
        db_session, rankings_redis, users[0].id, period, radius=1
    )
    assert [(entry.rank, entry.score) for entry in top_user] == [(1, 300), (2, 200)]

    bottom = await service.around_me(
        db_session, rankings_redis, users[2].id, period, radius=5
    )
    assert [(entry.rank, entry.score) for entry in bottom] == [
        (1, 300),
        (2, 200),
        (3, 100),
    ]

    absent = await service.around_me(
        db_session, rankings_redis, uuid4(), period, radius=1
    )
    assert absent == []


@pytest.mark.integration
async def test_ranking_entry_exposes_exactly_the_public_fields(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
) -> None:
    """The privacy pin (spec §17/§40): nickname/display_honor/score/rank
    and NOTHING else — no user id the shape could leak through."""
    users = await _seed_scores(db_session, projection, rankings_redis, [300])
    service = RankingService(
        directory=_FakeDirectory({users[0].id: DisplayProfile(nickname="昵称0")})
    )

    top = await service.top(
        db_session, rankings_redis, RankingPeriod.all_time(), limit=10
    )

    assert len(top) == 1
    assert {field.name for field in dataclasses.fields(RankingEntry)} == {
        "nickname",
        "display_honor",
        "score",
        "rank",
    }
    # The rendered shape (what an API layer would serialize) carries no
    # identifier: the user id exists only as the ZSET member internally.
    rendered = dataclasses.asdict(top[0])
    assert set(rendered) == {"nickname", "display_honor", "score", "rank"}
    assert str(users[0].id) not in str(rendered)
    assert users[0].username not in str(rendered)


@pytest.mark.integration
async def test_real_directory_enrichment_reads_nickname(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
) -> None:
    """End-to-end with the concrete adapter: nickname comes from the
    users table through the frozen port; the honor title is the None
    placeholder until the honors module lands (Task 7)."""
    from app.modules.identity.directory import SqlAlchemyUserDirectory

    await _seed_scores(db_session, projection, rankings_redis, [250])
    service = RankingService(directory=SqlAlchemyUserDirectory())

    top = await service.top(
        db_session, rankings_redis, RankingPeriod.all_time(), limit=10
    )

    assert [
        (entry.nickname, entry.display_honor, entry.score, entry.rank) for entry in top
    ] == [("同学0", None, 250, 1)]


# --- rebuild (Redis loss recovery) ----------------------------------------------------


async def _snapshot(
    service: RankingService,
    db_session: AsyncSession,
    redis: aioredis.Redis,
    periods: list[RankingPeriod],
    around: tuple[UUID, RankingPeriod],
) -> Any:
    tops = {
        period.redis_key(): [
            (e.rank, e.nickname, e.score)
            for e in await service.top(db_session, redis, period, limit=10)
        ]
        for period in periods
    }
    around_me = [
        (e.rank, e.nickname, e.score)
        for e in await service.around_me(
            db_session, redis, around[0], around[1], radius=2
        )
    ]
    return tops, around_me


@pytest.mark.integration
async def test_redis_loss_rebuild_restores_identical_results(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
) -> None:
    """§17.3/§39: Redis is a projection — FLUSHDB, rebuild from
    PostgreSQL, and Top N + around-me are byte-identical before/after."""
    users = await _seed_scores(db_session, projection, rankings_redis, [300, 150])
    # A second business day/month so the rebuild spans several keys.
    other_day = _UTC_BEFORE_LOCAL_MIDNIGHT
    await _flush(db_session, _reward(users[1], 400, other_day))
    await projection.apply_ranking_update(
        db_session, rankings_redis, users[1].id, other_day
    )

    service = RankingService(
        directory=_FakeDirectory(
            {
                user.id: DisplayProfile(nickname=f"昵称{index}")
                for index, user in enumerate(users)
            }
        )
    )
    periods = [
        RankingPeriod.daily(date(2026, 9, 20)),
        RankingPeriod.daily(date(2026, 9, 21)),
        RankingPeriod.monthly(date(2026, 9, 1)),
        RankingPeriod.all_time(),
    ]
    before_tops, before_around = await _snapshot(
        service,
        db_session,
        rankings_redis,
        periods,
        (users[1], RankingPeriod.all_time()),
    )
    assert before_tops["ranking:all"] is not None

    await rankings_redis.flushdb()
    assert (
        await service.top(
            db_session, rankings_redis, RankingPeriod.all_time(), limit=10
        )
        == []
    )

    summary = await projection.rebuild_all(db_session, rankings_redis)

    rebuilt_days = {"ranking:daily:2026-09-20", "ranking:daily:2026-09-21"}
    rebuilt_months = {"ranking:monthly:2026-09"}
    assert set(summary["keys_rebuilt"]) == rebuilt_days | rebuilt_months | {
        "ranking:all"
    }
    after_tops, after_around = await _snapshot(
        service,
        db_session,
        rankings_redis,
        periods,
        (users[1], RankingPeriod.all_time()),
    )
    assert after_tops == before_tops
    assert after_around == before_around


@pytest.mark.integration
async def test_rebuild_evicts_stale_members(
    db_session: AsyncSession,
    rankings_redis: aioredis.Redis,
    projection: RankingRedisProjection,
) -> None:
    """The wholesale DELETE + ZADD rewrite drops members PostgreSQL no
    longer has (and wholly stale keys), which a converged incremental
    ZADD could never remove."""
    users = await _seed_scores(db_session, projection, rankings_redis, [300])
    live_key = "ranking:daily:2026-09-21"
    stale_key = "ranking:daily:2020-01-01"
    await rankings_redis.zadd(live_key, {str(uuid4()): 999})
    await rankings_redis.zadd(stale_key, {str(uuid4()): 5})

    await projection.rebuild_all(db_session, rankings_redis)

    assert await rankings_redis.zcard(live_key) == 1  # only the PG member
    assert await rankings_redis.exists(stale_key) == 0
    assert await rankings_redis.zscore(live_key, str(users[0].id)) == 300
