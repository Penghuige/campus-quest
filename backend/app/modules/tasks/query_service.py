# backend/app/modules/tasks/query_service.py
"""Task read side: the student surfaces and the statistics aggregate
(spec §28, §41-§42).

Design decisions:

- **Counts only, by construction.** Availability numbers are computed
  with GROUP BY / COUNT over the task's own rows; no Assignment row
  (platform, keyword, payload) is ever loaded outside an owner-scoped
  claim view, so a list or detail DTO cannot leak what spec §40/§42 keep
  hidden (领取后不暴露 Assignment 列表). Every read DTO is an explicitly
  enumerated frozen dataclass, not a serialized ORM object
  (backend-engineering §8-§9).
- **Only PUBLISHED tasks are publicly readable.** ``get_published_task``
  and ``list_published_tasks`` filter on status = PUBLISHED: DRAFT is
  invisible (spec §6.2) and a PAUSED/CLOSED/ARCHIVED id reads as the same
  ``TaskNotFoundError`` the unknown-id path raises — the student surface
  answers "not visible", never "why". Claim history on a closed task
  stays reachable through ``list_own_claims``.
- **The claim view is owner-scoped.** ``ClaimView`` is the only DTO that
  may carry an Assignment's platform/keyword (spec §42), and every method
  returning it takes the owner's user id from its caller (the
  authenticated actor) — never from request input.
- **Read authorization (statistics).** Owner, Admin, or a collaborator
  holding VIEW_TASK; any other Teacher (and Students) get
  ``PERMISSION_DENIED``. The owner/Admin check runs before any query
  besides the task lookup.
- **Completion rate** = COMPLETED / (AVAILABLE + OCCUPIED + COMPLETED)
  assignments. RETIRED units are excluded from the denominator: the
  owner withdrew them from the pool, so they were never completable
  work; an empty pool reports 0.0, never a division by zero.
- **Active claims** are the shared ``ACTIVE_CLAIM_STATUSES`` tuple from
  models.py (CLAIMED/VALIDATING/UNDER_REVIEW/REVISION_REQUIRED) — the
  same definition that builds the partial unique indexes, so
  "active" can never drift between uniqueness and statistics.
- **Offset pagination** is the documented V1 choice for both public
  lists (tasks, own claims): simple to reason about, stable enough at
  V1 volumes, and the route layer owns the limit/offset bounds.
- **Submission counts are a future seam.** The submissions table does
  not exist yet; the field ships empty and NO submission query is
  invented here. The submission module fills it against its own status
  axes.
- **Rating summary** comes from the cross-module ``RatingSummaryPort``
  (interfaces.md: this module ships a null port; the community module
  supplies the concrete adapter backed by TaskRating). The port sketch in
  interfaces.md reads ``summary(task_id) -> RatingSummary | None``; the
  Protocol below keeps that name and shape but is ``async`` because the
  real adapter reads PostgreSQL — the adapter receives its session at
  construction, like every per-request-scoped port. ``summary`` is
  None-safe: a task nobody rated reports ``rating=None``.
- **No Clock.** Reads are as-of-now snapshots with no time-dependent
  business rule, so no clock is injected
  (revisit only if a query ever needs business time).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.events import Actor
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.enums import AssignmentAvailability, TaskStatus
from app.modules.tasks.models import (
    ACTIVE_CLAIM_STATUSES,
    Assignment,
    AssignmentClaim,
    Task,
    TaskCollaborator,
)
from app.modules.tasks.schemas import (
    ClaimView,
    PublishedTaskDetail,
    RatingSummary,
    TaskCard,
)
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "ClaimView",
    "NullRatingSummaryPort",
    "PublishedTaskDetail",
    "RatingSummary",
    "RatingSummaryPort",
    "TaskCard",
    "TaskQueryService",
    "TaskStatistics",
    "TeacherTaskListItem",
]

_STATISTICS_DENIED_MESSAGE = (
    "只有任务所有者、拥有 VIEW_TASK 权限的协作者或管理员可以查看任务统计"
)
_TASK_READ_DENIED_MESSAGE = "只有任务所有者、协作者或管理员可以查看该任务"


# --- cross-module rating port (the community module supplies the adapter) -------


class RatingSummaryPort(Protocol):
    """Read-side rating port; the community module's adapter is backed by
    TaskRating."""

    async def summary(self, task_id: UUID) -> RatingSummary | None: ...


class NullRatingSummaryPort:
    """The interim stand-in: every task reports "not rated yet".

    Used until the community module wires the TaskRating-backed adapter,
    and by tests that exercise the None path.
    """

    async def summary(self, task_id: UUID) -> RatingSummary | None:
        return None


# --- the read DTO -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskStatistics:
    """Counts-only task aggregate (spec §41; privacy per §40/§42).

    ``submission_counts`` is the documented future seam: empty until
    the submission module lands, and its key set will be defined by
    that module's own status axes (validation / review), not invented
    here.
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


