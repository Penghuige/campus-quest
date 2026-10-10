# backend/tests/integration/tasks/test_marketplace_ordering.py
"""任务广场排序：可领在前、已领完在后（spec §42 小注记，owner 裁定）。

The marketplace list orders claimable tasks (at least one AVAILABLE
assignment) before depleted ones (zero AVAILABLE — all occupied /
completed / retired, or no units at all); within each group the
published_at DESC, id order is unchanged. Three acceptance shapes:

- group semantics: a claimable task published EARLIER still lists ahead
  of a depleted task published LATER; the depleted group itself keeps
  newest-first order; a PUBLISHED task with no units at all counts as
  depleted (nothing is claimable on it);
- pagination boundaries: with the documented offset choice, page N's
  tail and page N+1's head never misorder — including the boundary that
  crosses the group edge exactly;
- availability flips: claiming the last AVAILABLE unit through the real
  claim API moves the task behind depleted ones; abandoning it restores
  the claimable-first position (the claim/abandon services are the
  production writers of availability_status).

The sort key is computed in the same statement that returns the cards,
so the position and the displayed assignments_available can never
disagree within one page fetch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import delete
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
from app.modules.identity.events import InMemoryEventCollector
from app.modules.identity.models import User
from app.modules.identity.session_service import SessionService
from app.modules.submissions.models import Submission
from app.modules.tasks.enums import (
    AssignmentAvailability,
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task, TaskCollaborator
from app.modules.tasks.router import (
    get_event_publisher,
    get_rate_limiter,
    get_tasks_redis,
)
from tests.fakes.integrations import FakeRateLimiter

_TASKS_TEST_REDIS_DB = 14
# Anchored to the real now: PyJWT validates `exp` at decode time against
# wall-clock time (the task API suite uses the same anchoring).
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"

_TEACHER_EMAIL = "marketplace-teacher@pku.edu.cn"
_STUDENT_NUMBER = "20250990001"


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_TASKS_TEST_REDIS_DB}"))


@pytest_asyncio.fixture
async def api_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
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
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def event_collector() -> InMemoryEventCollector:
    return InMemoryEventCollector()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    fake_limiter: FakeRateLimiter,
    event_collector: InMemoryEventCollector,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_tasks_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_event_publisher] = lambda: event_collector
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


@pytest_asyncio.fixture(autouse=True)
async def swept_task_catalogue(db_session: AsyncSession) -> None:
    """Empty the task-side tables inside this test's rolled-back
    transaction (the task API suite's sweep — ordering assertions are on
    the global list, so each test starts from a deterministic
    catalogue). Submissions delete first: suites that commit through
    their own connections (claim concurrency, document validation, the
    e2e world) can leave submissions referencing claims, and deleting
    the claims first dies on that FK."""
    await db_session.execute(delete(Submission))
    await db_session.execute(delete(AssignmentClaim))
    await db_session.execute(delete(Assignment))
    await db_session.execute(delete(TaskCollaborator))
    await db_session.execute(delete(Task))
    await db_session.flush()


# --- seeding helpers --------------------------------------------------------------


async def _seed_user(db: AsyncSession, *, username: str, role: Role) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="测试用户",
        role=role.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _session_tokens(db: AsyncSession, api_clock: FrozenClock, user: User) -> dict:
    sessions = SessionService(clock=api_clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=api_clock.now())
    return {"access_token": tokens.access_token}


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _seed_task(owner_id: Any, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner_id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL.value,
        "rarity": TaskRarity.NORMAL.value,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED.value,
        "deadline_mode": DeadlineMode.RELATIVE.value,
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
        "published_at": _T0,
        "submission_schema": {"columns": ["title", "likes"]},
        "submission_schema_version": 1,
    }
    fields.update(overrides)
    return Task(**fields)


def _seed_assignment(
    task_id: Any, keyword: str, availability: AssignmentAvailability
) -> Assignment:
    return Assignment(
        task_id=task_id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=availability.value,
    )


async def _list_titles(
    client: httpx.AsyncClient, tokens: dict, *, limit: int, offset: int
) -> tuple[list[str], dict[str, int]]:
    """One page of the marketplace as (titles in order, available counts
    by title) — the assertions read both from the same response."""
    response = await client.get(
        "/api/v1/tasks",
        params={"limit": limit, "offset": offset},
        headers=_bearer(tokens),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    counts = {card["title"]: card["assignments_available"] for card in body["items"]}
    return [card["title"] for card in body["items"]], counts


# --- group semantics ---------------------------------------------------------------


@pytest.mark.integration
async def test_claimable_lists_before_depleted_regardless_of_publish_time(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """A claimable task published EARLIER outranks every depleted task;
    the depleted group itself keeps published_at DESC; a task with no
    units at all is depleted (nothing on it is claimable)."""
    teacher = await _seed_user(db_session, username=_TEACHER_EMAIL, role=Role.TEACHER)
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    tokens = await _session_tokens(db_session, api_clock, student)

    depleted_newest = _seed_task(teacher.id, title="已领完·最新发布", published_at=_T0)
    claimable_older = _seed_task(
        teacher.id, title="可领·较早发布", published_at=_T0 - timedelta(hours=2)
    )
    depleted_mid = _seed_task(
        teacher.id, title="已领完·居中", published_at=_T0 - timedelta(hours=1)
    )
    no_units = _seed_task(
        teacher.id, title="无名额任务", published_at=_T0 - timedelta(minutes=30)
    )
    db_session.add_all([depleted_newest, claimable_older, depleted_mid, no_units])
    await db_session.flush()
    db_session.add_all(
        [
            _seed_assignment(
                depleted_newest.id, "关键词A", AssignmentAvailability.OCCUPIED
            ),
            _seed_assignment(
                depleted_newest.id, "关键词B", AssignmentAvailability.COMPLETED
            ),
            _seed_assignment(
                claimable_older.id, "关键词C", AssignmentAvailability.AVAILABLE
            ),
            _seed_assignment(
                claimable_older.id, "关键词D", AssignmentAvailability.AVAILABLE
            ),
            _seed_assignment(
                depleted_mid.id, "关键词E", AssignmentAvailability.RETIRED
            ),
        ]
    )
    await db_session.flush()

    titles, counts = await _list_titles(client, tokens, limit=10, offset=0)
    # Depleted group, newest-first: 无名额任务 (-30min) is newer than
    # 已领完·居中 (-1h).
    assert titles == [
        "可领·较早发布",
        "已领完·最新发布",
        "无名额任务",
        "已领完·居中",
    ]
    assert counts == {
        "可领·较早发布": 2,
        "已领完·最新发布": 0,
        "已领完·居中": 0,
        "无名额任务": 0,
    }


# --- pagination boundaries -----------------------------------------------------------


@pytest.mark.integration
async def test_page_boundaries_keep_group_order_across_pages(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """Offset pages concatenate to exactly the full ordering; the
    page-1/page-2 boundary crosses the group edge (claimable tail,
    depleted head) without misordering."""
    teacher = await _seed_user(db_session, username=_TEACHER_EMAIL, role=Role.TEACHER)
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    tokens = await _session_tokens(db_session, api_clock, student)

    # Publish times interleave the two groups deliberately: under plain
    # published_at DESC the depleted tasks would outrank the claimable
    # ones, so this page fails until the group ordering exists.
    catalogue = [
        _seed_task(teacher.id, title="领完一", published_at=_T0),
        _seed_task(teacher.id, title="可领一", published_at=_T0 - timedelta(hours=1)),
        _seed_task(teacher.id, title="领完二", published_at=_T0 - timedelta(hours=2)),
        _seed_task(teacher.id, title="可领二", published_at=_T0 - timedelta(hours=3)),
        _seed_task(teacher.id, title="领完三", published_at=_T0 - timedelta(hours=4)),
    ]
    db_session.add_all(catalogue)
    await db_session.flush()
    for task in catalogue:
        depleted = task.title.startswith("领完")
        db_session.add(
            _seed_assignment(
                task.id,
                f"关键词{task.title}",
                AssignmentAvailability.COMPLETED
                if depleted
                else AssignmentAvailability.AVAILABLE,
            )
        )
    await db_session.flush()

    expected_full = ["可领一", "可领二", "领完一", "领完二", "领完三"]
    collected: list[str] = []
    for offset in (0, 2, 4):
        titles, _ = await _list_titles(client, tokens, limit=2, offset=offset)
        collected.extend(titles)
    assert collected == expected_full

    total = (
        await client.get(
            "/api/v1/tasks", params={"limit": 1, "offset": 0}, headers=_bearer(tokens)
        )
    ).json()["total"]
    assert total == 5


# --- availability flips ---------------------------------------------------------------


@pytest.mark.integration
async def test_claiming_last_unit_flips_task_behind_depleted_and_abandon_restores(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The real claim API is the production writer of the availability
    flip: claiming the only AVAILABLE unit moves the task behind a
    NEWER depleted task (the group edge overrides publish order);
    abandoning returns the unit to AVAILABLE and restores the
    claimable-first position."""
    teacher = await _seed_user(db_session, username=_TEACHER_EMAIL, role=Role.TEACHER)
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    tokens = await _session_tokens(db_session, api_clock, student)

    depleted_newer = _seed_task(teacher.id, title="旧账已领完", published_at=_T0)
    one_left_older = _seed_task(
        teacher.id, title="仅剩一个名额", published_at=_T0 - timedelta(hours=1)
    )
    db_session.add_all([depleted_newer, one_left_older])
    await db_session.flush()
    db_session.add_all(
        [
            _seed_assignment(
                depleted_newer.id, "关键词F", AssignmentAvailability.COMPLETED
            ),
            _seed_assignment(
                one_left_older.id, "关键词G", AssignmentAvailability.AVAILABLE
            ),
        ]
    )
    await db_session.flush()

    titles, _ = await _list_titles(client, tokens, limit=10, offset=0)
    assert titles == ["仅剩一个名额", "旧账已领完"]

    claim = await client.post(
        f"/api/v1/tasks/{one_left_older.id}/claim", headers=_bearer(tokens)
    )
    assert claim.status_code == 201, claim.text
    claim_id = claim.json()["claim_id"]

    titles, counts = await _list_titles(client, tokens, limit=10, offset=0)
    assert titles == ["旧账已领完", "仅剩一个名额"]
    assert counts["仅剩一个名额"] == 0

    abandon = await client.post(
        f"/api/v1/claims/{claim_id}/abandon", headers=_bearer(tokens)
    )
    assert abandon.status_code == 200, abandon.text

    titles, counts = await _list_titles(client, tokens, limit=10, offset=0)
    assert titles == ["仅剩一个名额", "旧账已领完"]
    assert counts["仅剩一个名额"] == 1
