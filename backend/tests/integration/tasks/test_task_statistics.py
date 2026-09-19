# backend/tests/integration/tasks/test_task_statistics.py
"""Task statistics aggregate (spec §41 Teacher 工作台; plan 03 T3).

Pinned behavior:

- Counts are pure aggregates over the task's own rows: Assignment counts
  by availability, active (non-terminal) Claim count, completion rate,
  and the rating summary supplied by the RatingSummaryPort. Nothing that
  could carry an Assignment payload (platform/keyword) ever enters the
  DTO — spec §40/§42 keep Assignment lists hidden, so statistics are
  counts only by construction.
- Submission status counts are a Plan 04 seam: the field exists and is
  empty until the submission module lands; no submission query is
  invented here.
- Read authorization: owner, Admin, or a collaborator holding
  VIEW_TASK; any other Teacher (and Students) get PERMISSION_DENIED.
- The rating port is None-safe: NullRatingSummaryPort yields rating
  None; a fake port's summary is surfaced verbatim.
- No Clock is injected: statistics are as-of-now snapshots with no
  time-dependent business rule (plan pre-flight decision).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task, TaskCollaborator
from app.modules.tasks.query_service import (
    NullRatingSummaryPort,
    RatingSummary,
    TaskQueryService,
)
from app.modules.tasks.service import TaskNotFoundError

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


@dataclass
class FakeRatingSummaryPort:
    """Test double for the cross-module rating port: hands out programmed
    summaries and records every lookup for exact-call assertions."""

    summaries: dict[UUID, RatingSummary] = field(default_factory=dict)
    calls: list[UUID] = field(default_factory=list)

    async def summary(self, task_id: UUID) -> RatingSummary | None:
        self.calls.append(task_id)
        return self.summaries.get(task_id)


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
    task: Task, keyword: str, availability: AssignmentAvailability
) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=keyword,
        availability_status=availability,
    )


def _claim(
    assignment: Assignment, user: User, status: ClaimStatus
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
        submission_schema_version=1,
        reward_lock_status=RewardLockStatus.NONE,
    )


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


async def _seed_population(db_session: AsyncSession) -> tuple[Task, User, User]:
    """A coherent slice: 2 AVAILABLE (one with an ABANDONED claim that
    returned it), 2 OCCUPIED (active claims CLAIMED / UNDER_REVIEW), 2
    COMPLETED (terminal claims), 1 RETIRED — plus another task whose rows
    must stay out of the statistics."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    collaborator = _user(username="teacher0002", role=Role.TEACHER)
    student_a = _user(username="20250010001", role=Role.STUDENT)
    student_b = _user(username="20250010002", role=Role.STUDENT)
    student_c = _user(username="20250010003", role=Role.STUDENT)
    student_d = _user(username="20250010004", role=Role.STUDENT)
    student_e = _user(username="20250010005", role=Role.STUDENT)
    db_session.add_all(
        [owner, collaborator, student_a, student_b, student_c, student_d, student_e]
    )
    await db_session.flush()

    task = _task(owner)
    other_task = _task(owner, title="另一个任务")
    db_session.add_all([task, other_task])
    await db_session.flush()

    available_free = _assignment(task, "考研英语", AssignmentAvailability.AVAILABLE)
    available_again = _assignment(task, "考研政治", AssignmentAvailability.AVAILABLE)
    occupied_a = _assignment(task, "考研数学", AssignmentAvailability.OCCUPIED)
    occupied_b = _assignment(task, "考研日语", AssignmentAvailability.OCCUPIED)
    completed_a = _assignment(task, "考研物理", AssignmentAvailability.COMPLETED)
    completed_b = _assignment(task, "考研化学", AssignmentAvailability.COMPLETED)
    retired = _assignment(task, "考研生物", AssignmentAvailability.RETIRED)
    other_available = _assignment(
        other_task, "考研英语", AssignmentAvailability.AVAILABLE
    )
    db_session.add_all(
        [
            available_free,
            available_again,
            occupied_a,
            occupied_b,
            completed_a,
            completed_b,
            retired,
            other_available,
        ]
    )
    await db_session.flush()

    db_session.add_all(
        [
            # Active claims: one CLAIMED, one UNDER_REVIEW (different
            # assignments and users so both partial indexes allow them).
            _claim(occupied_a, student_a, ClaimStatus.CLAIMED),
            _claim(occupied_b, student_b, ClaimStatus.UNDER_REVIEW),
            # Terminal claims: COMPLETED x2 and one ABANDONED that put
            # `available_again` back to AVAILABLE (spec §8.2).
            _claim(completed_a, student_c, ClaimStatus.COMPLETED),
            _claim(completed_b, student_d, ClaimStatus.COMPLETED),
            _claim(available_again, student_e, ClaimStatus.ABANDONED),
            # Foreign-task active claim: must not leak into task stats.
            _claim(other_available, student_a, ClaimStatus.CLAIMED),
        ]
    )
    await db_session.flush()
    return task, owner, collaborator


