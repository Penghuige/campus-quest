# backend/tests/workers/test_expire_claims.py
"""Worker-level tests for the claim-expiry Celery shells (plan 07 T6; spec
§8.2, §11.4; backend-engineering §12, §15).

Pinned first, against the app factory and the task objects only:

- Amendment 1 (plan-04 final review): the worker acknowledges LATE and
  rejects on worker loss (``task_acks_late`` + ``task_reject_on_worker_lost``),
  so a worker dying mid-task redelivers instead of silently dropping the
  job. That at-least-once contract is why every task here is idempotent.
- The job module is importable by a real worker startup (JOB_MODULES).
- Signature shapes: tasks receive ids/params only (plus the correlation
  id); ``collect_due_claim_ids`` takes the session and the instant, with
  the limit keyword-only.

The end-to-end scenario then runs the REAL eager chain against the real
PostgreSQL test database — scan discovers, per-ID jobs expire with real
commits, the LoggingEventPublisher wiring inside ``build_expire_service``
stays exercised — substituting only the session maker with a NullPool
factory (one ``asyncio.run`` loop per task body must never share pooled
connections across closed loops; the same substitution pattern as
tests/workers/test_notification_delivery.py). Real commits require real
cleanup: every seeded row is deleted in FK order (claims -> assignments
-> tasks -> users).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from celery import Celery
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
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
from app.workers.celery_app import JOB_MODULES, create_celery_app
from app.workers.jobs.expire_claims import (
    collect_due_claim_ids,
    expire_claim,
    expire_claims_scan,
)

pytestmark = pytest.mark.integration

# --- test database guard (mirrors tests/integration/db_guard.py) --------------------

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15432/campusquest_test"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    from sqlalchemy.engine import make_url

    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing expiry-worker tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "DATABASE_URL": _database_url(),
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_BUCKET": "campusquest-test",
        "S3_ACCESS_KEY": "campusquest",
        "S3_SECRET_KEY": "campusquest-dev",
        "BUSINESS_TIMEZONE": "Asia/Shanghai",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def db_engine() -> Iterator[AsyncEngine]:
    """NullPool engine: connections never outlive their asyncio.run loop.

    The sync Celery tasks drive their sessions from one-shot loops (one
    ``asyncio.run`` per task body); NullPool opens a fresh connection per
    session so no connection is reused across a closed loop.
    """
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def celery_app(monkeypatch: pytest.MonkeyPatch) -> Celery:
    """TEST-ONLY eager app (same pattern as test_celery_wiring.py):
    `.delay()` runs inline — the scan's per-ID enqueues included — and no
    broker socket is ever opened."""
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    app.conf.task_always_eager = True
    return app


# --- row builders ------------------------------------------------------------------


def _user(
    *, username: str, role: Role = Role.STUDENT, status: UserStatus = UserStatus.ACTIVE
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=status,
    )


def _task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL,
        "rarity": TaskRarity.NORMAL,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": 4320,
        "submission_schema": {"columns": [{"name": "note", "type": "string"}]},
        "submission_schema_version": 2,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _assignment(task: Task, *, keyword: str) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=AssignmentAvailability.AVAILABLE,
    )


def _claim(
    assignment: Assignment,
    user: User,
    *,
    status: ClaimStatus,
    claimed_at: datetime,
    grace_deadline_at: datetime,
) -> AssignmentClaim:
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=status,
        claimed_at=claimed_at,
        deadline_at=grace_deadline_at - timedelta(minutes=1440),
        grace_deadline_at=grace_deadline_at,
        reward_policy_snapshot={"version": 1, "ladder_fractions": ["1", "0.8"]},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
    )


async def _persist(session: AsyncSession, *objects: Any) -> None:
    session.add_all(objects)
    await session.flush()


async def _cleanup(
    maker: async_sessionmaker[AsyncSession],
    *,
    task_ids: list[UUID],
    user_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order (claims -> assignments ->
    tasks -> users); real commits leave no outer rollback to trust."""
    async with maker() as session:
        if task_ids:
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.task_id.in_(task_ids))
            )
            await session.execute(
                delete(Assignment).where(Assignment.task_id.in_(task_ids))
            )
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


# --- amendment 1 pins ----------------------------------------------------------------


