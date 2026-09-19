# backend/tests/integration/tasks/test_assignment_import.py
"""Assignment import preview->confirm against real PostgreSQL and Redis
(spec §7.1; plan 03 T4).

Pinned behavior:

- The full pipeline: preview parses and pre-checks against the live
  database, confirm lands exactly the previewed (canonicalized) rows in
  one transaction, and the token is single-use.
- The preview->confirm race is closed by the database: two independent
  sessions confirming two previews containing the same new pair —
  exactly one insert succeeds, the loser gets the typed duplicate
  conflict (409), never a 500. Seed rows for the race are committed on
  a dedicated connection (the rollback harness would hide them from the
  racing sessions), and the race test cleans up after itself.
- Preview DUPLICATE_IN_DB detection reflects the real table; confirm
  imports only the still-valid rows.
- Authorization: owner, Admin, and a MANAGE_ASSIGNMENTS collaborator may
  preview and import; a VIEW_TASK-only collaborator, an unrelated
  Teacher, and Students are denied on both operations.

Redis: previews live in a dedicated test database (index 13, flushed per
test), following the rate-limiter/OTP integration-suite pattern.
"""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.tasks import importer
from app.modules.tasks.importer import (
    AssignmentImportResult,
    AssignmentImportService,
    DuplicateAssignmentsError,
    ImportErrorCode,
    InvalidPreviewTokenError,
)
from app.modules.tasks.models import Assignment, Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_IMPORT_TEST_REDIS_DB = 13
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"Import integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_IMPORT_TEST_REDIS_DB}"))


@pytest_asyncio.fixture
async def import_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_NOW)