@pytest.mark.integration
async def test_counts_and_completion_rate_for_owner(
    db_session: AsyncSession,
) -> None:
    """Owner sees the exact aggregates for the seeded population:
    2/2/2 assignments (plus 1 retired), 2 active claims (CLAIMED and
    UNDER_REVIEW only — terminal claims never count), completion rate
    2/(2+2+2), no rating (null port), and the empty Plan 04 submission
    seam."""
    task, owner, _ = await _seed_population(db_session)

    stats = await TaskQueryService().get_task_statistics(
        db_session, _actor(owner), task.id, NullRatingSummaryPort()
    )

    assert stats.task_id == task.id
    assert stats.assignments_available == 2
    assert stats.assignments_occupied == 2
    assert stats.assignments_completed == 2
    assert stats.assignments_retired == 1
    assert stats.active_claims == 2
    assert stats.completion_rate == pytest.approx(2 / 6)
    assert stats.rating is None
    assert stats.submission_counts == {}


@pytest.mark.integration
async def test_empty_task_reports_zero_counts(db_session: AsyncSession) -> None:
    """A task with no assignments and no claims reports all-zero counts
    and a 0.0 completion rate rather than dividing by zero."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    db_session.add(owner)
    await db_session.flush()
    task = _task(owner)
    db_session.add(task)
    await db_session.flush()

    stats = await TaskQueryService().get_task_statistics(
        db_session, _actor(owner), task.id, NullRatingSummaryPort()
    )

    assert stats.assignments_available == 0
    assert stats.assignments_occupied == 0
    assert stats.assignments_completed == 0
    assert stats.assignments_retired == 0
    assert stats.active_claims == 0
    assert stats.completion_rate == 0.0


@pytest.mark.integration
async def test_rating_port_summary_surfaced(db_session: AsyncSession) -> None:
    """A supplied summary flows through verbatim (spec §20: 聚合分和数量),
    and the port is consulted exactly once with the task's id."""
    task, owner, _ = await _seed_population(db_session)
    summary = RatingSummary(average=4.5, count=12)
    port = FakeRatingSummaryPort({task.id: summary})

    stats = await TaskQueryService().get_task_statistics(
        db_session, _actor(owner), task.id, port
    )

    assert stats.rating == summary
    assert port.calls == [task.id]


@pytest.mark.integration
async def test_collaborator_with_view_task_allowed(
    db_session: AsyncSession,
) -> None:
    """A collaborator holding VIEW_TASK reads the same aggregates."""
    task, owner, collaborator = await _seed_population(db_session)

    db_session.add(
        TaskCollaborator(
            task_id=task.id,
            teacher_id=collaborator.id,
            permissions=[CollaboratorPermission.VIEW_TASK],
        )
    )
    await db_session.flush()

    stats = await TaskQueryService().get_task_statistics(
        db_session, _actor(collaborator), task.id, NullRatingSummaryPort()
    )

    assert stats.assignments_available == 2
    assert stats.active_claims == 2


@pytest.mark.integration
async def test_collaborator_without_view_task_denied(
    db_session: AsyncSession,
) -> None:
    """A capability set without VIEW_TASK does not unlock statistics."""
    task, owner, collaborator = await _seed_population(db_session)

    db_session.add(
        TaskCollaborator(
            task_id=task.id,
            teacher_id=collaborator.id,
            permissions=[CollaboratorPermission.MANAGE_ASSIGNMENTS],
        )
    )
    await db_session.flush()

    with pytest.raises(BusinessError) as denied:
        await TaskQueryService().get_task_statistics(
            db_session, _actor(collaborator), task.id, NullRatingSummaryPort()
        )
    assert denied.value.code == ErrorCode.PERMISSION_DENIED
    assert denied.value.status_code == 403


@pytest.mark.integration
async def test_unrelated_teacher_and_student_denied(
    db_session: AsyncSession,
) -> None:
    """A Teacher with no standing and any Student are both denied."""
    task, owner, _ = await _seed_population(db_session)
    outsider = _user(username="teacher0009", role=Role.TEACHER)
    student = _user(username="20250010009", role=Role.STUDENT)
    db_session.add_all([outsider, student])
    await db_session.flush()

    for user in (outsider, student):
        with pytest.raises(BusinessError) as denied:
            await TaskQueryService().get_task_statistics(
                db_session, _actor(user), task.id, NullRatingSummaryPort()
            )
        assert denied.value.code == ErrorCode.PERMISSION_DENIED


@pytest.mark.integration
async def test_admin_reads_any_task(db_session: AsyncSession) -> None:
    """Admin may query every task's statistics (spec §4.3)."""
    task, _, _ = await _seed_population(db_session)
    admin = _user(username="admin0001", role=Role.ADMIN)
    db_session.add(admin)
    await db_session.flush()

    stats = await TaskQueryService().get_task_statistics(
        db_session, _actor(admin), task.id, NullRatingSummaryPort()
    )

    assert stats.task_id == task.id
    assert stats.assignments_completed == 2


@pytest.mark.integration
async def test_unknown_task_not_found(db_session: AsyncSession) -> None:
    """Statistics for a missing task raise the shared typed error."""
    owner = _user(username="teacher0001", role=Role.TEACHER)
    db_session.add(owner)
    await db_session.flush()

    with pytest.raises(TaskNotFoundError):
        await TaskQueryService().get_task_statistics(
            db_session,
            _actor(owner),
            UUID("00000000-0000-0000-0000-000000000000"),
            NullRatingSummaryPort(),
        )
