# backend/tests/integration/tasks/test_task_constraints.py
"""Database-level task/assignment/claim constraints (spec §6-8, §31.3-31.5).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects duplicates and hides finished assignments even
when the application forgets:

- UNIQUE(task_id, platform, keyword) on assignments (§31.3);
- at most one active Claim per Assignment via partial unique index (§31.4);
- at most one non-terminal Claim per user per Task via partial unique
  index (§31.5), and terminal Claims (COMPLETED/ABANDONED/EXPIRED) free
  both indexes for re-claiming (spec §8.2);
- COMPLETED assignments never resurface through the AVAILABLE selection
  query that random claiming reads (spec §7).

Rows are built after their parents flush: ids are server-generated, so a
transient parent's id is still None at construction time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _teacher(username: str = "teacher0001") -> User:
    return _user(username=username, role=Role.TEACHER)


def _student(username: str = "20250010001") -> User:
    return _user(username=username, role=Role.STUDENT)


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
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _assignment(
    task: Task,
    *,
    keyword: str = "考研英语",
    platform: str = "xiaohongshu",
    availability: AssignmentAvailability = AssignmentAvailability.AVAILABLE,
) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform=platform,
        keyword=keyword,
        availability_status=availability,
    )


def _claim(
    assignment: Assignment,
    user: User,
    *,
    status: ClaimStatus = ClaimStatus.CLAIMED,
    submission_schema_version: int = 1,
) -> AssignmentClaim:
    deadline = datetime.now(UTC) + timedelta(days=3)
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=status,
        deadline_at=deadline,
        grace_deadline_at=deadline + timedelta(minutes=1440),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=submission_schema_version,
        reward_lock_status=RewardLockStatus.NONE,
    )


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


@pytest.mark.integration
async def test_duplicate_assignment_platform_keyword_rejected(
    db_session: AsyncSession,
) -> None:
    """(task_id, platform, keyword) is unique per task (spec §7, §31.3)."""
    owner = _teacher()
    await _flush(db_session, owner)
    task = _task(owner)
    await _flush(db_session, task)

    await _flush(
        db_session, _assignment(task, keyword="考研英语", platform="xiaohongshu")
    )

    db_session.add(_assignment(task, keyword="考研英语", platform="xiaohongshu"))
    with pytest.raises(IntegrityError, match="uq_assignments_task_id_platform_keyword"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_same_keyword_other_platform_or_task_allowed(
    db_session: AsyncSession,
) -> None:
    """Only the full triple is unique: same keyword on another platform or
    under another task inserts cleanly (spec §7)."""
    owner = _teacher()
    await _flush(db_session, owner)
    task_a = _task(owner)
    task_b = _task(owner, title="第二个任务")
    await _flush(db_session, task_a, task_b)

    await _flush(
        db_session,
        _assignment(task_a, keyword="考研英语", platform="xiaohongshu"),
        _assignment(task_a, keyword="考研英语", platform="zhihu"),
        _assignment(task_b, keyword="考研英语", platform="xiaohongshu"),
    )


@pytest.mark.integration
async def test_duplicate_active_claim_per_assignment_rejected(
    db_session: AsyncSession,
) -> None:
    """One active Claim per Assignment at any instant (spec §8, §31.4).

    The two rows use different active statuses (CLAIMED, UNDER_REVIEW) and
    different users, so only the assignment-level partial index can fire.
    """
    owner = _teacher()
    student_a = _student("20250010001")
    student_b = _student("20250010002")
    await _flush(db_session, owner, student_a, student_b)
    task = _task(owner)
    await _flush(db_session, task)

    assignment = _assignment(task)
    await _flush(db_session, assignment)

    await _flush(db_session, _claim(assignment, student_a, status=ClaimStatus.CLAIMED))

    db_session.add(_claim(assignment, student_b, status=ClaimStatus.UNDER_REVIEW))
    with pytest.raises(IntegrityError, match="uq_assignment_claims_active_assignment"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_nonterminal_claim_per_user_task_rejected(
    db_session: AsyncSession,
) -> None:
    """One non-terminal Claim per user per Task (spec §8.2, §31.5).

    The second claim targets a different Assignment of the same Task, so
    only the (user_id, task_id) partial index can fire.
    """
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)

    assignment_a = _assignment(task, keyword="考研英语")
    assignment_b = _assignment(task, keyword="考研政治")
    await _flush(db_session, assignment_a, assignment_b)

    await _flush(db_session, _claim(assignment_a, student, status=ClaimStatus.CLAIMED))

    db_session.add(_claim(assignment_b, student, status=ClaimStatus.REVISION_REQUIRED))
    with pytest.raises(IntegrityError, match="uq_assignment_claims_active_user_task"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_terminal_claims_free_both_partial_indexes(
    db_session: AsyncSession,
) -> None:
    """Terminal Claims exit both partial indexes, so the same user may
    re-claim the same Task through the same Assignment afterwards
    (spec §8.2: ABANDONED/EXPIRED return the Assignment to AVAILABLE)."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)

    assignment = _assignment(task)
    await _flush(db_session, assignment)

    await _flush(db_session, _claim(assignment, student, status=ClaimStatus.ABANDONED))

    # Same (assignment, user, task) triple as the terminal claim: both
    # partial unique indexes must ignore the ABANDONED row.
    await _flush(db_session, _claim(assignment, student, status=ClaimStatus.CLAIMED))


