# backend/tests/integration/tasks/test_task_api.py
"""The task/claim HTTP API end-to-end over real PostgreSQL + Redis (T9).

Drives the real app (``create_app()`` — real routes, real envelope handlers,
real composition-root wiring incl. the tasks router mounted under /api/v1)
through the surfaces the task brief freezes:

- permissions: Student cannot create Task (403); a Teacher without
  collaborator standing cannot edit another Teacher's Task (403 — and a
  VIEW_TASK grant opens reads, never edits); a claim request cannot carry
  ``assignment_id`` (the schema forbids it, 422, and no claim happens);
- the happy chain: teacher creates + publishes + imports previews/confirms,
  the student lists cards (availability COUNT only — never an assignment
  list), claims (response shows the OWN assignment's platform/keyword only),
  and abandons;
- pagination (offset, documented V1 choice), lifecycle verbs,
  statistics, and the §29 envelope on business errors;
- endpoint rate limiting on claim/abandon normalized to the user id.

Seams are dependency overrides, not route fakes: the rollback-harness
session, the flushed Redis test database 14 (import preview tokens), the
FrozenClock anchored to the real now (PyJWT validates ``exp`` against
wall-clock time), the FakeRateLimiter, and the InMemoryEventCollector for
the CLAIM_ABANDONED audit event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import delete, func, select
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
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
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
# wall-clock time, so tokens minted by the app must be "just now" (the
# identity API suite uses the same anchoring).
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"

_TEACHER_EMAIL = "task-teacher@pku.edu.cn"
_OTHER_TEACHER_EMAIL = "other-teacher@pku.edu.cn"
_STUDENT_NUMBER = "20250010001"

_CSV_HEADER = "platform,keyword\n"

_CARD_FIELDS = {
    "id",
    "title",
    "rarity",
    "base_reward_points",
    "deadline_mode",
    "fixed_deadline_at",
    "duration_minutes",
    "assignments_available",
    "rating",
}
_CLAIM_FIELDS = {
    "claim_id",
    "task_id",
    "status",
    "platform",
    "keyword",
    "claimed_at",
    "deadline_at",
    "grace_deadline_at",
    "base_reward_points_snapshot",
}


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
    """Empty the task-side tables inside this test's rolled-back transaction.

    The claim concurrency/quota suites intentionally commit through their
    own connections, so PUBLISHED tasks persist in the shared test
    database between runs. The public list/count surfaces this module
    asserts on are global, so each test starts from a deterministic
    catalogue: the DELETEs ride the harness transaction and roll back at
    teardown, restoring the leftovers for the suites that own them.
    """
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


async def _seed_management_teacher(
    db: AsyncSession, api_clock: FrozenClock, *, username: str
) -> tuple[User, dict]:
    """A TEACHER able to pass ``require_staff_management_actor``.

    The guard needs role + ACTIVE + a CONFIRMED TOTP credential; the
    secret bytes are opaque to it (only ``confirmed_at`` is read), so the
    test seeds a stand-in instead of running the whole onboarding flow.
    """
    teacher = await _seed_user(db, username=username, role=Role.TEACHER)
    db.add(
        TotpCredential(
            user_id=teacher.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=api_clock.now(),
        )
    )
    await db.flush()
    return teacher, await _session_tokens(db, api_clock, teacher)


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
        # A PUBLISHED task always carries these (publish validation); a
        # claim on a row missing them answers TASK_NOT_CLAIMABLE.
        "submission_schema": {"columns": ["title", "likes"]},
        "submission_schema_version": 1,
    }
    fields.update(overrides)
    return Task(**fields)


def _seed_assignment(task_id: Any, keyword: str) -> Assignment:
    return Assignment(
        task_id=task_id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.AVAILABLE.value,
    )


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


def _create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "抖音学习打卡视频数据采集",
        "description": "采集指定关键词下的视频标题与互动数据。",
        "base_reward_points": 100,
        "deadline_mode": "RELATIVE",
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "submission_schema": {"columns": ["title", "likes"]},
        "submission_schema_version": 1,
    }
    payload.update(overrides)
    return payload


# --- permission surface (brief step 1) ---------------------------------------------


@pytest.mark.integration
async def test_student_cannot_create_task(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    tokens = await _session_tokens(db_session, api_clock, student)

    denied = await client.post(
        "/api/v1/teacher/tasks", json=_create_payload(), headers=_bearer(tokens)
    )
    assert denied.status_code == 403
    error = _envelope(denied)
    assert error["code"] == "PERMISSION_DENIED"

    tasks = await db_session.scalar(select(func.count()).select_from(Task))
    assert tasks == 0


@pytest.mark.integration
async def test_teacher_cannot_edit_another_teachers_task(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    owner, owner_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    other, other_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_OTHER_TEACHER_EMAIL
    )
    task = _seed_task(owner.id)
    db_session.add(task)
    await db_session.flush()

    # No relationship at all: the edit is refused.
    denied = await client.patch(
        f"/api/v1/teacher/tasks/{task.id}",
        json={"title": "被篡改的标题"},
        headers=_bearer(other_tokens),
    )
    assert denied.status_code == 403, denied.text
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"

    # A VIEW_TASK grant is a READ capability: statistics open up...
    granted = await client.put(
        f"/api/v1/teacher/tasks/{task.id}/collaborators/{other.id}",
        json={"permissions": ["VIEW_TASK"]},
        headers=_bearer(owner_tokens),
    )
    assert granted.status_code == 200, granted.text
    assert granted.json() == {
        "task_id": str(task.id),
        "teacher_id": str(other.id),
        "permissions": ["VIEW_TASK"],
    }

    statistics = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/statistics",
        headers=_bearer(other_tokens),
    )
    assert statistics.status_code == 200, statistics.text

    # ...but the V1 edit rule stays owner-or-admin even for collaborators.
    still_denied = await client.patch(
        f"/api/v1/teacher/tasks/{task.id}",
        json={"title": "协作者也不能改"},
        headers=_bearer(other_tokens),
    )
    assert still_denied.status_code == 403
    assert _envelope(still_denied)["code"] == "PERMISSION_DENIED"

    # Removal revokes the read again (and covers the remove route).
    removed = await client.delete(
        f"/api/v1/teacher/tasks/{task.id}/collaborators/{other.id}",
        headers=_bearer(owner_tokens),
    )
    assert removed.status_code == 204
    revoked = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/statistics",
        headers=_bearer(other_tokens),
    )
    assert revoked.status_code == 403
    assert _envelope(revoked)["code"] == "PERMISSION_DENIED"


@pytest.mark.integration
async def test_claim_request_cannot_choose_an_assignment(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    teacher, _ = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()
    assignment = _seed_assignment(task.id, "考研经验")
    db_session.add(assignment)
    await db_session.flush()
    tokens = await _session_tokens(db_session, api_clock, student)

    # The claim schema has no assignment_id field and forbids extras: the
    # attempt is a 422 VALIDATION_ERROR before any service call.
    rejected = await client.post(
        f"/api/v1/tasks/{task.id}/claim",
        json={"assignment_id": str(assignment.id)},
        headers=_bearer(tokens),
    )
    assert rejected.status_code == 422
    error = _envelope(rejected)
    assert error["code"] == "VALIDATION_ERROR"

    # Proof the rejection preceded the service: nothing was claimed and
    # the unit is still AVAILABLE.
    claims = await db_session.scalar(select(func.count()).select_from(AssignmentClaim))
    assert claims == 0
    await db_session.refresh(assignment)
    assert assignment.availability_status == AssignmentAvailability.AVAILABLE.value


# --- the frozen happy chain (brief step 2) -----------------------------------------


@pytest.mark.integration
async def test_teacher_creates_publishes_imports_student_claims_and_abandons(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    event_collector: InMemoryEventCollector,
) -> None:
    teacher, teacher_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    student_tokens = await _session_tokens(db_session, api_clock, student)

    # -- teacher: create ------------------------------------------------------
    created = await client.post(
        "/api/v1/teacher/tasks",
        json=_create_payload(),
        headers=_bearer(teacher_tokens),
    )
    assert created.status_code == 201, created.text
    task_body = created.json()
    assert task_body["status"] == "DRAFT"
    assert task_body["title"] == "抖音学习打卡视频数据采集"
    assert task_body["deadline_mode"] == "RELATIVE"
    assert task_body["duration_minutes"] == 4320
    assert task_body["allowed_file_types"] == ["CSV"]
    assert task_body["submission_schema"] == {"columns": ["title", "likes"]}
    task_id = task_body["id"]

    # -- teacher: publish -----------------------------------------------------
    published = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/publish", headers=_bearer(teacher_tokens)
    )
    assert published.status_code == 200, published.text
    transition = published.json()
    assert transition == {
        "task_id": task_id,
        "status": "PUBLISHED",
        "claimable": True,
        "published_at": transition["published_at"],
        "closed_at": None,
    }

    # -- teacher: import preview + confirm -------------------------------------
    keywords = ["关键词甲", "关键词乙", "关键词丙"]
    csv_rows = "".join(f"xiaohongshu,{keyword}\n" for keyword in keywords)
    csv_bytes = (_CSV_HEADER + csv_rows).encode()
    previewed = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/assignments/import/preview",
        content=csv_bytes,
        headers={**_bearer(teacher_tokens), "Content-Type": "text/csv"},
    )
    assert previewed.status_code == 200, previewed.text
    preview = previewed.json()
    assert preview["total_rows"] == 3
    assert preview["valid_count"] == 3
    assert preview["error_count"] == 0
    assert preview["errors"] == []
    assert preview["preview_token"]
    assert preview["expires_at"]

    confirmed = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/assignments/import/confirm",
        json={"preview_token": preview["preview_token"]},
        headers=_bearer(teacher_tokens),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"task_id": task_id, "inserted": 3}

    # -- student: the task list shows the card, never the assignment list ------
    listed = await client.get("/api/v1/tasks", headers=_bearer(student_tokens))
    assert listed.status_code == 200, listed.text
    listing = listed.json()
    assert listing["total"] == 1
    assert listing["limit"] == 20
    assert listing["offset"] == 0
    assert len(listing["items"]) == 1
    card = listing["items"][0]
    assert set(card) == _CARD_FIELDS
    assert card["id"] == task_id
    assert card["rarity"] == "NORMAL"
    assert card["base_reward_points"] == 100
    assert card["deadline_mode"] == "RELATIVE"
    assert card["duration_minutes"] == 4320
    assert card["fixed_deadline_at"] is None
    assert card["assignments_available"] == 3
    assert card["rating"] is None  # Plan 06 fills the slot
    # The §42 rule, verbatim: 领取后不暴露 Assignment 列表 — an unclaimed
    # task exposes no platform/keyword material at all.
    assert "platform" not in listed.text
    assert "keyword" not in listed.text
    assert "xiaohongshu" not in listed.text

    # -- student: detail before claiming carries no claim and no assignment ----
    detail = await client.get(
        f"/api/v1/tasks/{task_id}", headers=_bearer(student_tokens)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["my_claim"] is None
    assert "xiaohongshu" not in detail.text

    # -- student: claim — the response names ONLY the assigned unit -------------
    claimed = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_bearer(student_tokens)
    )
    assert claimed.status_code == 201, claimed.text
    claim = claimed.json()
    assert set(claim) == _CLAIM_FIELDS
    assert claim["task_id"] == task_id
    assert claim["status"] == "CLAIMED"
    assert claim["platform"] == "xiaohongshu"
    assert claim["keyword"] in keywords
    assert claim["base_reward_points_snapshot"] == 100
    assert claim["claimed_at"]
    assert claim["deadline_at"]
    assert claim["grace_deadline_at"]
    others = [keyword for keyword in keywords if keyword != claim["keyword"]]
    for keyword in others:
        assert keyword not in claimed.text

    # The list card now counts one fewer AVAILABLE unit.
    relisted = await client.get("/api/v1/tasks", headers=_bearer(student_tokens))
    assert relisted.json()["items"][0]["assignments_available"] == 2

    # The detail names the claim — with its own platform/keyword.
    reseen = await client.get(
        f"/api/v1/tasks/{task_id}", headers=_bearer(student_tokens)
    )
    assert reseen.status_code == 200
    my_claim = reseen.json()["my_claim"]
    assert set(my_claim) == _CLAIM_FIELDS
    assert my_claim["claim_id"] == claim["claim_id"]
    assert my_claim["status"] == "CLAIMED"
    assert my_claim["keyword"] == claim["keyword"]

    # -- student: /me/claims shows the own assignment material ------------------
    mine = await client.get("/api/v1/me/claims", headers=_bearer(student_tokens))
    assert mine.status_code == 200, mine.text
    my_claims = mine.json()
    assert my_claims["total"] == 1
    assert len(my_claims["items"]) == 1
    item = my_claims["items"][0]
    assert set(item) == _CLAIM_FIELDS | {"task_title"}
    assert item["claim_id"] == claim["claim_id"]
    assert item["task_id"] == task_id
    assert item["task_title"] == "抖音学习打卡视频数据采集"
    assert item["platform"] == "xiaohongshu"
    assert item["keyword"] == claim["keyword"]

    # -- student: abandon releases the unit back to AVAILABLE -------------------
    abandoned = await client.post(
        f"/api/v1/claims/{claim['claim_id']}/abandon",
        headers=_bearer(student_tokens),
    )
    assert abandoned.status_code == 200, abandoned.text
    abandoned_body = abandoned.json()
    assert set(abandoned_body) == _CLAIM_FIELDS
    assert abandoned_body["status"] == "ABANDONED"

    history = await client.get("/api/v1/me/claims", headers=_bearer(student_tokens))
    assert history.json()["items"][0]["status"] == "ABANDONED"
    assert history.json()["items"][0]["keyword"] == claim["keyword"]

    # The released unit is claimable again by someone else.
    final_list = await client.get("/api/v1/tasks", headers=_bearer(student_tokens))
    assert final_list.json()["items"][0]["assignments_available"] == 3

    # The abandon wrote its behavior-history audit event (spec §8.5).
    events = event_collector.of_type("CLAIM_ABANDONED")
    assert len(events) == 1
    assert events[0].aggregate_id == UUID(claim["claim_id"])
    assert events[0].payload["user_id"] == str(student.id)


# --- pagination (offset; documented V1 choice) --------------------------------------


@pytest.mark.integration
async def test_task_list_pagination_and_draft_invisibility(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    teacher, _ = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    for index in range(3):
        db_session.add(
            _seed_task(
                teacher.id,
                title=f"已发布任务 {index}",
                published_at=_T0 - timedelta(hours=index),
            )
        )
    # DRAFT is invisible on the student surface (spec §6.2).
    draft = _seed_task(
        teacher.id, title="草稿任务", status=TaskStatus.DRAFT.value, published_at=None
    )
    db_session.add(draft)
    await db_session.flush()
    tokens = await _session_tokens(db_session, api_clock, student)

    first_page = await client.get(
        "/api/v1/tasks",
        params={"limit": 2, "offset": 0},
        headers=_bearer(tokens),
    )
    assert first_page.status_code == 200, first_page.text
    body = first_page.json()
    assert body["total"] == 3
    assert [card["title"] for card in body["items"]] == [
        "已发布任务 0",
        "已发布任务 1",
    ]

    second_page = await client.get(
        "/api/v1/tasks",
        params={"limit": 2, "offset": 2},
        headers=_bearer(tokens),
    )
    assert second_page.status_code == 200
    assert [card["title"] for card in second_page.json()["items"]] == ["已发布任务 2"]

    # A DRAFT task id is not readable through the public detail route.
    hidden = await client.get(f"/api/v1/tasks/{draft.id}", headers=_bearer(tokens))
    assert hidden.status_code == 404
    assert _envelope(hidden)["code"] == "NOT_FOUND"


# --- lifecycle verbs + claim conflict family through the API ------------------------


@pytest.mark.integration
async def test_lifecycle_verbs_and_claim_conflicts(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    teacher, teacher_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    student_tokens = await _session_tokens(db_session, api_clock, student)

    created = await client.post(
        "/api/v1/teacher/tasks",
        json=_create_payload(),
        headers=_bearer(teacher_tokens),
    )
    task_id = created.json()["id"]
    await client.post(
        f"/api/v1/teacher/tasks/{task_id}/publish", headers=_bearer(teacher_tokens)
    )

    paused = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/pause", headers=_bearer(teacher_tokens)
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "PAUSED"
    assert paused.json()["claimable"] is False

    refused = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_bearer(student_tokens)
    )
    assert refused.status_code == 409
    error = _envelope(refused)
    assert error["code"] == "TASK_NOT_CLAIMABLE"

    resumed = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/resume", headers=_bearer(teacher_tokens)
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "PUBLISHED"
    assert resumed.json()["claimable"] is True

    # No units imported yet: claiming is NO_ASSIGNMENT_AVAILABLE (409).
    empty = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_bearer(student_tokens)
    )
    assert empty.status_code == 409
    assert _envelope(empty)["code"] == "NO_ASSIGNMENT_AVAILABLE"

    csv_bytes = (_CSV_HEADER + "xiaohongshu,关键词甲\n").encode()
    preview = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/assignments/import/preview",
        content=csv_bytes,
        headers={**_bearer(teacher_tokens), "Content-Type": "text/csv"},
    )
    assert preview.status_code == 200, preview.text
    await client.post(
        f"/api/v1/teacher/tasks/{task_id}/assignments/import/confirm",
        json={"preview_token": preview.json()["preview_token"]},
        headers=_bearer(teacher_tokens),
    )

    claimed = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_bearer(student_tokens)
    )
    assert claimed.status_code == 201, claimed.text

    # The same student claiming again is the 409 same-task conflict.
    duplicate = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_bearer(student_tokens)
    )
    assert duplicate.status_code == 409
    duplicate_error = _envelope(duplicate)
    assert duplicate_error["code"] == "TASK_ACTIVE_CLAIM_EXISTS"
    assert duplicate_error["details"]["task_id"] == task_id

    closed = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/close", headers=_bearer(teacher_tokens)
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "CLOSED"
    assert closed.json()["closed_at"]

    archived = await client.post(
        f"/api/v1/teacher/tasks/{task_id}/archive", headers=_bearer(teacher_tokens)
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"

    # Unknown aggregates stay NOT_FOUND (the T2 carry: system 404 is the
    # right semantic; no registry change).
    unknown = uuid4()
    missing_claim = await client.post(
        f"/api/v1/tasks/{unknown}/claim", headers=_bearer(student_tokens)
    )
    assert missing_claim.status_code == 404
    assert _envelope(missing_claim)["code"] == "NOT_FOUND"

    missing_detail = await client.get(
        f"/api/v1/tasks/{unknown}", headers=_bearer(student_tokens)
    )
    assert missing_detail.status_code == 404
    assert _envelope(missing_detail)["code"] == "NOT_FOUND"

    missing_abandon = await client.post(
        f"/api/v1/claims/{uuid4()}/abandon", headers=_bearer(student_tokens)
    )
    assert missing_abandon.status_code == 404
    assert _envelope(missing_abandon)["code"] == "NOT_FOUND"


@pytest.mark.integration
async def test_unauthenticated_requests_are_rejected(client: httpx.AsyncClient) -> None:
    rejected = await client.get("/api/v1/tasks")
    assert rejected.status_code == 401
    assert _envelope(rejected)["code"] == "AUTHENTICATION_REQUIRED"

    teacher_surface = await client.get(f"/api/v1/teacher/tasks/{uuid4()}/statistics")
    assert teacher_surface.status_code == 401


# --- import preview error surface ----------------------------------------------------


@pytest.mark.integration
async def test_import_preview_reports_row_errors(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    teacher, teacher_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()

    csv_bytes = (_CSV_HEADER + "xiaohongshu,考研经验\nwechat,无效平台\n").encode()
    previewed = await client.post(
        f"/api/v1/teacher/tasks/{task.id}/assignments/import/preview",
        content=csv_bytes,
        headers={**_bearer(teacher_tokens), "Content-Type": "text/csv"},
    )
    assert previewed.status_code == 200, previewed.text
    preview = previewed.json()
    assert preview["total_rows"] == 2
    assert preview["valid_count"] == 1
    assert preview["error_count"] == 1
    assert preview["errors"][0]["code"] == "UNSUPPORTED_PLATFORM"
    assert preview["errors"][0]["row_number"] == 2
    assert preview["preview_token"]

    confirmed = await client.post(
        f"/api/v1/teacher/tasks/{task.id}/assignments/import/confirm",
        json={"preview_token": preview["preview_token"]},
        headers=_bearer(teacher_tokens),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["inserted"] == 1


# --- statistics surface ---------------------------------------------------------------


@pytest.mark.integration
async def test_teacher_statistics_counts(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    teacher, teacher_tokens = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()

    availabilities = [
        AssignmentAvailability.AVAILABLE,
        AssignmentAvailability.AVAILABLE,
        AssignmentAvailability.OCCUPIED,
        AssignmentAvailability.COMPLETED,
        AssignmentAvailability.RETIRED,
    ]
    assignments = [
        _seed_assignment(task.id, f"关键词{index}")
        for index in range(len(availabilities))
    ]
    for assignment, availability in zip(assignments, availabilities, strict=True):
        assignment.availability_status = availability.value
        db_session.add(assignment)
    await db_session.flush()

    db_session.add(
        AssignmentClaim(
            assignment_id=assignments[2].id,
            task_id=task.id,
            user_id=student.id,
            status=ClaimStatus.CLAIMED.value,
            claimed_at=_T0,
            deadline_at=_T0 + timedelta(days=3),
            grace_deadline_at=_T0 + timedelta(days=4),
            reward_policy_snapshot={},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status="NONE",
        )
    )
    await db_session.flush()

    statistics = await client.get(
        f"/api/v1/teacher/tasks/{task.id}/statistics",
        headers=_bearer(teacher_tokens),
    )
    assert statistics.status_code == 200, statistics.text
    body = statistics.json()
    assert set(body) == {
        "task_id",
        "assignments_available",
        "assignments_occupied",
        "assignments_completed",
        "assignments_retired",
        "active_claims",
        "completion_rate",
        "rating",
        "submission_counts",
    }
    assert body["task_id"] == str(task.id)
    assert body["assignments_available"] == 2
    assert body["assignments_occupied"] == 1
    assert body["assignments_completed"] == 1
    assert body["assignments_retired"] == 1
    assert body["active_claims"] == 1
    assert body["completion_rate"] == 0.25  # 1 / (2+1+1)
    assert body["rating"] is None  # NullRatingSummaryPort until Plan 06
    assert body["submission_counts"] == {}  # Plan 04 seam


# --- rate limiting on the student-heavy endpoints (spec §33.1) ------------------------


@pytest.mark.integration
async def test_claim_and_abandon_are_rate_limited_per_user(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
    fake_limiter: FakeRateLimiter,
) -> None:
    teacher, _ = await _seed_management_teacher(
        db_session, api_clock, username=_TEACHER_EMAIL
    )
    student = await _seed_user(db_session, username=_STUDENT_NUMBER, role=Role.STUDENT)
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()
    db_session.add(_seed_assignment(task.id, "考研经验"))
    await db_session.flush()
    tokens = await _session_tokens(db_session, api_clock, student)

    claimed = await client.post(
        f"/api/v1/tasks/{task.id}/claim", headers=_bearer(tokens)
    )
    assert claimed.status_code == 201, claimed.text
    claim_checks = fake_limiter.checks_for("tasks:claim")
    assert claim_checks[-1].identifier == str(student.id)

    abandoned = await client.post(
        f"/api/v1/claims/{claimed.json()['claim_id']}/abandon",
        headers=_bearer(tokens),
    )
    assert abandoned.status_code == 200, abandoned.text
    abandon_checks = fake_limiter.checks_for("claims:abandon")
    assert abandon_checks[-1].identifier == str(student.id)

    # An exhausted window renders the 429 RATE_LIMITED envelope.
    fake_limiter.fail_on("tasks:claim")
    throttled = await client.post(
        f"/api/v1/tasks/{task.id}/claim", headers=_bearer(tokens)
    )
    assert throttled.status_code == 429
    assert _envelope(throttled)["code"] == "RATE_LIMITED"