def _service(redis: aioredis.Redis, clock: FrozenClock) -> AssignmentImportService:
    return AssignmentImportService(
        redis=redis,
        clock=clock,
        max_file_bytes=64 * 1024,
        max_rows=100,
        keyword_max_length=16,
        preview_ttl_seconds=900,
    )


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试用户",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": "DATA_CRAWL",
        "rarity": "NORMAL",
        "base_reward_points": 100,
        "status": "PUBLISHED",
        "deadline_mode": "RELATIVE",
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _csv(*rows: tuple[str, str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("platform", "keyword"))
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


async def _seed_task(db_session: AsyncSession, owner: User) -> Task:
    # Flush the owner first: User.id is a server default, so Task needs it
    # populated before construction (same order as the collaborator suite).
    db_session.add(owner)
    await db_session.flush()
    task = _task(owner)
    db_session.add(task)
    await db_session.flush()
    return task


@pytest.mark.integration
async def test_preview_then_confirm_lands_rows(
    db_session: AsyncSession, import_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    """Full happy path: canonicalized, trimmed rows land AVAILABLE in one
    commit; the token is consumed."""
    owner = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    task = await _seed_task(db_session, owner)
    service = _service(import_redis, clock)
    owner_actor = Actor(user_id=owner.id, role=Role.TEACHER)

    preview = await service.preview_assignments(
        db_session,
        owner_actor,
        task.id,
        _csv(("XiaoHongShu", "  考研 经验  "), ("zhihu", "留学")),
    )
    assert preview.valid_count == 2
    assert preview.errors == ()
    assert preview.preview_token is not None

    result = await service.confirm_assignments(
        db_session, owner_actor, task.id, preview.preview_token
    )
    assert isinstance(result, AssignmentImportResult)
    assert result.inserted == 2

    rows = (
        await db_session.scalars(
            select(Assignment).where(Assignment.task_id == task.id)
        )
    ).all()
    assert sorted((row.platform, row.keyword) for row in rows) == [
        ("xiaohongshu", "考研 经验"),
        ("zhihu", "留学"),
    ]
    assert all(row.availability_status == "AVAILABLE" for row in rows)


@pytest.mark.integration
async def test_preview_flags_db_duplicates_and_confirms_only_valid(
    db_session: AsyncSession, import_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    """A pair already in the table is flagged DUPLICATE_IN_DB with its row
    number; confirm imports only the still-valid rows."""
    owner = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    task = await _seed_task(db_session, owner)
    db_session.add(
        Assignment(
            task_id=task.id,
            platform="douyin",
            keyword="Python 入门",
            availability_status="AVAILABLE",
        )
    )
    await db_session.flush()
    service = _service(import_redis, clock)
    owner_actor = Actor(user_id=owner.id, role=Role.TEACHER)

    preview = await service.preview_assignments(
        db_session,
        owner_actor,
        task.id,
        _csv(("DOUYIN", "Python 入门"), ("xiaohongshu", "考研")),
    )
    assert [(error.row_number, error.code) for error in preview.errors] == [
        (1, ImportErrorCode.DUPLICATE_IN_DB)
    ]
    assert [row.keyword for row in preview.valid] == ["考研"]

    result = await service.confirm_assignments(
        db_session, owner_actor, task.id, preview.preview_token
    )
    assert result.inserted == 1
    keywords = {
        row.keyword
        for row in await db_session.scalars(
            select(Assignment).where(Assignment.task_id == task.id)
        )
    }
    assert keywords == {"Python 入门", "考研"}


@pytest.mark.integration
async def test_preview_token_single_use(
    db_session: AsyncSession, import_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    """The second confirm with the same token is the typed NOT_FOUND and
    inserts nothing more."""
    owner = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    task = await _seed_task(db_session, owner)
    service = _service(import_redis, clock)
    owner_actor = Actor(user_id=owner.id, role=Role.TEACHER)

    preview = await service.preview_assignments(
        db_session, owner_actor, task.id, _csv(("zhihu", "留学"))
    )
    assert preview.preview_token is not None
    await service.confirm_assignments(
        db_session, owner_actor, task.id, preview.preview_token
    )

    with pytest.raises(InvalidPreviewTokenError) as consumed:
        await service.confirm_assignments(
            db_session, owner_actor, task.id, preview.preview_token
        )
    assert consumed.value.code == ErrorCode.NOT_FOUND
    assert consumed.value.status_code == 404

    count = len(
        (
            await db_session.scalars(
                select(Assignment).where(Assignment.task_id == task.id)
            )
        ).all()
    )
    assert count == 1


@pytest.mark.integration
async def test_confirm_unknown_token_not_found(
    db_session: AsyncSession, import_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    owner = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    task = await _seed_task(db_session, owner)
    service = _service(import_redis, clock)

    with pytest.raises(InvalidPreviewTokenError):
        await service.confirm_assignments(
            db_session, Actor(user_id=owner.id, role=Role.TEACHER), task.id, "x"
        )
    assert (
        await db_session.scalars(
            select(Assignment).where(Assignment.task_id == task.id)
        )
    ).all() == []


@pytest.mark.integration
async def test_import_permissions(
    db_session: AsyncSession, import_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    """Owner, Admin, and MANAGE_ASSIGNMENTS collaborators may preview and
    confirm; a VIEW_TASK-only collaborator, an unrelated Teacher, and a
    Student are denied on both, and denial leaves no rows behind."""
    owner = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    admin = _user(username=f"admin{uuid4().hex[:8]}", role=Role.ADMIN)
    manager = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    viewer = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    outsider = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
    student = _user(username=f"2025{uuid4().hex[:8]}", role=Role.STUDENT)
    task = await _seed_task(db_session, owner)
    db_session.add_all([admin, manager, viewer, outsider, student])
    await db_session.flush()  # populate the server-default user ids first
    db_session.add_all(
        [
            TaskCollaborator(
                task_id=task.id,
                teacher_id=manager.id,
                permissions=["MANAGE_ASSIGNMENTS"],
            ),
            TaskCollaborator(
                task_id=task.id, teacher_id=viewer.id, permissions=["VIEW_TASK"]
            ),
        ]
    )
    await db_session.flush()
    service = _service(import_redis, clock)
    data = _csv(("zhihu", "留学"))
    owner_actor = Actor(user_id=owner.id, role=Role.TEACHER)

    allowed = [
        owner_actor,
        Actor(user_id=admin.id, role=Role.ADMIN),
        Actor(user_id=manager.id, role=Role.TEACHER),
    ]
    for index, actor in enumerate(allowed):
        preview = await service.preview_assignments(
            db_session, actor, task.id, _csv(("zhihu", f"关键词{index}"))
        )
        assert preview.preview_token is not None
        await service.confirm_assignments(
            db_session, actor, task.id, preview.preview_token
        )

    denied = [
        Actor(user_id=viewer.id, role=Role.TEACHER),
        Actor(user_id=outsider.id, role=Role.TEACHER),
        Actor(user_id=student.id, role=Role.STUDENT),
    ]
    for actor in denied:
        with pytest.raises(BusinessError) as preview_denied:
            await service.preview_assignments(db_session, actor, task.id, data)
        assert preview_denied.value.code == ErrorCode.PERMISSION_DENIED
        assert preview_denied.value.status_code == 403

        with pytest.raises(BusinessError) as confirm_denied:
            await service.confirm_assignments(db_session, actor, task.id, "any")
        assert confirm_denied.value.code == ErrorCode.PERMISSION_DENIED

    # Three allowed imports landed; every denied attempt wrote nothing.
    assert (
        len(
            (
                await db_session.scalars(
                    select(Assignment).where(Assignment.task_id == task.id)
                )
            ).all()
        )
        == 3
    )


@pytest.mark.integration
async def test_preview_unknown_task_not_found(
    db_session: AsyncSession, import_redis: aioredis.Redis, clock: FrozenClock
) -> None:
    service = _service(import_redis, clock)
    with pytest.raises(TaskNotFoundError):
        await service.preview_assignments(
            db_session, Actor(user_id=uuid4(), role=Role.ADMIN), uuid4(), b""
        )


# --- the concurrent-confirm race (real sessions, real Redis) ----------------------


async def _seed_committed_owner_task(
    engine: AsyncEngine,
) -> tuple[User, Task]:
    """Owner + task committed on a dedicated connection so the racing
    sessions on other connections can see them."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        owner = _user(username=f"teacher{uuid4().hex[:8]}", role=Role.TEACHER)
        session.add(owner)
        await session.flush()
        task = _task(owner)
        session.add(task)
        await session.commit()
        return owner, task


async def _cleanup_task(engine: AsyncEngine, task: Task, *users: User) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(delete(Assignment).where(Assignment.task_id == task.id))
        await session.execute(
            delete(TaskCollaborator).where(TaskCollaborator.task_id == task.id)
        )
        await session.execute(delete(Task).where(Task.id == task.id))
        await session.execute(delete(User).where(User.id.in_([u.id for u in users])))
        await session.commit()


async def _confirm_on_own_session(
    engine: AsyncEngine,
    service: AssignmentImportService,
    actor: Actor,
    task_id: UUID,
    token: str,
) -> AssignmentImportResult | BusinessError:
    """Run one confirm on an independent session with a real commit.

    Returns the result or the BusinessError raised, so asyncio.gather
    results classify without losing either side (the registration-race
    suite's pattern)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        try:
            return await service.confirm_assignments(session, actor, task_id, token)
        except BusinessError as exc:
            return exc


@pytest.mark.integration
async def test_concurrent_confirm_exactly_one_wins(
    db_engine: AsyncEngine,
    import_redis: aioredis.Redis,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two previews of the same new pair, confirmed concurrently on two
    independent sessions: exactly one insert lands, the loser gets the
    typed duplicate conflict (409), and neither is a 500. The UNIQUE
    constraint is the race closer (spec §7.1: 正式写入时仍依赖数据库
    UNIQUE 兜底).

    The friendly pre-check would serialize most real-world races, so the
    test pins the constraint path itself: the pre-check is held back
    until BOTH confirms have passed it (an asyncio barrier), guaranteeing
    both reach the INSERT with an empty conflict set — only PostgreSQL
    can then decide the winner.
    """
    owner, task = await _seed_committed_owner_task(db_engine)
    try:
        service = _service(import_redis, clock)
        actor = Actor(user_id=owner.id, role=Role.TEACHER)
        data = _csv(("xiaohongshu", "并发导入"))
        tokens = []
        for _ in range(2):
            async with AsyncSession(db_engine) as session:
                preview = await service.preview_assignments(
                    session, actor, task.id, data
                )
            assert preview.preview_token is not None
            tokens.append(preview.preview_token)

        real_existing_pairs = importer._existing_pairs
        both_past_precheck = asyncio.Event()
        arrivals = 0

        async def _synchronized_existing_pairs(
            db: AsyncSession,
            task_id: UUID,
            candidates: list[tuple[str, str]],
        ) -> set[tuple[str, str]]:
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_past_precheck.set()
            await asyncio.wait_for(both_past_precheck.wait(), timeout=10)
            return await real_existing_pairs(db, task_id, candidates)

        # Patched only after the previews: confirm (not preview) resolves
        # the module-global at call time, so both confirms hold at the
        # barrier until each has an empty conflict set.
        monkeypatch.setattr(importer, "_existing_pairs", _synchronized_existing_pairs)

        results = await asyncio.gather(
            _confirm_on_own_session(db_engine, service, actor, task.id, tokens[0]),
            _confirm_on_own_session(db_engine, service, actor, task.id, tokens[1]),
        )

        successes = [r for r in results if isinstance(r, AssignmentImportResult)]
        failures = [r for r in results if isinstance(r, BusinessError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert successes[0].inserted == 1
        assert isinstance(failures[0], DuplicateAssignmentsError)
        assert failures[0].code == ErrorCode.VALIDATION_ERROR
        assert failures[0].status_code == 409  # typed conflict, never a 500
        # details is None only on the constraint-closed path (the pre-check
        # path would list the pair) — the race really went to the database.
        assert failures[0].details is None

        async with AsyncSession(db_engine) as verifier:
            rows = (
                await verifier.scalars(
                    select(Assignment).where(Assignment.task_id == task.id)
                )
            ).all()
            assert len(rows) == 1
            assert (rows[0].platform, rows[0].keyword) == (
                "xiaohongshu",
                "并发导入",
            )
    finally:
        await _cleanup_task(db_engine, task, owner)