@pytest.mark.integration
async def test_claim_snapshots_submission_schema_version(
    db_session: AsyncSession,
) -> None:
    """The claim-side submission_schema_version is a §6.2 MUST-snapshot:
    bumping the Task's schema afterwards must not retroactively change the
    submission requirements of already-claimed Claims."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(
        owner,
        submission_schema={"columns": [{"name": "note", "type": "string"}]},
        submission_schema_version=3,
    )
    await _flush(db_session, task)

    assignment = _assignment(task)
    await _flush(db_session, assignment)

    claim = _claim(assignment, student, submission_schema_version=3)
    await _flush(db_session, claim)

    # Teacher evolves the task schema after students have claimed.
    task.submission_schema = {
        "columns": [
            {"name": "note", "type": "string"},
            {"name": "link", "type": "string"},
        ]
    }
    task.submission_schema_version = 4
    await _flush(db_session)
    db_session.expunge_all()

    loaded_claim = await db_session.scalar(
        select(AssignmentClaim).where(AssignmentClaim.id == claim.id)
    )
    loaded_task = await db_session.scalar(select(Task).where(Task.id == task.id))

    assert loaded_task is not None
    assert loaded_task.submission_schema_version == 4
    assert loaded_claim is not None
    assert loaded_claim.submission_schema_version == 3


@pytest.mark.integration
async def test_completed_assignment_not_returned_by_available_query(
    db_session: AsyncSession,
) -> None:
    """The availability query driving random claiming (spec §8.3) sees only
    this task's AVAILABLE assignments; COMPLETED and RETIRED rows of the
    same task and AVAILABLE rows of other tasks stay out of the result."""
    owner = _teacher()
    await _flush(db_session, owner)
    task = _task(owner)
    other_task = _task(owner, title="另一个任务")
    await _flush(db_session, task, other_task)

    available = _assignment(task, keyword="考研英语")
    completed = _assignment(
        task, keyword="考研政治", availability=AssignmentAvailability.COMPLETED
    )
    retired = _assignment(
        task, keyword="考研数学", availability=AssignmentAvailability.RETIRED
    )
    other = _assignment(other_task, keyword="考研英语")
    await _flush(db_session, available, completed, retired, other)

    rows = (
        await db_session.scalars(
            select(Assignment).where(
                Assignment.task_id == task.id,
                Assignment.availability_status == AssignmentAvailability.AVAILABLE,
            )
        )
    ).all()

    assert {row.id for row in rows} == {available.id}
    assert completed.id not in {row.id for row in rows}


@pytest.mark.integration
async def test_terminal_at_column_comment_documents_semantics() -> None:
    """The terminal_at column carries its semantic contract as a database
    comment (final-review Minor 1): when the claim reached a terminal
    status, NULL while active, and that the daily abandon cap counts
    ABANDONED rows by this instant — a reader discovering the column in
    psql should not have to open the service to learn this. The ORM
    comment is what fresh alembic upgrades and create_all environments
    both install (migration 0003 carries the same text).
    """
    comment = AssignmentClaim.__table__.c.terminal_at.comment
    assert comment is not None
    assert "COMPLETED/ABANDONED/EXPIRED" in comment
    assert "NULL" in comment
    assert "abandon" in comment