def test_worker_acks_late_and_rejects_on_worker_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan-04 amendment 1: a dead worker must not silently swallow a job.
    Late acknowledgements + reject-on-worker-loss make delivery
    at-least-once, which the idempotent task bodies absorb (EXPIRED
    replays answer ALREADY_TERMINAL; the scan re-discovers nothing)."""
    _set_required_env(monkeypatch)
    app = create_celery_app(Settings())
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True


def test_expire_claims_module_registered_in_job_modules() -> None:
    # A real worker process imports no test module: the job module must be
    # named in JOB_MODULES or its tasks stay invisible to worker startup.
    assert "app.workers.jobs.expire_claims" in JOB_MODULES


def test_job_signatures_carry_ids_and_params_only() -> None:
    # §12: a job loads IDs and parameters, constructs dependencies, then
    # calls a service. bind=True consumes `self` (job-id logging only), so
    # producers pass ids and the correlation id — never a dependency
    # object — through the task signature.
    task_parameters = inspect.signature(expire_claim).parameters
    assert list(task_parameters) == ["claim_id", "request_id"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in task_parameters.values()
    )
    assert list(inspect.signature(expire_claims_scan).parameters) == ["request_id"]

    # The candidate query's inputs are the session and the instant, with
    # the batch size keyword-only.
    collector = inspect.signature(collect_due_claim_ids).parameters
    assert list(collector) == ["session", "now", "limit"]
    assert collector["limit"].kind is inspect.Parameter.KEYWORD_ONLY


# --- eager end-to-end on the real database -------------------------------------------


def test_eager_scan_expires_due_claims_end_to_end(
    celery_app: Celery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real chain, inline: the scan samples SystemClock, discovers
    only the due claim, enqueues one per-ID job per id (eager: they run
    inside the scan call), and each per-ID job opens its own session,
    calls the service, and returns a JSON payload. The not-due claim is
    untouched; a second scan discovers nothing; a redelivered per-ID job
    answers ALREADY_TERMINAL without rewriting anything."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # The jobs import get_async_session_maker lazily from app.db.session
    # at call time; substituting the module attribute routes the REAL
    # task code (build_expire_service included — SystemClock +
    # LoggingEventPublisher + the default inspector) onto this NullPool
    # engine.
    monkeypatch.setattr("app.db.session.get_async_session_maker", lambda: maker)

    run = uuid4().hex[:8]
    real_now = datetime.now(UTC)

    seeded_rows = tuple[User, User, list[Task], list[Assignment], list[AssignmentClaim]]

    async def seed() -> seeded_rows:
        async with maker() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001")
            await _persist(session, teacher, student)
            tasks = [_task(teacher, title=f"任务{i}") for i in range(2)]
            await _persist(session, *tasks)
            due_assignment = _assignment(tasks[0], keyword="考研数学")
            later_assignment = _assignment(tasks[1], keyword="考研英语")
            await _persist(session, due_assignment, later_assignment)
            due_claim = _claim(
                due_assignment,
                student,
                status=ClaimStatus.CLAIMED,
                claimed_at=real_now - timedelta(days=5),
                grace_deadline_at=real_now - timedelta(hours=1),
            )
            later_claim = _claim(
                later_assignment,
                student,
                status=ClaimStatus.CLAIMED,
                claimed_at=real_now - timedelta(hours=2),
                grace_deadline_at=real_now + timedelta(days=1),
            )
            due_assignment.availability_status = AssignmentAvailability.OCCUPIED
            later_assignment.availability_status = AssignmentAvailability.OCCUPIED
            await _persist(session, due_claim, later_claim)
            await session.commit()
            return (
                teacher,
                student,
                tasks,
                [due_assignment, later_assignment],
                [due_claim, later_claim],
            )

    teacher, student, tasks, assignments, claims = asyncio.run(seed())
    task_ids = [task.id for task in tasks]
    user_ids = [teacher.id, student.id]
    try:
        scan = expire_claims_scan.delay("req-scan-1").get(timeout=30)
        assert scan["request_id"] == "req-scan-1"
        assert scan["discovered"] == 1
        assert scan["claim_ids"] == [str(claims[0].id)]
        # JSON wire contract: the summary round-trips through json.
        assert json.loads(json.dumps(scan)) == scan

        async def inspect() -> tuple[
            AssignmentClaim, AssignmentClaim, Assignment, Assignment
        ]:
            async with maker() as session:
                expired = await session.get(AssignmentClaim, claims[0].id)
                untouched = await session.get(AssignmentClaim, claims[1].id)
                due_row = await session.get(Assignment, assignments[0].id)
                later_row = await session.get(Assignment, assignments[1].id)
                assert expired is not None and untouched is not None
                assert due_row is not None and later_row is not None
                return expired, untouched, due_row, later_row

        expired, untouched, due_row, later_row = asyncio.run(inspect())
        assert ClaimStatus(expired.status) is ClaimStatus.EXPIRED
        assert expired.terminal_at is not None
        assert expired.terminal_at >= real_now  # SystemClock-sampled instant
        assert ClaimStatus(untouched.status) is ClaimStatus.CLAIMED
        assert untouched.terminal_at is None
        assert (
            AssignmentAvailability(due_row.availability_status)
            is AssignmentAvailability.AVAILABLE
        )
        assert (
            AssignmentAvailability(later_row.availability_status)
            is AssignmentAvailability.OCCUPIED
        )

        # A redelivered per-ID job (at-least-once) is an idempotent no-op
        # with a JSON-serializable payload.
        replay = expire_claim.delay(str(claims[0].id), "req-replay-1").get(timeout=30)
        assert replay["claim_id"] == str(claims[0].id)
        assert replay["outcome"] == "ALREADY_TERMINAL"
        assert replay["status"] == ClaimStatus.EXPIRED.value
        assert replay["terminal_at"] == expired.terminal_at.isoformat()
        assert json.loads(json.dumps(replay)) == replay

        # A second scan finds nothing: the EXPIRED row left the
        # actionable candidate set.
        again = expire_claims_scan.delay("req-scan-2").get(timeout=30)
        assert again["discovered"] == 0
        assert again["claim_ids"] == []
    finally:
        asyncio.run(_cleanup(maker, task_ids=task_ids, user_ids=user_ids))
    asyncio.run(engine.dispose())
