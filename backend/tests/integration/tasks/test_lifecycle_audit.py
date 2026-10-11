# backend/tests/integration/tasks/test_lifecycle_audit.py
"""Task lifecycle audit rows (spec §30, batch 4 item 5; owner ruling
2026-10-11).

Seven actions append AuditLog rows inside their own service
transactions — publish/pause/resume/close/archive (status snapshots),
collaborator add/remove (permission sets), and the import confirm
(inserted count). The tests pin the three §30 discipline properties:

- one row per committed action, with the actor, target, and snapshots
  the spec's audit reader needs (publish vs resume are distinct verbs
  on the same PUBLISHED target);
- transactionality: a REJECTED transition (illegal edge) leaves no
  row — the append rides the same transaction as the mutation;
- idempotence of the RECORD, not the action: a repeated pause is
  rejected by the transition table and never writes a second row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.modules.audit.models import AuditLog
from app.modules.identity.directory import SqlAlchemyUserDirectory
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.tasks.collaborator_service import TaskCollaboratorService
from app.modules.tasks.enums import (
    AssignmentAvailability,
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.importer import AssignmentImportService
from app.modules.tasks.models import Assignment, Task, TaskCollaborator
from app.modules.tasks.service import TaskService

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_TASKS_TEST_REDIS_DB = 14

pytestmark = pytest.mark.integration


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"integration tests refuse non-local Redis: {base!r}")
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


@pytest_asyncio.fixture(autouse=True)
async def swept(db_session: AsyncSession) -> None:
    """Deterministic audit assertions: this test's rows are the only
    task-targeted rows (claims ride the harness transaction and roll
    back; committed leftovers from other suites are swept the same
    way, then restored at teardown)."""
    await db_session.execute(delete(Assignment))
    await db_session.execute(delete(TaskCollaborator))
    await db_session.execute(delete(Task))
    await db_session.execute(delete(AuditLog).where(AuditLog.target_type == "task"))
    await db_session.flush()


async def _seed_teacher(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="审计教师",
        phone_e164=None,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _seed_task(owner_id: Any) -> Task:
    return Task(
        owner_teacher_id=owner_id,
        title="生命周期审计任务",
        description="审计行覆盖用种子任务。",
        task_type=TaskType.DATA_CRAWL.value,
        rarity=TaskRarity.NORMAL.value,
        base_reward_points=100,
        status=TaskStatus.DRAFT.value,
        deadline_mode=DeadlineMode.RELATIVE.value,
        duration_minutes=4320,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
        submission_schema={"columns": ["title", "likes"]},
        submission_schema_version=1,
    )


def _service(clock: FrozenClock) -> TaskService:
    return TaskService(clock=clock, max_upload_bytes=10 * 1024 * 1024)


async def _task_audit_rows(db: AsyncSession, task_id: Any) -> list[AuditLog]:
    return list(
        await db.scalars(
            select(AuditLog)
            .where(AuditLog.target_type == "task", AuditLog.target_id == str(task_id))
            .order_by(AuditLog.created_at, AuditLog.id)
        )
    )


async def test_lifecycle_verbs_each_append_one_audited_row(
    db_session: AsyncSession,
) -> None:
    """publish -> pause -> resume -> close -> archive: five committed
    actions, five rows, in order, with before/after status snapshots
    and the acting teacher as actor. Publish and resume are DISTINCT
    actions on the same PUBLISHED target."""
    teacher = await _seed_teacher(db_session, "audit-teacher@pku.edu.cn")
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()

    service = _service(FrozenClock(_T0))
    actor = Actor(user_id=teacher.id, role=Role.TEACHER)
    await service.publish_task(db_session, actor, task.id)
    await service.pause_task(db_session, actor, task.id)
    await service.resume_task(db_session, actor, task.id)
    await service.close_task(db_session, actor, task.id)
    await service.archive_task(db_session, actor, task.id)

    rows = await _task_audit_rows(db_session, task.id)
    # AuditLog ids are random UUIDs and created_at shares one
    # transaction instant, so there is no storage order to assert —
    # the contract is exactly one row per committed action, each with
    # its own status edge.
    by_action = {row.action: row for row in rows}
    assert set(by_action) == {
        "TASK_PUBLISHED",
        "TASK_PAUSED",
        "TASK_RESUMED",
        "TASK_CLOSED",
        "TASK_ARCHIVED",
    }
    assert all(row.actor_user_id == teacher.id for row in rows)
    assert by_action["TASK_PUBLISHED"].before_snapshot == {"status": "DRAFT"}
    assert by_action["TASK_PUBLISHED"].after_snapshot == {"status": "PUBLISHED"}
    assert by_action["TASK_RESUMED"].before_snapshot == {"status": "PAUSED"}
    assert by_action["TASK_RESUMED"].after_snapshot == {"status": "PUBLISHED"}
    assert by_action["TASK_CLOSED"].after_snapshot == {"status": "CLOSED"}
    assert by_action["TASK_ARCHIVED"].before_snapshot == {"status": "CLOSED"}


async def test_rejected_transition_leaves_no_audit_row(
    db_session: AsyncSession,
) -> None:
    """Transactionality: the append rides the mutation's transaction —
    an illegal edge (pausing a DRAFT) is rejected BEFORE any append,
    and even the audit row a FAILED commit would have written rolls
    back with it."""
    from app.modules.tasks.service import IllegalTransitionError

    teacher = await _seed_teacher(db_session, "audit-teacher@pku.edu.cn")
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()

    service = _service(FrozenClock(_T0))
    actor = Actor(user_id=teacher.id, role=Role.TEACHER)
    with pytest.raises(IllegalTransitionError):
        await service.pause_task(db_session, actor, task.id)

    assert await _task_audit_rows(db_session, task.id) == []


async def test_repeated_pause_writes_exactly_one_row(
    db_session: AsyncSession,
) -> None:
    """The action is idempotent by rejection: a second pause dies on
    the transition table and never duplicates the audit row."""
    from app.modules.tasks.service import IllegalTransitionError

    teacher = await _seed_teacher(db_session, "audit-teacher@pku.edu.cn")
    task = _seed_task(teacher.id)
    db_session.add(task)
    await db_session.flush()

    service = _service(FrozenClock(_T0))
    actor = Actor(user_id=teacher.id, role=Role.TEACHER)
    await service.publish_task(db_session, actor, task.id)
    await service.pause_task(db_session, actor, task.id)
    with pytest.raises(IllegalTransitionError):
        await service.pause_task(db_session, actor, task.id)

    rows = await _task_audit_rows(db_session, task.id)
    assert sorted(row.action for row in rows) == [
        "TASK_PAUSED",
        "TASK_PUBLISHED",
    ]


async def test_collaborator_add_and_remove_are_audited(
    db_session: AsyncSession,
) -> None:
    """Grant and removal each append one row carrying the collaborator
    and the permission set (the §30 reader's who-got-what)."""
    owner = await _seed_teacher(db_session, "audit-owner@pku.edu.cn")
    peer = await _seed_teacher(db_session, "audit-peer@pku.edu.cn")
    task = _seed_task(owner.id)
    task.status = TaskStatus.PUBLISHED.value
    db_session.add(task)
    await db_session.flush()

    service = TaskCollaboratorService(user_directory=SqlAlchemyUserDirectory())
    actor = Actor(user_id=owner.id, role=Role.TEACHER)
    await service.add_collaborator(
        db_session, actor, task.id, peer.id, ("VIEW_TASK", "REVIEW_SUBMISSIONS")
    )
    await service.remove_collaborator(db_session, actor, task.id, peer.id)

    rows = await _task_audit_rows(db_session, task.id)
    by_action = {row.action: row for row in rows}
    assert set(by_action) == {"TASK_COLLABORATOR_ADDED", "TASK_COLLABORATOR_REMOVED"}
    assert by_action["TASK_COLLABORATOR_ADDED"].details == {
        "collaborator_teacher_id": str(peer.id),
        "permissions": ["REVIEW_SUBMISSIONS", "VIEW_TASK"],
    }
    assert by_action["TASK_COLLABORATOR_REMOVED"].details[
        "collaborator_teacher_id"
    ] == str(peer.id)


async def test_import_confirm_appends_one_row_with_count(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
) -> None:
    """The confirm leg — the one that writes Assignment rows — is the
    audited import verb; the row carries the inserted count."""
    teacher = await _seed_teacher(db_session, "audit-teacher@pku.edu.cn")
    task = _seed_task(teacher.id)
    task.status = TaskStatus.PUBLISHED.value
    db_session.add(task)
    await db_session.flush()

    service = AssignmentImportService(
        redis=api_redis,
        clock=FrozenClock(_T0),
        max_file_bytes=2 * 1024 * 1024,
        max_rows=5000,
        keyword_max_length=200,
        preview_ttl_seconds=600,
    )
    actor = Actor(user_id=teacher.id, role=Role.TEACHER)
    preview = await service.preview_assignments(
        db_session,
        actor,
        task.id,
        b"platform,keyword\nxiaohongshu,audit-one\nxiaohongshu,audit-two\n",
    )
    assert preview.preview_token is not None
    result = await service.confirm_assignments(
        db_session, actor, task.id, preview.preview_token
    )
    assert result.inserted == 2

    rows = await _task_audit_rows(db_session, task.id)
    assert [row.action for row in rows] == ["TASK_ASSIGNMENTS_IMPORTED"]
    assert rows[0].details == {"inserted": 2}
    # The audited verb left the real rows behind (same transaction).
    statuses = await db_session.scalars(
        select(Assignment.availability_status).where(Assignment.task_id == task.id)
    )
    assert set(statuses) == {AssignmentAvailability.AVAILABLE.value}
