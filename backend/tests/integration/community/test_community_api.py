# backend/tests/integration/community/test_community_api.py
"""The community HTTP API end-to-end over real PostgreSQL + Redis (T9).

Drives the real app (``create_app()`` — the community router mounted under
/api/v1, real envelope handlers, real composition-root wiring) through the
surfaces plan 06 task 9 freezes:

- the API flow (plan step 1): Student creates an ANONYMOUS comment ->
  another Student sees 匿名用户 (and none of the author's identity facts)
  -> votes and reacts -> the owner edits (revision history retained) ->
  the Teacher soft-deletes -> the public list renders the parent as a
  tombstone while the child reply survives, and the Teacher moderation
  list still reviews the deleted thread pseudonymously;
- hot ordering (plan step 2): ``sort=hot`` reorders by the server-side
  time-decayed engagement score — different from ``sort=latest`` — and a
  client-supplied ``hot_score`` is a 422 before any row exists (spec §24
  不得将客户端传入的 hot_score 当事实值);
- rate limiting (plan step 3): the REAL Redis fixed-window limiter (not a
  fake) keyed by the authenticated user id — the 11th comment in the
  10/min window returns the stable 429 RATE_LIMITED envelope and inserts
  no row;
- ratings: a non-completer gets the §29 RATING_NOT_ELIGIBLE envelope,
  completers upsert one row each, and the tasks module's task detail
  reads the REAL TaskRating aggregate through the swapped
  CommunityRatingSummaryPort wiring (the plan 06 task 7/9 carry);
- the moderation boundaries: the report queue shows reporter identity to
  the Teacher (never to Students, and never the anonymous author's own
  nickname on a self-report), the hard hide is Admin-only, and the reveal
  is Admin-only, reason-capped, and audited on every call.

Seams are dependency overrides, not route fakes: the rollback-harness
session, the FrozenClock anchored to the real now (PyJWT validates ``exp``
against wall-clock time), the flushed Redis test database 13 (the REAL
limiter — the 429 behavior under test is the window arithmetic), and the
InMemoryEventCollector for the audit events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.community.models import (
    Comment,
    CommentRevision,
    TaskRating,
)
from app.modules.community.router import (
    get_community_redis,
    get_event_publisher,
)
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import InMemoryEventCollector
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
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

# Anchored to the real now: PyJWT validates `exp` at decode time against
# wall-clock time, so tokens minted by the app must be "just now" (the
# identity/tasks API suites use the same anchoring). The frozen clock also
# pins the REAL limiter's fixed-window index, so one test's requests all
# land in the same window deterministically.
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_COMMUNITY_TEST_REDIS_DB = 13

_TEACHER_EMAIL = "community-teacher@pku.edu.cn"
_ADMIN_EMAIL = "community-admin@pku.edu.cn"


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_COMMUNITY_TEST_REDIS_DB}"))


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
def event_collector() -> InMemoryEventCollector:
    return InMemoryEventCollector()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    event_collector: InMemoryEventCollector,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    # The REAL RedisFixedWindowLimiter stays wired (only its Redis client
    # is pointed at the flushed test database): the 429-under-test is the
    # window arithmetic itself, not a programmed fake.
    app.dependency_overrides[get_community_redis] = lambda: api_redis
    app.dependency_overrides[get_event_publisher] = lambda: event_collector
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


# --- seeding helpers --------------------------------------------------------------


def _user(
    *,
    username: str,
    role: Role,
    nickname: str = "测试同学",
    status: UserStatus = UserStatus.ACTIVE,
    phone_e164: str | None = None,
    email_normalized: str | None = None,
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=phone_e164,
        email_normalized=email_normalized,
        role=role,
        status=status,
    )


async def _tokens(db: AsyncSession, clock: FrozenClock, user: User) -> dict:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, pair = await sessions.issue_session(db, user=user, now=clock.now())
    return {"access_token": pair.access_token}


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _seed(db: AsyncSession, *objects: Any) -> None:
    db.add_all(objects)
    await db.flush()


async def _staff_account(
    db: AsyncSession,
    clock: FrozenClock,
    *,
    username: str,
    role: Role,
) -> tuple[User, dict]:
    """A staff account holding a normal ACTIVE session that ALSO passes
    ``require_staff_management_actor`` (confirmed TOTP credential; the
    secret bytes are opaque to the guard)."""
    user = _user(username=username, role=role)
    db.add(user)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return user, await _tokens(db, clock, user)


def _published_task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
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


def _seeded_comment(
    task: Task,
    user: User,
    *,
    content: str = "这个任务的说明很清楚，做起来很顺利。",
    is_anonymous: bool = False,
    created_at: datetime = _T0,
    parent_id: UUID | None = None,
) -> Comment:
    return Comment(
        task_id=task.id,
        user_id=user.id,
        parent_id=parent_id,
        content=content,
        is_anonymous=is_anonymous,
        created_at=created_at,
    )


async def _seed_completed_claim(
    db: AsyncSession, task: Task, user: User, *, keyword: str
) -> None:
    """One COMPLETED claim — the spec §20 rating-eligibility predicate.
    The keyword is caller-supplied: (task, platform, keyword) is UNIQUE,
    so parallel claims need distinct units."""
    assignment = Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.COMPLETED.value,
    )
    db.add(assignment)
    await db.flush()
    deadline = _T0 + timedelta(days=3)
    db.add(
        AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task.id,
            user_id=user.id,
            status=ClaimStatus.COMPLETED.value,
            deadline_at=deadline,
            grace_deadline_at=deadline + timedelta(minutes=1440),
            reward_policy_snapshot={"version": 1},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status=RewardLockStatus.NONE.value,
        )
    )
    await db.flush()


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


def _comment_ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


async def _comment_rows(db: AsyncSession, task: Task) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(Comment).where(Comment.task_id == task.id)
        )
        or 0
    )


# --- the API flow (plan step 1) -----------------------------------------------------


@pytest.mark.integration
async def test_comment_flow_anonymous_through_moderation_tombstone(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The plan's step-1 chain over the real routes: anonymous create ->
    another student sees 匿名用户 (never the author's identity facts) ->
    vote + reaction -> owner edit with retained history -> Teacher
    soft-delete -> tombstone parent with surviving child, and the
    pseudonymous Teacher moderation list still reviewing the thread."""
    teacher, teacher_tokens = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    author = _user(
        username="20250911001",
        role=Role.STUDENT,
        nickname="阿一",
        phone_e164="+8613800910001",
        email_normalized="flow-author@pku.edu.cn",
    )
    reader = _user(username="20250911002", role=Role.STUDENT, nickname="小二")
    await _seed(db_session, author, reader)
    task = _published_task(teacher)
    await _seed(db_session, task)
    author_tokens = await _tokens(db_session, api_clock, author)
    reader_tokens = await _tokens(db_session, api_clock, reader)

    # 1. The author publishes an ANONYMOUS comment.
    created = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "  匿名提问：截止时间怎么算？  ", "is_anonymous": True},
        headers=_bearer(author_tokens),
    )
    assert created.status_code == 201, created.text
    anonymous = created.json()
    assert anonymous["content"] == "匿名提问：截止时间怎么算？"  # trimmed
    assert anonymous["is_anonymous"] is True
    assert anonymous["author_display"] == "匿名用户"
    assert anonymous["edited"] is False
    assert anonymous["deleted"] is False
    # The privacy contract on the wire: none of the author's identity
    # facts ride the response (spec §21.4/§40).
    for secret in (
        author.username,
        author.phone_e164 or "",
        author.email_normalized or "",
        author.nickname,
        str(author.id),
    ):
        assert secret not in created.text

    # 2. Another student sees the anonymous comment — still 匿名用户.
    listed = await client.get(
        f"/api/v1/tasks/{task.id}/comments", headers=_bearer(reader_tokens)
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "items": [anonymous],
        "total": 1,
        "limit": 20,
        "offset": 0,
    }

    # 3. The reader votes (like) and reacts.
    voted = await client.post(
        f"/api/v1/comments/{anonymous['id']}/vote",
        json={"value": 1},
        headers=_bearer(reader_tokens),
    )
    assert voted.status_code == 200, voted.text
    assert voted.json() == {"current_value": 1, "likes": 1, "dislikes": 0}

    reacted = await client.post(
        f"/api/v1/comments/{anonymous['id']}/reactions",
        json={"emoji": "👍"},
        headers=_bearer(reader_tokens),
    )
    assert reacted.status_code == 200, reacted.text
    assert reacted.json() == {"emoji": "👍", "added": True, "counts": {"👍": 1}}

    # 4. The reader replies (the child that must survive moderation).
    reply = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "按领取时间相对计算。", "parent_id": anonymous["id"]},
        headers=_bearer(reader_tokens),
    )
    assert reply.status_code == 201, reply.text
    child = reply.json()
    assert child["parent_id"] == anonymous["id"]
    assert child["author_display"] == "小二"

    # The reader cannot edit someone else's comment (spec §21.2 MUST).
    denied = await client.patch(
        f"/api/v1/comments/{anonymous['id']}",
        json={"content": "越权修改"},
        headers=_bearer(reader_tokens),
    )
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    # 5. The owner edits; the previous version lands in the history.
    edited = await client.patch(
        f"/api/v1/comments/{anonymous['id']}",
        json={"content": "匿名提问（已补充）：截止时间按哪个时区算？"},
        headers=_bearer(author_tokens),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["content"] == "匿名提问（已补充）：截止时间按哪个时区算？"
    assert edited.json()["edited"] is True
    revision = await db_session.scalar(
        select(CommentRevision).where(
            CommentRevision.comment_id == UUID(anonymous["id"])
        )
    )
    assert revision is not None
    assert revision.content == "匿名提问：截止时间怎么算？"

    # 6. The task's Teacher soft-deletes the parent (reason mandatory).
    # httpx's delete() takes no json body — the reason rides a raw request.
    removed = await client.request(
        "DELETE",
        f"/api/v1/teacher/comments/{anonymous['id']}",
        json={"reason": "与任务无关的讨论"},
        headers=_bearer(teacher_tokens),
    )
    assert removed.status_code == 204, removed.text

    # 7. The public list: tombstone parent, surviving child (spec §21.3).
    after = await client.get(
        f"/api/v1/tasks/{task.id}/comments", headers=_bearer(reader_tokens)
    )
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["total"] == 2
    by_id = {item["id"]: item for item in body["items"]}
    tombstone = by_id[anonymous["id"]]
    assert tombstone["deleted"] is True
    assert tombstone["content"] is None
    assert tombstone["author_display"] == "该评论已删除"
    survivor = by_id[child["id"]]
    assert survivor["deleted"] is False
    assert survivor["content"] == "按领取时间相对计算。"

    # 8. The Teacher moderation list reviews the same thread
    # pseudonymously: 匿名用户 + the moderation key on the anonymous
    # record, no key on the named one, the removal flagged as history.
    moderation = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/comments/moderation",
        headers=_bearer(teacher_tokens),
    )
    assert moderation.status_code == 200, moderation.text
    queue = {item["id"]: item for item in moderation.json()["items"]}
    assert queue[anonymous["id"]]["deleted"] is True
    assert queue[anonymous["id"]]["author_display"] == "匿名用户"
    assert len(queue[anonymous["id"]]["moderation_key"]) == 16
    assert queue[child["id"]]["moderation_key"] is None
    assert queue[child["id"]]["author_display"] == "小二"

    # Students never reach the moderation surface.
    student_denied = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/comments/moderation",
        headers=_bearer(reader_tokens),
    )
    assert student_denied.status_code == 403
    assert _envelope(student_denied)["code"] == "PERMISSION_DENIED"


