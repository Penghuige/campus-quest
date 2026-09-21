# backend/tests/integration/rankings/test_growth_api.py
"""Growth profile + ranking board HTTP APIs over real PostgreSQL + Redis
(spec §17 boards/privacy, §19 growth page, §28 URL shapes, §29 envelope;
plan 05 task 8; backend-engineering §3/§16).

Drives the real app (``create_app()`` — the rankings and points routers
mounted under /api/v1) with the rollback-harness session and the shared
rankings Redis database:

- the §19 growth metric core: completed claims including ONE invalidated
  empty-shell that a valid LATE submission repaired — the on-time ratio
  must judge by the FINAL valid reward lock (tier 20 re-lock clamp), not
  by the pre-deadline shell;
- month points/rank from the BUSINESS_TIMEZONE month (redemptions never
  count, spec §17.1), total earned, completed count, current streak
  (shared with the honor service's computation), best HISTORICAL monthly
  rank (distinct from the current month rank), and the owned honors;
- the board endpoints: top-N + my rank for daily/monthly/all with the
  §17/§40 privacy pin (entry field set is EXACTLY nickname /
  display_honor / score / rank) and the empty-board/no-score shape;
- around-me as a global-rank window on one board;
- the role boundary: student surfaces reject non-STUDENT roles with
  PERMISSION_DENIED and unauthenticated calls with 401.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.session_service import SessionService
from app.modules.points.enums import LedgerType
from app.modules.points.models import PointsLedger
from app.modules.rankings.honor_service import (
    HonorEvaluationEvent,
    HonorService,
    HonorTrigger,
)
from app.modules.rankings.periods import business_month, month_bounds
from app.modules.rankings.redis_projection import RankingRedisProjection
from app.modules.rankings.repository import RankingRepository
from app.modules.rankings.router import get_rankings_redis
from app.modules.submissions.models import RewardLockHistory
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PASSWORD = "correct-horse-battery"

# Anchored to the real now: access tokens must decode against wall-clock
# time (the submissions-API precedent), while every BUSINESS period in
# this file derives from the frozen clock for determinism.
_T0 = datetime.now(UTC).replace(microsecond=0)

_GROWTH_FIELDS = {
    "month_points",
    "month_rank",
    "total_earned_points",
    "completed_count",
    "on_time_count",
    "on_time_ratio",
    "current_streak",
    "best_month_rank",
    "honors",
}
_BOARD_FIELDS = {"entries", "my_rank", "my_score"}
# Spec §17/§40: the public entry is EXACTLY these four fields.
_ENTRY_FIELDS = {"nickname", "display_honor", "score", "rank"}


# --- fixtures -----------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_redis() -> AsyncIterator[aioredis.Redis]:
    """Redis client on the shared rankings test database, flushed around
    every test (the boards' whole state lives in keys)."""
    url = get_settings().redis_url
    if urlsplit(url).hostname not in _LOCAL_HOSTS:
        pytest.fail(f"Growth API tests refuse non-local Redis: {url!r}")
    client = aioredis.from_url(url, decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def api_app(
    db_session: AsyncSession, api_clock: FrozenClock, api_redis: aioredis.Redis
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    # The boards ride the per-test Redis client: the router's own
    # provider is process-cached, so its pooled connections would die
    # with the first test's event loop (the submissions-API seam).
    app.dependency_overrides[get_rankings_redis] = lambda: api_redis
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


# --- seeding helpers ----------------------------------------------------------------


def _user(username: str, role: Role = Role.STUDENT) -> User:
    return User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


async def _token(db: AsyncSession, clock: FrozenClock, user: User) -> dict[str, str]:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


def _task(owner: User, slug: str) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title=f"数据采集任务 {slug}",
        description="采集指定关键词下的笔记数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=50,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={
            "required_columns": [{"name": "url", "type": "string", "unique": True}]
        },
        submission_schema_version=1,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
    )


def _assignment(task: Task, slug: str) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=f"考研{slug}",
        availability_status=AssignmentAvailability.COMPLETED.value,
    )


def _completed_claim(
    user: User,
    task: Task,
    assignment: Assignment,
    *,
    tier: int,
    reward_locked_at: datetime,
    terminal_at: datetime,
) -> AssignmentClaim:
    """One COMPLETED claim carrying the §19 on-time judgment as its lock
    tier (100 = the §9.3 ladder's on-time arm; a re-lock after an
    invalidated shell is clamped to <= 20)."""
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=user.id,
        status=ClaimStatus.COMPLETED.value,
        claimed_at=terminal_at - timedelta(days=3),
        deadline_at=terminal_at - timedelta(days=1),
        grace_deadline_at=terminal_at - timedelta(days=1) + timedelta(hours=24),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=50,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.CONFIRMED.value,
        reward_tier_locked=tier,
        locked_reward_points=50 * tier // 100,
        reward_locked_at=reward_locked_at,
        terminal_at=terminal_at,
    )


