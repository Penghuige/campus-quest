# backend/tests/integration/tasks/test_abandon_settings_composition.py
"""The abandon-limit composition test (PR #5 final review fix B): the
Admin setting is CONSUMED by the data plane, through the real app.

Drives ``create_app()`` — the tasks and system routers mounted under
/api/v1 with their REAL composition roots — over the rollback-harness
session. The provider dependency under test
(``get_abandon_service``'s store-backed ``SystemDailyAbandonLimit``) is
NOT overridden (G18): only the harness allowances are (session,
FrozenClock anchored to the real now for JWT ``exp``, the FakeRateLimiter
— the anti-hammering window is not what this suite pins — and the
InMemoryEventCollector for the CLAIM_ABANDONED audit stream).

The owner's three scenarios (spec §12.4/§8.5; G7 — the row is the fact,
``Settings.daily_abandon_limit`` the seed):

- no ABANDON_DAILY_LIMIT row -> the deployment seed (2) rules: the 3rd
  abandon of the BUSINESS_TIMEZONE natural day is refused, then the
  audited row takes over the moment one exists;
- Admin sets ABANDON_DAILY_LIMIT=0 -> the very next abandon is refused
  (0 = abandoning DISABLED — even the day's first abandon is over a
  cap of 0), the claim and its assignment untouched;
- the limit set back to N (>= 1) -> the same claim abandons fine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
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
from app.modules.system.models import SystemSetting
from app.modules.system.service import ABANDON_DAILY_LIMIT
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
from app.modules.tasks.router import get_event_publisher, get_rate_limiter
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

# PyJWT validates `exp` at decode time against wall-clock time, so the
# frozen clock is anchored to the real now (the API-suite convention).
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_ABANDON_PATH = "/api/v1/claims/{claim_id}/abandon"
_SETTING_PATH = "/api/v1/admin/settings/abandon-daily-limit"


# --- fixtures -----------------------------------------------------------------------


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
    api_clock: FrozenClock,
    fake_limiter: FakeRateLimiter,
    event_collector: InMemoryEventCollector,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
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
async def no_abandon_limit_row(db_session: AsyncSession) -> None:
    """Start from the no-row state inside this test's rolled-back
    transaction: the DELETE rides the harness transaction, so a row some
    other suite durably committed is invisible here and restored at
    teardown (the swept_task_catalogue pattern)."""
    await db_session.execute(
        delete(SystemSetting).where(SystemSetting.key == ABANDON_DAILY_LIMIT)
    )


# --- seeding helpers ----------------------------------------------------------------


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


async def _tokens(db: AsyncSession, clock: FrozenClock, user: User) -> dict:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, pair = await sessions.issue_session(db, user=user, now=clock.now())
    return {"access_token": pair.access_token}


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _admin_headers(
    db: AsyncSession, clock: FrozenClock, *, username: str
) -> dict[str, str]:
    """An ADMIN account able to pass ``require_admin_actor`` (role +
    ACTIVE + a confirmed TOTP credential row — the stand-in secret is
    opaque to the guard)."""
    admin = _user(username=username, role=Role.ADMIN)
    db.add(admin)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=admin.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return _bearer(await _tokens(db, clock, admin))


async def _seed_claimed(
    db: AsyncSession, student: User, *, task: Task, keyword: str
) -> AssignmentClaim:
    """One CLAIMED claim occupying its own assignment — the state a
    student abandons from. The keyword is caller-supplied: (task,
    platform, keyword) is UNIQUE, so parallel units need distinct ones."""
    assignment = Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.OCCUPIED.value,
    )
    db.add(assignment)
    await db.flush()
    deadline = _T0 + timedelta(days=3)
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=student.id,
        status=ClaimStatus.CLAIMED.value,
        claimed_at=_T0,
        deadline_at=deadline,
        grace_deadline_at=deadline + timedelta(minutes=1440),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE.value,
    )
    db.add(claim)
    await db.flush()
    return claim


def _published_task(owner: User, *, title: str) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title=title,
        description="采集指定关键词下的笔记正文与互动数据。",
        task_type=TaskType.DATA_CRAWL.value,
        rarity=TaskRarity.NORMAL.value,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED.value,
        deadline_mode=DeadlineMode.RELATIVE.value,
        duration_minutes=4320,
        allowed_file_types=["CSV"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
        published_at=_T0,
        submission_schema={"columns": ["title", "likes"]},
        submission_schema_version=1,
    )


async def _set_limit(
    client: httpx.AsyncClient, admin_headers: dict[str, str], value: int
) -> None:
    written = await client.put(
        _SETTING_PATH,
        json={"value": value, "reason": "终审修复 B 组合验证"},
        headers=admin_headers,
    )
    assert written.status_code == 200, written.text
    body = written.json()
    assert body["key"] == ABANDON_DAILY_LIMIT
    assert body["value"] == str(value)


async def _abandon(
    client: httpx.AsyncClient, student_headers: dict[str, str], claim: AssignmentClaim
) -> httpx.Response:
    return await client.post(
        _ABANDON_PATH.format(claim_id=claim.id), headers=student_headers
    )


# --- the owner's three scenarios ----------------------------------------------------


@pytest.mark.integration
async def test_zero_limit_disables_abandon_and_restoring_reenables(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """ABANDON_DAILY_LIMIT=0 refuses even the day's first abandon (0 =
    disabled) and writes nothing; set back to 1 the SAME claim abandons
    (Claim -> ABANDONED, Assignment -> AVAILABLE)."""
    teacher = _user(username="abandon-comp-teacher", role=Role.TEACHER)
    student = _user(username="2025abandon01", role=Role.STUDENT)
    db_session.add_all((teacher, student))
    await db_session.flush()
    task = _published_task(teacher, title="放弃限额组合验证任务")
    db_session.add(task)
    await db_session.flush()
    claim = await _seed_claimed(db_session, student, task=task, keyword="考研数学0")
    student_headers = _bearer(await _tokens(db_session, api_clock, student))
    admin_headers = await _admin_headers(
        db_session, api_clock, username="abandon-comp-admin"
    )

    # 0 disables abandoning: the first abandon of the day is already
    # over a cap of 0 (details carry the effective limit).
    await _set_limit(client, admin_headers, 0)
    disabled = await _abandon(client, student_headers, claim)
    assert disabled.status_code == 409, disabled.text
    error = disabled.json()["error"]
    assert error["code"] == "ABANDON_LIMIT_REACHED"
    assert error["details"]["limit"] == 0

    # The refusal wrote nothing: the claim is still CLAIMED and its
    # assignment still OCCUPIED.
    await db_session.refresh(claim)
    assert ClaimStatus(claim.status) is ClaimStatus.CLAIMED
    assert claim.terminal_at is None
    assignment = await db_session.get(Assignment, claim.assignment_id)
    assert assignment is not None
    assert AssignmentAvailability(assignment.availability_status) is (
        AssignmentAvailability.OCCUPIED
    )

    # Set back to N (>= 1): the same claim now abandons — the §8.5
    # terminal transition and the §8.2 release land together.
    await _set_limit(client, admin_headers, 1)
    abandoned = await _abandon(client, student_headers, claim)
    assert abandoned.status_code == 200, abandoned.text
    await db_session.refresh(claim)
    assert ClaimStatus(claim.status) is ClaimStatus.ABANDONED
    assert claim.terminal_at == _T0
    await db_session.refresh(assignment)
    assert AssignmentAvailability(assignment.availability_status) is (
        AssignmentAvailability.AVAILABLE
    )


@pytest.mark.integration
async def test_no_limit_row_uses_deployment_seed_until_a_row_exists(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """No row -> the deployment seed (spec §8.5 default of 2) rules: the
    3rd abandon of the natural day is refused; once the audited row
    says 3, that same abandon succeeds (the row is the fact, G7)."""
    teacher = _user(username="abandon-seed-teacher", role=Role.TEACHER)
    student = _user(username="2025abandon02", role=Role.STUDENT)
    db_session.add_all((teacher, student))
    await db_session.flush()
    # One task per claim: uq_assignment_claims_active_user_task holds a
    # single ACTIVE claim per (user, task), so three same-day abandons
    # need three tasks (the test_abandon.py seeding shape).
    claims = []
    for i in range(3):
        unit = _published_task(teacher, title=f"放弃种子路径验证任务{i}")
        db_session.add(unit)
        await db_session.flush()
        claims.append(
            await _seed_claimed(db_session, student, task=unit, keyword=f"考研数学{i}")
        )
    student_headers = _bearer(await _tokens(db_session, api_clock, student))
    admin_headers = await _admin_headers(
        db_session, api_clock, username="abandon-seed-admin"
    )

    # The seed path: abandons 1 and 2 land, the 3rd is over the
    # Settings.daily_abandon_limit default of 2.
    assert (await _abandon(client, student_headers, claims[0])).status_code == 200
    assert (await _abandon(client, student_headers, claims[1])).status_code == 200
    capped = await _abandon(client, student_headers, claims[2])
    assert capped.status_code == 409, capped.text
    error = capped.json()["error"]
    assert error["code"] == "ABANDON_LIMIT_REACHED"
    assert error["details"]["limit"] == 2  # the deployment seed, not a row

    # The audited row takes over the moment it exists.
    await _set_limit(client, admin_headers, 3)
    assert (await _abandon(client, student_headers, claims[2])).status_code == 200
    for claim in claims:
        await db_session.refresh(claim)
        assert ClaimStatus(claim.status) is ClaimStatus.ABANDONED