# --- hot ordering (plan step 2) -----------------------------------------------------


@pytest.mark.integration
async def test_hot_sort_is_server_computed_and_reorders_the_thread(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """``sort=hot`` orders by the replaceable server-side score — fresh
    mid-engagement beats old higher-engagement beats fresh silent, a
    DIFFERENT order than ``sort=latest`` — and a client-supplied
    ``hot_score`` is a 422 that inserts nothing (spec §24)."""
    teacher, _ = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    students = [
        _user(username=f"2025092100{index}", role=Role.STUDENT, nickname=f"同学{index}")
        for index in range(1, 4)
    ]
    await _seed(db_session, *students)
    task = _published_task(teacher)
    await _seed(db_session, task)
    student_tokens = [
        await _tokens(db_session, api_clock, student) for student in students
    ]

    # Engagement layout (72h half-life): the mid comment (2 likes + 1
    # reaction, age 48h -> ~1.89) outranks the old comment (3 likes, age
    # 192h -> ~0.47), which outranks the silent fresh one (0.0). Latest
    # order is exactly reversed in popularity terms.
    old = _seeded_comment(
        task, students[0], created_at=_T0 - timedelta(days=8), content="旧的讨论"
    )
    mid = _seeded_comment(
        task, students[1], created_at=_T0 - timedelta(days=2), content="近期的热评"
    )
    await _seed(db_session, old, mid)

    for tokens in student_tokens:
        assert (
            await client.post(
                f"/api/v1/comments/{old.id}/vote",
                json={"value": 1},
                headers=_bearer(tokens),
            )
        ).status_code == 200
    for tokens in student_tokens[:2]:
        assert (
            await client.post(
                f"/api/v1/comments/{mid.id}/vote",
                json={"value": 1},
                headers=_bearer(tokens),
            )
        ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/comments/{mid.id}/reactions",
            json={"emoji": "🔥"},
            headers=_bearer(student_tokens[2]),
        )
    ).status_code == 200

    fresh = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "刚刚发出的安静评论"},
        headers=_bearer(student_tokens[0]),
    )
    assert fresh.status_code == 201, fresh.text

    hot = await client.get(
        f"/api/v1/tasks/{task.id}/comments",
        params={"sort": "hot"},
        headers=_bearer(student_tokens[1]),
    )
    assert hot.status_code == 200, hot.text
    assert _comment_ids(hot.json()) == [str(mid.id), str(old.id), fresh.json()["id"]]

    latest = await client.get(
        f"/api/v1/tasks/{task.id}/comments",
        params={"sort": "latest"},
        headers=_bearer(student_tokens[1]),
    )
    assert latest.status_code == 200, latest.text
    assert _comment_ids(latest.json()) == [
        fresh.json()["id"],
        str(mid.id),
        str(old.id),
    ]

    # The score is server-owned: a client-supplied hot_score never parses.
    before_rows = await _comment_rows(db_session, task)
    rejected = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "带热度作弊", "hot_score": 999999},
        headers=_bearer(student_tokens[1]),
    )
    assert rejected.status_code == 422
    assert _envelope(rejected)["code"] == "VALIDATION_ERROR"
    assert await _comment_rows(db_session, task) == before_rows


