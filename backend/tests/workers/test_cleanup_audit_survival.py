# backend/tests/workers/test_cleanup_audit_survival.py
"""Spec §13's audit-survival half of the cleanup contract, on real
PostgreSQL: «数据库业务元数据、Submission 校验报告和审计记录不随原
文件删除» — the row-level tests already pin that only deleted_at moves
on the Submission and the e2e pins metadata + validation_report; the
AUDIT rows' survival (file-retention matrix G-3) had only the
by-construction argument (cleanup never writes the audit table), no
assertion.

This leg makes the survival observable instead of implied: one real
submission whose object is due, one real §30 audit row written through
AuditLogWriter (the SUBMISSION_APPROVED shape the review flow produces
for that submission), then the REAL repository + service path deletes
the object and marks the row — and the audit row reads back
field-for-field identical. The cleanup's write scope staying off the
audit table is the mechanism; this test is the tripwire if that scope
ever widens.

Harness conventions: test_file_cleanup.py's real-PG legs — explicit
committed sessions from a NullPool engine factory, per-phase
asyncio.run on fresh loops, per-run usernames, committed DELETE
cleanup in FK order (plus this file's own audit rows) in ``finally``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditLogWriter
from app.modules.files.cleanup_service import CleanupSummary, cleanup_expired_files
from app.modules.identity.events import Actor
from tests.fakes.integrations import FakeObjectStorage

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)

_TEST_DATABASE_MARKER = "campusquest_test"
_DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://test:test@localhost:15434/campusquest_test_3"
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _database_url() -> str:
    """The integration database URL, refusing non-test databases."""
    import os

    from sqlalchemy.engine import make_url

    url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    database = make_url(url).database or ""
    if _TEST_DATABASE_MARKER not in database:
        pytest.fail(
            f"Refusing cleanup-audit tests against non-test database "
            f"{database!r} (DATABASE_URL={url!r}): the database name must "
            f"contain {_TEST_DATABASE_MARKER!r}."
        )
    return url


def _factory() -> async_sessionmaker[AsyncSession]:
    """NullPool session factory: fresh connection per checkout, so
    asyncio.run phases on fresh loops never share a pooled connection."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_world(
    maker: async_sessionmaker[AsyncSession], run: str
) -> dict[str, Any]:
    """One teacher/student/task/assignment/claim, one DUE submission,
    and one §30 SUBMISSION_APPROVED audit row targeting that submission
    (written through the real writer) — the pre-cleanup state of a file
    whose review history must outlive its deletion."""
    from app.modules.identity.enums import Role, UserStatus
    from app.modules.identity.models import User
    from app.modules.submissions.models import Submission
    from app.modules.submissions.review_service import AUDIT_ACTION_SUBMISSION_APPROVED
    from app.modules.tasks.enums import (
        AssignmentAvailability,
        DeadlineMode,
        RewardLockStatus,
        TaskRarity,
        TaskStatus,
        TaskType,
    )
    from app.modules.tasks.models import Assignment, AssignmentClaim, Task

    object_key = f"submissions/{run}-audit-survival/{uuid.uuid4()}.csv"
    async with maker() as session:
        teacher = User(
            username=f"t{run}",
            password_hash=_PASSWORD_HASH,
            nickname=f"老师{run[-4:]}",
            phone_e164=None,
            role=Role.TEACHER,
            status=UserStatus.ACTIVE,
        )
        student = User(
            username=f"2025{run}001",
            password_hash=_PASSWORD_HASH,
            nickname=f"同学{run[-4:]}",
            phone_e164=None,
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
        session.add_all((teacher, student))
        await session.flush()
        task = Task(
            owner_teacher_id=teacher.id,
            title="期末课程问卷数据采集",
            description="采集问卷数据。",
            task_type=TaskType.DATA_CRAWL,
            rarity=TaskRarity.NORMAL,
            base_reward_points=100,
            status=TaskStatus.PUBLISHED,
            deadline_mode=DeadlineMode.RELATIVE,
            duration_minutes=4320,
            submission_schema={"columns": [{"name": "note", "type": "string"}]},
            submission_schema_version=1,
            allowed_file_types=["CSV"],
            max_file_size_bytes=10 * 1024 * 1024,
            notification_channels=["SMS"],
        )
        session.add(task)
        await session.flush()
        assignment = Assignment(
            task_id=task.id,
            platform="xiaohongshu",
            keyword=f"问卷{run[-4:]}",
            availability_status=AssignmentAvailability.OCCUPIED,
        )
        session.add(assignment)
        await session.flush()
        claim = AssignmentClaim(
            assignment_id=assignment.id,
            task_id=task.id,
            user_id=student.id,
            status="CLAIMED",
            claimed_at=NOW - timedelta(days=400),
            deadline_at=NOW - timedelta(days=200),
            grace_deadline_at=NOW - timedelta(days=199),
            reward_policy_snapshot={"version": 1},
            base_reward_points_snapshot=100,
            submission_schema_version=1,
            reward_lock_status=RewardLockStatus.NONE,
        )
        session.add(claim)
        await session.flush()
        submission = Submission(
            claim_id=claim.id,
            version=1,
            object_key=object_key,
            original_filename="数据.csv",
            declared_type="CSV",
            file_size=128,
            submitted_at=NOW - timedelta(days=365),
            validation_status="VALIDATED",
            retention_until=NOW - timedelta(days=30),
            retention_permanent=False,
        )
        session.add(submission)
        await session.flush()
        claim.latest_submission_id = submission.id

        # The §30 durable trace of the submission's review decision —
        # the exact row shape the review flow writes (actor=teacher,
        # target=submission, the state-migration snapshot pair).
        await AuditLogWriter().append(
            session,
            actor=Actor(user_id=teacher.id, role=Role.TEACHER),
            action=AUDIT_ACTION_SUBMISSION_APPROVED,
            target_type="submission",
            target_id=str(submission.id),
            before_snapshot={
                "submission_review_status": "PENDING_REVIEW",
                "claim_status": "UNDER_REVIEW",
                "reward_lock_status": "PROVISIONAL",
                "reward_tier_locked": 100,
                "locked_reward_points": 100,
            },
            after_snapshot={
                "submission_review_status": "APPROVED",
                "claim_status": "COMPLETED",
                "reward_lock_status": "CONFIRMED",
                "reward_tier_locked": 100,
                "locked_reward_points": 100,
                "points_granted": 100,
            },
            details={
                "submission_id": str(submission.id),
                "reviewer_id": str(teacher.id),
            },
        )
        await session.commit()
        return {
            "teacher_id": teacher.id,
            "student_id": student.id,
            "task_id": task.id,
            "claim_id": claim.id,
            "submission_id": submission.id,
            "object_key": object_key,
        }


async def _cleanup_world(
    maker: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> None:
    """Explicit committed cleanup in FK order, the file's OWN audit rows
    first (audit_logs carries no FK — nothing cascades to it)."""
    from app.modules.identity.models import User
    from app.modules.submissions.models import Submission
    from app.modules.tasks.models import Assignment, AssignmentClaim, Task

    async with maker() as session:
        await session.execute(
            delete(AuditLog).where(AuditLog.actor_user_id == world["teacher_id"])
        )
        await session.execute(
            delete(Submission).where(Submission.claim_id == world["claim_id"])
        )
        await session.execute(
            delete(AssignmentClaim).where(AssignmentClaim.id == world["claim_id"])
        )
        await session.execute(
            delete(Assignment).where(Assignment.task_id == world["task_id"])
        )
        await session.execute(delete(Task).where(Task.id == world["task_id"]))
        await session.execute(
            delete(User).where(User.id.in_((world["teacher_id"], world["student_id"])))
        )
        await session.commit()


async def _audit_snapshot(
    maker: async_sessionmaker[AsyncSession], submission_id: UUID
) -> dict[str, Any] | None:
    """The submission's §30 rows as comparable field snapshots."""
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.target_id == str(submission_id))
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        return {
            "actions": sorted(row.action for row in rows),
            "actor_user_ids": sorted(str(row.actor_user_id) for row in rows),
            "actor_roles": sorted(row.actor_role for row in rows),
            "target": (rows[0].target_type, rows[0].target_id),
            "snapshots": sorted(
                (
                    str(row.before_snapshot),
                    str(row.after_snapshot),
                    str(row.details),
                    str(row.reason),
                )
                for row in rows
            ),
            "ids": sorted((str(row.id), str(row.created_at)) for row in rows),
        }


def test_cleanup_deletion_keeps_the_submission_audit_rows() -> None:
    """§13: the cleanup deletes the expired object and marks the row —
    and the audit record of the submission's life (its SUBMISSION_APPROVED
    §30 row) survives field-for-field: same id and created_at, same
    actor, same action, same target, same snapshot pair. Survival is
    asserted AFTER the deletion observably happened (object gone,
    deleted_at set), so a cleanup that skipped the file could not pass."""
    from app.modules.submissions.models import Submission
    from app.workers.jobs.cleanup_files import SubmissionCleanupRepository

    maker = _factory()
    run = uuid.uuid4().hex[:8]
    world = asyncio.run(_seed_world(maker, run))
    try:
        before = asyncio.run(_audit_snapshot(maker, world["submission_id"]))
        assert before is not None, "the seeded audit row is missing pre-cleanup"
        assert before["actions"] == ["SUBMISSION_APPROVED"]

        storage = FakeObjectStorage()
        storage.put_object(object_key=world["object_key"], size=128)
        repository = SubmissionCleanupRepository(maker, lease_seconds=300)
        summary = asyncio.run(cleanup_expired_files(NOW, repository, storage))

        # The deletion really ran: object gone, row marked.
        assert summary == CleanupSummary(scanned=1, deleted=1)
        assert storage.deleted_keys == [world["object_key"]]
        assert storage.head_object(object_key=world["object_key"]) is None

        async def _deleted_at() -> datetime | None:
            async with maker() as session:
                row = await session.get(Submission, world["submission_id"])
                assert row is not None
                return row.deleted_at

        assert asyncio.run(_deleted_at()) == NOW

        # §13 survival: the audit row is untouched by the deletion.
        after = asyncio.run(_audit_snapshot(maker, world["submission_id"]))
        assert after == before, (
            "the cleanup path mutated the submission's audit rows — spec §13 "
            "keeps audit records when the original file is deleted"
        )
    finally:
        asyncio.run(_cleanup_world(maker, world))
