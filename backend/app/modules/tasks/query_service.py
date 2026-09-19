# backend/app/modules/tasks/query_service.py
"""Task statistics aggregate (spec §41 Teacher 工作台 "Task statistics";
plan 03 task 3).

Design decisions:

- **Counts only, by construction.** The aggregate is computed with
  GROUP BY / COUNT over the task's own rows; no Assignment row (platform,
  keyword, payload) is ever loaded, so the DTO cannot leak what spec
  §40/§42 keep hidden (领取后不暴露 Assignment 列表). ``TaskStatistics``
  is an explicitly enumerated frozen dataclass, not a serialized ORM
  object (backend-engineering §8-§9).
- **Read authorization.** Owner, Admin, or a collaborator holding
  VIEW_TASK; any other Teacher (and Students) get ``PERMISSION_DENIED``.
  The owner/Admin check runs before any query besides the task lookup.
- **Completion rate** = COMPLETED / (AVAILABLE + OCCUPIED + COMPLETED)
  assignments. RETIRED units are excluded from the denominator: the
  owner withdrew them from the pool, so they were never completable
  work; an empty pool reports 0.0, never a division by zero.
- **Active claims** are the shared ``ACTIVE_CLAIM_STATUSES`` tuple from
  models.py (CLAIMED/VALIDATING/UNDER_REVIEW/REVISION_REQUIRED) — the
  same definition that builds the partial unique indexes, so
  "active" can never drift between uniqueness and statistics.
- **Submission counts are a Plan 04 seam.** The submissions table does
  not exist yet; the field ships empty and NO submission query is
  invented here. Plan 04 fills it against its own status axes.
- **Rating summary** comes from the cross-module ``RatingSummaryPort``
  (interfaces.md: Plan 03 ships a null port, Plan 06 supplies the
  concrete adapter backed by TaskRating). The port sketch in
  interfaces.md reads ``summary(task_id) -> RatingSummary | None``; the
  Protocol below keeps that name and shape but is ``async`` because the
  Plan 06 adapter reads PostgreSQL — the adapter receives its session at
  construction, like every per-request-scoped port. ``summary`` is
  None-safe: a task nobody rated reports ``rating=None``.
- **No Clock.** Statistics are as-of-now snapshots with no
  time-dependent business rule, so no clock is injected (plan pre-flight
  decision; revisit only if a query ever needs business time).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.events import Actor
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.enums import AssignmentAvailability
from app.modules.tasks.models import (
    ACTIVE_CLAIM_STATUSES,
    Assignment,
    AssignmentClaim,
    Task,
    TaskCollaborator,
)
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "NullRatingSummaryPort",
    "RatingSummary",
    "RatingSummaryPort",
    "TaskQueryService",
    "TaskStatistics",
]

_STATISTICS_DENIED_MESSAGE = (
    "只有任务所有者、拥有 VIEW_TASK 权限的协作者或管理员可以查看任务统计"
)


# --- cross-module rating port (Plan 06 supplies the real adapter) ----------------


@dataclass(frozen=True, slots=True)
class RatingSummary:
    """The public rating aggregate of one Task (spec §20: 前台只展示聚合分
    和数量 — average and count, never who gave what)."""

    average: float
    count: int


class RatingSummaryPort(Protocol):
    """Read-side rating port; Plan 06's adapter is backed by TaskRating."""

    async def summary(self, task_id: UUID) -> RatingSummary | None: ...


class NullRatingSummaryPort:
    """The Plan 03 stand-in: every task reports "not rated yet".

    Used until Plan 06 wires the community-backed adapter, and by tests
    that exercise the None path.
    """

    async def summary(self, task_id: UUID) -> RatingSummary | None:
        return None


# --- the read DTO -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskStatistics:
    """Counts-only task aggregate (spec §41; privacy per §40/§42).

    ``submission_counts`` is the documented Plan 04 seam: empty until
    the submission module lands, and its key set will be defined by
    Plan 04's own status axes (validation / review), not invented here.
    """

    task_id: UUID
    assignments_available: int
    assignments_occupied: int
    assignments_completed: int
    assignments_retired: int
    active_claims: int
    completion_rate: float
    rating: RatingSummary | None
    submission_counts: Mapping[str, int]


# --- the service --------------------------------------------------------------------


class TaskQueryService:
    """Read-side task aggregates; ``get_task_statistics`` is the Teacher
    workbench's statistics surface (spec §41)."""

    async def get_task_statistics(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        rating_port: RatingSummaryPort,
    ) -> TaskStatistics:
        """Aggregate one task's participation numbers for an authorized
        reader (owner / VIEW_TASK collaborator / Admin)."""
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        await self._require_statistics_access(db, task, actor)

        availability_counts: dict[str, int] = {}
        availability_rows = await db.execute(
            select(Assignment.availability_status, func.count())
            .where(Assignment.task_id == task_id)
            .group_by(Assignment.availability_status)
        )
        for status, count in availability_rows:
            availability_counts[status] = count

        def availability(member: AssignmentAvailability) -> int:
            return int(availability_counts.get(member.value, 0))

        available = availability(AssignmentAvailability.AVAILABLE)
        occupied = availability(AssignmentAvailability.OCCUPIED)
        completed = availability(AssignmentAvailability.COMPLETED)
        retired = availability(AssignmentAvailability.RETIRED)

        active_claims = int(
            await db.scalar(
                select(func.count())
                .select_from(AssignmentClaim)
                .where(
                    AssignmentClaim.task_id == task_id,
                    AssignmentClaim.status.in_(
                        [status.value for status in ACTIVE_CLAIM_STATUSES]
                    ),
                )
            )
            or 0
        )

        completable = available + occupied + completed
        completion_rate = completed / completable if completable else 0.0

        return TaskStatistics(
            task_id=task_id,
            assignments_available=available,
            assignments_occupied=occupied,
            assignments_completed=completed,
            assignments_retired=retired,
            active_claims=active_claims,
            completion_rate=completion_rate,
            rating=await rating_port.summary(task_id),
            submission_counts={},
        )

    @staticmethod
    async def _require_statistics_access(
        db: AsyncSession, task: Task, actor: Actor
    ) -> None:
        """Owner, Admin, or a collaborator holding VIEW_TASK (spec §4.2)."""
        if actor.user_id == task.owner_teacher_id or is_admin(actor.role):
            return
        permissions = await db.scalar(
            select(TaskCollaborator.permissions).where(
                TaskCollaborator.task_id == task.id,
                TaskCollaborator.teacher_id == actor.user_id,
            )
        )
        if permissions is None or CollaboratorPermission.VIEW_TASK not in permissions:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _STATISTICS_DENIED_MESSAGE,
                status_code=403,
            )