# --- rate limiting (plan step 3) ----------------------------------------------------


@pytest.mark.integration
async def test_comment_create_beyond_window_is_stable_429_without_rows(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The REAL Redis window: the configured comments:create cap is 10/min
    keyed by the authenticated user id — the 11th post returns the stable
    429 RATE_LIMITED envelope and inserts no row, and so does the 12th."""
    teacher, _ = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    student = _user(username="20250931001", role=Role.STUDENT, nickname="刷屏同学")
    await _seed(db_session, student)
    task = _published_task(teacher)
    await _seed(db_session, task)
    tokens = await _tokens(db_session, api_clock, student)

    statuses = [
        (
            await client.post(
                f"/api/v1/tasks/{task.id}/comments",
                json={"content": f"第 {index} 条评论"},
                headers=_bearer(tokens),
            )
        ).status_code
        for index in range(1, 11)
    ]
    assert statuses == [201] * 10

    throttled = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "超出窗口的第 11 条"},
        headers=_bearer(tokens),
    )
    assert throttled.status_code == 429
    error = _envelope(throttled)
    assert error["code"] == "RATE_LIMITED"

    # Stability: the next request fails the same way, and no extra row
    # landed beyond the ten the window admitted.
    again = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "再试一次"},
        headers=_bearer(tokens),
    )
    assert again.status_code == 429
    assert _envelope(again)["code"] == "RATE_LIMITED"
    assert await _comment_rows(db_session, task) == 10


# --- ratings and the real aggregate wiring (the plan 06 T7/T9 carry) -----------------


@pytest.mark.integration
async def test_rating_eligibility_upsert_and_real_aggregate_on_task_detail(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """Only completers rate (§20): the denial is the §29
    RATING_NOT_ELIGIBLE envelope; completers upsert one row each; and the
    tasks module's detail surface reads the REAL TaskRating aggregate —
    the swapped CommunityRatingSummaryPort wiring, end to end."""
    teacher, teacher_tokens = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    rater_a = _user(username="20250941001", role=Role.STUDENT, nickname="评分甲")
    rater_b = _user(username="20250941002", role=Role.STUDENT, nickname="评分乙")
    browser = _user(username="20250941003", role=Role.STUDENT, nickname="只看不评")
    await _seed(db_session, rater_a, rater_b, browser)
    task = _published_task(teacher)
    await _seed(db_session, task)
    await _seed_completed_claim(db_session, task, rater_a, keyword="评分甲的单元")
    await _seed_completed_claim(db_session, task, rater_b, keyword="评分乙的单元")
    rater_a_tokens = await _tokens(db_session, api_clock, rater_a)
    rater_b_tokens = await _tokens(db_session, api_clock, rater_b)
    browser_tokens = await _tokens(db_session, api_clock, browser)

    # A non-completer (no claim at all) is rate-ineligible.
    denied = await client.put(
        f"/api/v1/tasks/{task.id}/rating",
        json={"rating": 5},
        headers=_bearer(browser_tokens),
    )
    assert denied.status_code == 403, denied.text
    assert _envelope(denied)["code"] == "RATING_NOT_ELIGIBLE"

    # Staff roles never reach the rating surface (the student guard).
    staff_denied = await client.put(
        f"/api/v1/tasks/{task.id}/rating",
        json={"rating": 5},
        headers=_bearer(teacher_tokens),
    )
    assert staff_denied.status_code == 403
    assert _envelope(staff_denied)["code"] == "PERMISSION_DENIED"

    # Completers upsert: re-rating the same task updates the one row.
    first = await client.put(
        f"/api/v1/tasks/{task.id}/rating",
        json={"rating": 2},
        headers=_bearer(rater_a_tokens),
    )
    assert first.status_code == 200, first.text
    assert first.json()["rating"] == 2
    rerated = await client.put(
        f"/api/v1/tasks/{task.id}/rating",
        json={"rating": 5},
        headers=_bearer(rater_a_tokens),
    )
    assert rerated.status_code == 200, rerated.text
    assert rerated.json()["rating"] == 5
    second = await client.put(
        f"/api/v1/tasks/{task.id}/rating",
        json={"rating": 3},
        headers=_bearer(rater_b_tokens),
    )
    assert second.status_code == 200, second.text

    rows = (
        (
            await db_session.execute(
                select(TaskRating.rating).where(TaskRating.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(rows) == [3, 5]

    # The tasks detail reads the REAL aggregate through the swapped port.
    detail = await client.get(
        f"/api/v1/tasks/{task.id}", headers=_bearer(browser_tokens)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["rating"] == {"average": 4.0, "count": 2}


# --- reports and the moderation queue (spec §23) ------------------------------------


@pytest.mark.integration
async def test_report_queue_shows_reporter_to_teacher_not_students(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """Filing never removes the comment; duplicates are idempotent; the
    queue carries reporter identity to the Teacher (the §23 moderation
    side of the wall) with the anonymous self-report folded to None; and
    Students never reach the queue."""
    teacher, teacher_tokens = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    author = _user(username="20250951001", role=Role.STUDENT, nickname="被举报人")
    reporter = _user(username="20250951002", role=Role.STUDENT, nickname="举报人")
    await _seed(db_session, author, reporter)
    task = _published_task(teacher)
    await _seed(db_session, task)
    author_tokens = await _tokens(db_session, api_clock, author)
    reporter_tokens = await _tokens(db_session, api_clock, reporter)

    reported = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "可疑的广告内容"},
        headers=_bearer(author_tokens),
    )
    assert reported.status_code == 201, reported.text
    comment_id = reported.json()["id"]

    filed = await client.post(
        f"/api/v1/comments/{comment_id}/reports",
        json={"category": "SPAM", "note": "广告刷屏"},
        headers=_bearer(reporter_tokens),
    )
    assert filed.status_code == 201, filed.text
    report = filed.json()
    assert report["category"] == "SPAM"
    assert report["note"] == "广告刷屏"
    assert report["status"] == "OPEN"
    assert report["comment_id"] == comment_id

    # The duplicate (comment, reporter, category) is the idempotent echo.
    duplicate = await client.post(
        f"/api/v1/comments/{comment_id}/reports",
        json={"category": "SPAM", "note": "再报一次"},
        headers=_bearer(reporter_tokens),
    )
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["id"] == report["id"]

    # The comment survives the filing (spec §23 举报不自动删除评论).
    listed = await client.get(
        f"/api/v1/tasks/{task.id}/comments", headers=_bearer(reporter_tokens)
    )
    assert listed.json()["total"] == 1

    # The Teacher queue: reporter identity present, comment in the
    # Teacher-safe shape.
    queue = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/reports", headers=_bearer(teacher_tokens)
    )
    assert queue.status_code == 200, queue.text
    items = queue.json()["items"]
    assert queue.json()["total"] == 1
    assert items[0]["reporter_user_id"] == str(reporter.id)
    assert items[0]["reporter_nickname"] == "举报人"
    assert items[0]["comment"]["author_display"] == "被举报人"

    # The self-report fold: the anonymous author reporting their own
    # comment carries no reporter identity (task 6 review F2).
    anonymous = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "匿名自评", "is_anonymous": True},
        headers=_bearer(author_tokens),
    )
    assert anonymous.status_code == 201, anonymous.text
    self_report = await client.post(
        f"/api/v1/comments/{anonymous.json()['id']}/reports",
        json={"category": "OTHER"},
        headers=_bearer(author_tokens),
    )
    assert self_report.status_code == 201, self_report.text
    refreshed = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/reports", headers=_bearer(teacher_tokens)
    )
    by_comment = {item["comment"]["id"]: item for item in refreshed.json()["items"]}
    fold = by_comment[anonymous.json()["id"]]
    assert fold["reporter_user_id"] is None
    assert fold["reporter_nickname"] is None
    assert fold["comment"]["author_display"] == "匿名用户"

    # Students never reach the queue.
    denied = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/reports",
        headers=_bearer(reporter_tokens),
    )
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"


# --- admin reveal and hard hide (spec §21.3/§21.4) ----------------------------------


@pytest.mark.integration
async def test_admin_reveal_is_audited_and_reason_capped(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    event_collector: InMemoryEventCollector,
) -> None:
    """The reveal is Admin-only (the owning Teacher is refused), returns
    the real identity, audits every call through the event port, and
    enforces the reason: capped at 1000 characters, blank rejected."""
    teacher, teacher_tokens = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    _, admin_tokens = await _staff_account(
        db_session, api_clock, username=_ADMIN_EMAIL, role=Role.ADMIN
    )
    author = _user(username="20250961001", role=Role.STUDENT, nickname="匿名作者")
    await _seed(db_session, author)
    task = _published_task(teacher)
    await _seed(db_session, task)
    author_tokens = await _tokens(db_session, api_clock, author)

    anonymous = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "这条是匿名的", "is_anonymous": True},
        headers=_bearer(author_tokens),
    )
    assert anonymous.status_code == 201, anonymous.text
    comment_id = anonymous.json()["id"]

    teacher_denied = await client.post(
        f"/api/v1/admin/comments/{comment_id}/reveal-identity",
        json={"reason": "教师想看看是谁"},
        headers=_bearer(teacher_tokens),
    )
    assert teacher_denied.status_code == 403
    assert _envelope(teacher_denied)["code"] == "PERMISSION_DENIED"

    revealed = await client.post(
        f"/api/v1/admin/comments/{comment_id}/reveal-identity",
        json={"reason": "接到涉诈举报，需要核实发帖人"},
        headers=_bearer(admin_tokens),
    )
    assert revealed.status_code == 200, revealed.text
    assert revealed.json() == {
        "user_id": str(author.id),
        "nickname": "匿名作者",
        "username": "20250961001",
    }

    events = event_collector.of_type("COMMENT_IDENTITY_REVEALED")
    assert len(events) == 1
    assert events[0].payload["reason"] == "接到涉诈举报，需要核实发帖人"
    assert events[0].payload["revealed_user_id"] == str(author.id)

    oversized = await client.post(
        f"/api/v1/admin/comments/{comment_id}/reveal-identity",
        json={"reason": "长" * 1001},
        headers=_bearer(admin_tokens),
    )
    assert oversized.status_code == 422
    assert _envelope(oversized)["code"] == "VALIDATION_ERROR"

    blank = await client.post(
        f"/api/v1/admin/comments/{comment_id}/reveal-identity",
        json={"reason": "   "},
        headers=_bearer(admin_tokens),
    )
    assert blank.status_code == 400
    assert _envelope(blank)["code"] == "VALIDATION_ERROR"


@pytest.mark.integration
async def test_hard_hide_is_admin_only_and_removes_the_subtree(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    event_collector: InMemoryEventCollector,
) -> None:
    """The hard hide is the Admin privacy/legal tool (spec §21.3 彻底隐
    藏): the owning Teacher is refused at the admin guard, the Admin's
    hide disappears the whole subtree from the public list (no tombstone
    for the parent), the rows survive flagged for moderation review, and
    the audit event carries the verbatim reason."""
    teacher, teacher_tokens = await _staff_account(
        db_session, api_clock, username=_TEACHER_EMAIL, role=Role.TEACHER
    )
    _, admin_tokens = await _staff_account(
        db_session, api_clock, username=_ADMIN_EMAIL, role=Role.ADMIN
    )
    author = _user(username="20250971001", role=Role.STUDENT, nickname="发帖人")
    replier = _user(username="20250971002", role=Role.STUDENT, nickname="跟帖人")
    await _seed(db_session, author, replier)
    task = _published_task(teacher)
    await _seed(db_session, task)
    author_tokens = await _tokens(db_session, api_clock, author)
    replier_tokens = await _tokens(db_session, api_clock, replier)

    root = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "涉隐私的根评论"},
        headers=_bearer(author_tokens),
    )
    assert root.status_code == 201, root.text
    child = await client.post(
        f"/api/v1/tasks/{task.id}/comments",
        json={"content": "跟帖", "parent_id": root.json()["id"]},
        headers=_bearer(replier_tokens),
    )
    assert child.status_code == 201, child.text

    teacher_denied = await client.post(
        f"/api/v1/teacher/comments/{root.json()['id']}/hard-hide",
        json={"reason": "教师试图彻底隐藏"},
        headers=_bearer(teacher_tokens),
    )
    assert teacher_denied.status_code == 403
    assert _envelope(teacher_denied)["code"] == "PERMISSION_DENIED"

    hidden = await client.post(
        f"/api/v1/teacher/comments/{root.json()['id']}/hard-hide",
        json={"reason": "涉个人信息泄露，依法彻底隐藏"},
        headers=_bearer(admin_tokens),
    )
    assert hidden.status_code == 204, hidden.text

    # Public list: the subtree renders NOTHING (not even a tombstone).
    public = await client.get(
        f"/api/v1/tasks/{task.id}/comments", headers=_bearer(replier_tokens)
    )
    assert public.status_code == 200, public.text
    assert public.json()["total"] == 0

    # The rows survive, flagged, for the moderation surface.
    moderation = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/comments/moderation",
        headers=_bearer(teacher_tokens),
    )
    assert moderation.status_code == 200, moderation.text
    flagged = {item["id"]: item for item in moderation.json()["items"]}
    assert flagged[root.json()["id"]]["hard_hidden"] is True
    assert flagged[child.json()["id"]]["hard_hidden"] is True
    assert flagged[root.json()["id"]]["deleted"] is True

    events = event_collector.of_type("COMMENT_HARD_HIDDEN")
    assert len(events) == 1
    assert events[0].payload["reason"] == "涉个人信息泄露，依法彻底隐藏"
    assert set(events[0].payload["hidden_comment_ids"]) == {
        root.json()["id"],
        child.json()["id"],
    }