def _reward(user: User, amount: int, effective_at: datetime) -> PointsLedger:
    return PointsLedger(
        user_id=user.id,
        ledger_type=LedgerType.ASSIGNMENT_REWARD,
        amount=amount,
        source_type="ASSIGNMENT_CLAIM",
        source_id=uuid4(),
        affects_balance=True,
        affects_ranking=True,
        ranking_effective_at=effective_at,
    )


def _spend(user: User, amount: int) -> PointsLedger:
    """A redemption spend: hits the balance, never the ranking (spec
    §17.1) — month points and total earned must both ignore it."""
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


def _shell_invalidation(claim: AssignmentClaim, reviewer: User) -> RewardLockHistory:
    """The §11.3 audit row for the empty-shell submission that opened a
    PREVISIONAL lock BEFORE the deadline and was then invalidated."""
    return RewardLockHistory(
        claim_id=claim.id,
        submission_id=None,
        lock_status_from=RewardLockStatus.NONE.value,
        lock_status_to=RewardLockStatus.INVALIDATED.value,
        reward_tier_locked=100,
        locked_reward_points=50,
        changed_by=reviewer.id,
        reason="空壳提交：文件无有效数据行",
    )


def _relock(claim: AssignmentClaim) -> RewardLockHistory:
    """The fresh PROVISIONAL re-lock the valid LATE submission opened
    (clamped to tier 20 by the §11.3 re-lock rule)."""
    return RewardLockHistory(
        claim_id=claim.id,
        submission_id=None,
        lock_status_from=RewardLockStatus.INVALIDATED.value,
        lock_status_to=RewardLockStatus.PROVISIONAL.value,
        reward_tier_locked=20,
        locked_reward_points=10,
        changed_by=None,
        reason=None,
    )


@pytest_asyncio.fixture
async def growth_world(
    db_session: AsyncSession, api_clock: FrozenClock, api_redis: aioredis.Redis
) -> dict[str, Any]:
    """The §19 scenario: student ``alice`` owns three completed claims —

    - oldest: genuinely late (tier 80);
    - middle: an empty-shell submission BEFORE the deadline that was
      INVALIDATE_REWARD_LOCK'd, then repaired by a valid LATE submission
      (final re-locked tier 20 — the FINAL valid lock is late);
    - newest: on time (tier 100);

    plus ledger history where alice leads the PREVIOUS business month
    but trails ``bob`` in the current one, and one non-ranking spend.
    """
    tz = ZoneInfo(get_settings().business_timezone)
    this_month = business_month(api_clock.now(), tz)
    month_start, _ = month_bounds(this_month, tz)
    in_month = month_start + timedelta(days=9, hours=10)  # safely inside
    previous_month = month_start - timedelta(days=10)

    teacher = _user("growth-teacher-0001", role=Role.TEACHER)
    alice = _user("growth-alice-0002")
    bob = _user("growth-bob-0003")
    db_session.add_all([teacher, alice, bob])
    await db_session.flush()

    task = _task(teacher, "growth")
    db_session.add(task)
    await db_session.flush()

    assignments = [_assignment(task, slug) for slug in ("late", "shell", "ontime")]
    db_session.add_all(assignments)
    await db_session.flush()

    late = _completed_claim(
        alice,
        task,
        assignments[0],
        tier=80,
        reward_locked_at=_T0 - timedelta(days=1),
        terminal_at=_T0 - timedelta(days=16),
    )
    shell = _completed_claim(
        alice,
        task,
        assignments[1],
        tier=20,
        # The shell landed before the deadline; the VALID submission that
        # re-locked the reward landed after it (the §19 rule this file
        # pins: the final valid lock decides, so this claim is LATE).
        reward_locked_at=late.deadline_at + timedelta(hours=2),
        terminal_at=_T0 - timedelta(days=13),
    )
    on_time = _completed_claim(
        alice,
        task,
        assignments[2],
        tier=100,
        reward_locked_at=_T0 - timedelta(days=1),
        terminal_at=_T0 - timedelta(days=9),
    )
    db_session.add_all([late, shell, on_time])
    await db_session.flush()
    db_session.add_all([_shell_invalidation(shell, teacher), _relock(shell)])

    ledger_rows = [
        # Current month: bob 500 > alice 300 -> alice's month rank is 2.
        _reward(alice, 300, in_month),
        _reward(bob, 500, in_month + timedelta(days=2)),
        # Previous month: alice 400 > bob 250 -> alice's best rank is 1.
        _reward(alice, 400, previous_month),
        _reward(bob, 250, previous_month + timedelta(days=1)),
        # A spend: -50 on the balance, invisible to every ranking figure.
        _spend(alice, -50),
    ]
    db_session.add_all(ledger_rows)
    await db_session.flush()

    # Every board from PostgreSQL: the same recovery path production
    # uses, so the API reads boards consistent with the seeded ledger.
    await RankingRedisProjection(repository=RankingRepository(), tz=tz).rebuild_all(
        db_session, api_redis
    )

    # Lifetime honors from the same facts the growth page shows.
    await HonorService().evaluate_honors(
        db_session,
        alice.id,
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED),
    )
    await db_session.flush()

    return {
        "teacher": teacher,
        "alice": alice,
        "bob": bob,
        "teacher_headers": await _token(db_session, api_clock, teacher),
        "alice_headers": await _token(db_session, api_clock, alice),
        "bob_headers": await _token(db_session, api_clock, bob),
    }