@dataclass(frozen=True, slots=True)
class TeacherTaskListItem:
    """One workbench list row (spec §41 Task list): card-level facts plus
    the lifecycle timestamps for every status — DRAFT included (the
    student surface hides DRAFT; the owner's workbench must show it).
    Contract fields (submission schema, file policy, notification
    config) ride only the detail view."""

    id: UUID
    title: str
    status: str
    task_type: str
    rarity: str
    base_reward_points: int
    deadline_mode: str
    fixed_deadline_at: datetime | None
    duration_minutes: int | None
    published_at: datetime | None
    closed_at: datetime | None
    created_at: datetime

    @classmethod
    def from_domain(cls, task: Task) -> TeacherTaskListItem:
        return cls(
            id=task.id,
            title=task.title,
            status=task.status,
            task_type=task.task_type,
            rarity=task.rarity,
            base_reward_points=task.base_reward_points,
            deadline_mode=task.deadline_mode,
            fixed_deadline_at=task.fixed_deadline_at,
            duration_minutes=task.duration_minutes,
            published_at=task.published_at,
            closed_at=task.closed_at,
            created_at=task.created_at,
        )


# --- the service --------------------------------------------------------------------


class TaskQueryService:
    """Read-side task queries: the student surfaces (cards, published
    detail, own claims) and the Teacher workbench's statistics aggregate
    (spec §41-§42)."""

    # -- student surfaces -------------------------------------------------------

    async def list_published_tasks(
        self,
        db: AsyncSession,
        *,
        rating_port: RatingSummaryPort,
        limit: int,
        offset: int,
    ) -> tuple[list[TaskCard], int]:
        """One offset page of §42 task cards, newest publish first.

        ``limit``/``offset`` arrive already bounded (the route owns the
        caps); the count query and the page query read the same status
        filter, so ``total`` is the whole claimable-ish catalogue, not the
        page. A PUBLISHED-but-past-cutoff FIXED task still lists (its card
        shows the passed deadline; claiming it answers CLAIM_CUTOFF_REACHED
        — the §42 rule that the card never lies by omission).
        """
        total = int(
            await db.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.status == TaskStatus.PUBLISHED.value)
            )
            or 0
        )
        if total == 0 or offset >= total:
            return [], total

        tasks = (
            await db.scalars(
                select(Task)
                .where(Task.status == TaskStatus.PUBLISHED.value)
                .order_by(Task.published_at.desc().nulls_last(), Task.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        counts = await self._available_counts(db, [task.id for task in tasks])
        cards: list[TaskCard] = []
        for task in tasks:
            cards.append(
                TaskCard(
                    id=task.id,
                    title=task.title,
                    rarity=task.rarity,
                    base_reward_points=task.base_reward_points,
                    deadline_mode=task.deadline_mode,
                    fixed_deadline_at=task.fixed_deadline_at,
                    duration_minutes=task.duration_minutes,
                    assignments_available=counts.get(task.id, 0),
                    rating=await rating_port.summary(task.id),
                )
            )
        return cards, total

    async def get_published_task(
        self,
        db: AsyncSession,
        *,
        task_id: UUID,
        viewer_id: UUID,
        rating_port: RatingSummaryPort,
    ) -> PublishedTaskDetail:
        """The public detail of one PUBLISHED task plus the viewer's own
        non-terminal claim on it, if any.

        Any other status (or an unknown id) is the same
        ``TaskNotFoundError``: the public surface distinguishes "visible"
        from "not", nothing finer (spec §6.2 DRAFT 不可见).
        """
        task = await db.scalar(
            select(Task).where(
                Task.id == task_id, Task.status == TaskStatus.PUBLISHED.value
            )
        )
        if task is None:
            raise TaskNotFoundError(task_id)

        counts = await self._available_counts(db, [task.id])
        active_claim = await db.scalar(
            select(AssignmentClaim)
            .where(
                AssignmentClaim.task_id == task_id,
                AssignmentClaim.user_id == viewer_id,
                AssignmentClaim.status.in_(ACTIVE_CLAIM_STATUSES),
            )
            .order_by(AssignmentClaim.claimed_at.desc())
            .limit(1)
        )
        return PublishedTaskDetail(
            id=task.id,
            title=task.title,
            description=task.description,
            task_type=task.task_type,
            rarity=task.rarity,
            base_reward_points=task.base_reward_points,
            deadline_mode=task.deadline_mode,
            fixed_deadline_at=task.fixed_deadline_at,
            duration_minutes=task.duration_minutes,
            published_at=task.published_at,
            assignments_available=counts.get(task.id, 0),
            rating=await rating_port.summary(task.id),
            my_claim=(
                await self.claim_view(db, active_claim)
                if active_claim is not None
                else None
            ),
        )

    async def list_own_claims(
        self, db: AsyncSession, *, user_id: UUID, limit: int, offset: int
    ) -> tuple[list[ClaimView], int]:
        """One offset page of the user's claim history, newest first —
        every status, terminal included (spec §42: 进行中 Claim / 待修改
        Claim on the home page; history below them)."""
        total = int(
            await db.scalar(
                select(func.count())
                .select_from(AssignmentClaim)
                .where(AssignmentClaim.user_id == user_id)
            )
            or 0
        )
        if total == 0 or offset >= total:
            return [], total

        claims = (
            await db.scalars(
                select(AssignmentClaim)
                .where(AssignmentClaim.user_id == user_id)
                .order_by(AssignmentClaim.claimed_at.desc(), AssignmentClaim.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        views = [await self.claim_view(db, claim) for claim in claims]
        return views, total

    async def claim_view(self, db: AsyncSession, claim: AssignmentClaim) -> ClaimView:
        """Serialize one claim for its OWNER: the owner-scoped join that
        fetches the assigned unit's platform/keyword and the task title.

        The caller guarantees ownership (the claim service returned this
        row for the requesting user, or the query filtered on it); the
        join reads exactly one assignment row by primary key.
        """
        platform, keyword, task_title = (
            await db.execute(
                select(Assignment.platform, Assignment.keyword, Task.title)
                .select_from(AssignmentClaim)
                .join(Assignment, Assignment.id == AssignmentClaim.assignment_id)
                .join(Task, Task.id == AssignmentClaim.task_id)
                .where(AssignmentClaim.id == claim.id)
            )
        ).one()
        return ClaimView(
            claim_id=claim.id,
            task_id=claim.task_id,
            task_title=task_title,
            status=claim.status,
            platform=platform,
            keyword=keyword,
            claimed_at=claim.claimed_at,
            deadline_at=claim.deadline_at,
            grace_deadline_at=claim.grace_deadline_at,
            base_reward_points_snapshot=claim.base_reward_points_snapshot,
        )

    @staticmethod
    async def _available_counts(
        db: AsyncSession, task_ids: list[UUID]
    ) -> dict[UUID, int]:
        """AVAILABLE assignment counts for exactly these tasks (one
        grouped query; empty input short-circuits)."""
        if not task_ids:
            return {}
        rows = await db.execute(
            select(Assignment.task_id, func.count())
            .where(
                Assignment.task_id.in_(task_ids),
                Assignment.availability_status
                == AssignmentAvailability.AVAILABLE.value,
            )
            .group_by(Assignment.task_id)
        )
        return {task_id: int(count) for task_id, count in rows}

    # -- teacher surfaces ---------------------------------------------------------

    async def list_teacher_tasks(
        self,
        db: AsyncSession,
        actor: Actor,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[TeacherTaskListItem], int]:
        """One offset page of the actor's workbench tasks: every task they
        own OR collaborate on, every status including DRAFT (spec §41
        Task list), newest first.

        Admin is deliberately NOT special-cased to list every teacher's
        tasks: §41's Admin console has its own surfaces, and the workbench
        list answers "my tasks and the ones I work on". ``limit``/
        ``offset`` arrive already bounded (the route owns the caps).
        """
        collaborated = select(TaskCollaborator.task_id).where(
            TaskCollaborator.teacher_id == actor.user_id
        )
        visibility = or_(
            Task.owner_teacher_id == actor.user_id, Task.id.in_(collaborated)
        )

        total = int(
            await db.scalar(select(func.count()).select_from(Task).where(visibility))
            or 0
        )
        if total == 0 or offset >= total:
            return [], total

        tasks = (
            await db.scalars(
                select(Task)
                .where(visibility)
                .order_by(Task.created_at.desc(), Task.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return [TeacherTaskListItem.from_domain(task) for task in tasks], total

    async def get_teacher_task(
        self, db: AsyncSession, actor: Actor, task_id: UUID
    ) -> Task:
        """One task's full workbench view — every field the owner
        configured, contract fields included — for an authorized reader
        (owner, any collaborator, or Admin).

        Returns the Task row for ``TeacherTaskResponse.from_domain``; the
        DRAFT status is readable here (unlike the student surface).
        """
        task = await db.scalar(select(Task).where(Task.id == task_id))
        if task is None:
            raise TaskNotFoundError(task_id)
        await self._require_teacher_task_read(db, task, actor)
        return task

    @staticmethod
    async def _require_teacher_task_read(
        db: AsyncSession, task: Task, actor: Actor
    ) -> None:
        """Owner, Admin, or ANY collaborator on the task (spec §4.2).

        Deliberately broader than the statistics gate (VIEW_TASK
        specifically): every capability is actionable only through task
        context (MANAGE_ASSIGNMENTS runs import previews against the
        task, REVIEW_SUBMISSIONS will read its claims), so a
        collaborator row of any capability can already act on the task —
        denying them the workbench detail would be incoherent. The
        module's 403-vs-404 posture follows the statistics precedent:
        an existing-but-forbidden task is PERMISSION_DENIED, an unknown
        id is the shared TaskNotFoundError.
        """
        if actor.user_id == task.owner_teacher_id or is_admin(actor.role):
            return
        standing = await db.scalar(
            select(TaskCollaborator.teacher_id).where(
                TaskCollaborator.task_id == task.id,
                TaskCollaborator.teacher_id == actor.user_id,
            )
        )
        if standing is None:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _TASK_READ_DENIED_MESSAGE,
                status_code=403,
            )

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