# --- the §19 growth profile ----------------------------------------------------------


async def test_growth_me_uses_final_valid_lock_for_on_time_ratio(
    client: httpx.AsyncClient, growth_world: dict[str, Any]
) -> None:
    response = await client.get(
        "/api/v1/growth/me", headers=growth_world["alice_headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _GROWTH_FIELDS

    # The shell's pre-deadline submission must NOT make its claim
    # on-time: only the newest tier-100 claim counts (1 of 3).
    assert body["completed_count"] == 3
    assert body["on_time_count"] == 1
    assert body["on_time_ratio"] == pytest.approx(1 / 3)

    # Streak walks newest-first and stops at the re-locked (late) claim.
    assert body["current_streak"] == 1

    # Ranking figures: current-month aggregate (spend ignored), the
    # current month board rank, the all-time aggregate, and the BEST
    # historical monthly rank (1 in the previous month — distinct from
    # the current month rank 2).
    assert body["month_points"] == 300
    assert body["month_rank"] == 2
    assert body["total_earned_points"] == 700
    assert body["best_month_rank"] == 1

    honor_names = {honor["name"] for honor in body["honors"]}
    assert honor_names == {"首次完成任务", "累计获得 500 积分"}
    for honor in body["honors"]:
        assert set(honor) == {"honor_id", "name", "honor_type", "period", "granted_at"}


async def test_growth_me_rejects_non_student_roles(
    client: httpx.AsyncClient, growth_world: dict[str, Any]
) -> None:
    response = await client.get(
        "/api/v1/growth/me", headers=growth_world["teacher_headers"]
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_growth_me_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/growth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# --- the board endpoints (spec §17) --------------------------------------------------


async def test_monthly_board_top_n_with_my_rank(
    client: httpx.AsyncClient, growth_world: dict[str, Any]
) -> None:
    response = await client.get(
        "/api/v1/rankings/monthly", headers=growth_world["alice_headers"]
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _BOARD_FIELDS

    assert body["my_rank"] == 2
    assert body["my_score"] == 300
    assert len(body["entries"]) == 2
    top = body["entries"][0]
    assert set(top) == _ENTRY_FIELDS  # the §17/§40 privacy pin
    assert top["nickname"] == growth_world["bob"].nickname
    assert top["display_honor"] is None
    assert top["score"] == 500
    assert top["rank"] == 1
    assert body["entries"][1]["rank"] == 2


async def test_all_time_board_and_daily_empty_shape(
    client: httpx.AsyncClient, growth_world: dict[str, Any]
) -> None:
    all_time = await client.get(
        "/api/v1/rankings/all", headers=growth_world["alice_headers"]
    )
    assert all_time.status_code == 200
    body = all_time.json()
    assert set(body) == _BOARD_FIELDS
    assert body["my_rank"] == 2
    assert body["my_score"] == 700  # 300 + 400; the spend never counts
    assert [entry["score"] for entry in body["entries"]] == [750, 700]

    # No traffic today: the daily board is an empty list and the caller
    # holds no position on it.
    daily = await client.get(
        "/api/v1/rankings/daily", headers=growth_world["alice_headers"]
    )
    assert daily.status_code == 200
    assert daily.json() == {"entries": [], "my_rank": None, "my_score": None}


async def test_around_me_is_a_global_rank_window(
    client: httpx.AsyncClient, growth_world: dict[str, Any]
) -> None:
    response = await client.get(
        "/api/v1/rankings/around-me",
        params={"period": "monthly", "radius": 1},
        headers=growth_world["alice_headers"],
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"entries"}
    # Ranks are GLOBAL board positions, not a re-ranked mini-board.
    assert [(entry["rank"], entry["score"]) for entry in body["entries"]] == [
        (1, 500),
        (2, 300),
    ]


async def test_ranking_boards_reject_non_student_roles(
    client: httpx.AsyncClient, growth_world: dict[str, Any]
) -> None:
    for path in ("daily", "monthly", "all", "around-me"):
        response = await client.get(
            f"/api/v1/rankings/{path}", headers=growth_world["teacher_headers"]
        )
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
