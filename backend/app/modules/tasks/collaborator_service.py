# backend/app/modules/tasks/collaborator_service.py
"""Task collaborator use cases: grant and remove capability sets
(spec §4.2 "管理自己 Task 的协作者"; plan 03 task 3).

Pinned permission model (the brief's rules, made explicit):

- **Who may grant.** The task owner and Admin (spec §4.3: 全部 Task
  管理) may add any Teacher with any non-empty subset of the four frozen
  capabilities. An existing collaborator may grant too, but never a
  capability outside their own set — "A collaborator cannot grant
  permissions beyond those they possess" — the owner is exempt because
  ownership implicitly carries every capability. Anyone else (unrelated
  Teacher, Student) modifying collaborators is ``PERMISSION_DENIED``.
- **Who may remove.** Owner/Admin only. The spec is silent on
  collaborator-initiated removal (including a collaborator removing
  themselves); plan 03 resolves it to owner/Admin — reopening that later
  means changing one check here, not the persistence shape.
- **Who may be granted.** Target must be a Teacher, verified through the
  identity ``UserDirectory`` port (interfaces.md: modules outside
  identity read account facts ONLY through the port, never identity ORM).
  A Student, an Admin, or an unknown account id is rejected with
  ``VALIDATION_ERROR``.
- **Capability set.** The four frozen capabilities (VIEW_TASK /
  MANAGE_ASSIGNMENTS / REVIEW_SUBMISSIONS / MODERATE_COMMUNITY) are a
  StrEnum here; grants are upper-cased, stripped, de-duplicated, and
  stored in canonical definition order. The column CHECK
  (``permissions <@ ARRAY[...]``, non-empty) is the database-boundary
  backstop; parity between the enum and the models' closed set is pinned
  by the integration tests, mirroring the T2 file-type parity tests.

Duplicates: ``(task_id, teacher_id)`` is UNIQUE; the service pre-checks
for a friendly typed conflict and translates the IntegrityError race
into the same error (backend-engineering §6-§7: service checks produce
friendly errors, constraints close races, and only the expected
constraint is converted).

Transaction shape per backend-engineering §5: lock the Task row FOR
UPDATE (one serialization point for every collaborator mutation on a
task), the invariants, one ``flush`` to evaluate constraints, exactly
one ``commit``. No Clock: granting has no time-dependent rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.identity.directory import UserDirectory
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.tasks.models import Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "SUPPORTED_COLLABORATOR_PERMISSIONS",
    "CollaboratorNotFoundError",
    "CollaboratorPermission",
    "DuplicateCollaboratorError",
    "TaskCollaboratorService",
]


# --- the frozen capability set (plan 03 task 3) --------------------------------


class CollaboratorPermission(StrEnum):
    """The four collaborator capabilities, verbatim from the models.py
    CHECK set (spec §4.2; grant semantics live in this service).

    ``value == member name``; the column persists the exact string.
    """

    VIEW_TASK = "VIEW_TASK"
    MANAGE_ASSIGNMENTS = "MANAGE_ASSIGNMENTS"
    REVIEW_SUBMISSIONS = "REVIEW_SUBMISSIONS"
    MODERATE_COMMUNITY = "MODERATE_COMMUNITY"


SUPPORTED_COLLABORATOR_PERMISSIONS: frozenset[str] = frozenset(
    member.value for member in CollaboratorPermission
)

# Canonical storage order (definition order) so equal sets compare equal
# as lists and every row reads deterministically.
_PERMISSION_ORDER: dict[str, int] = {
    member.value: index for index, member in enumerate(CollaboratorPermission)
}


# --- messages (§29 envelope text) -----------------------------------------------

_NO_GRANT_STANDING_MESSAGE = "只有任务所有者、管理员或现有协作者可以添加任务协作者"
_GRANT_BEYOND_OWN_MESSAGE = "协作者不能授予超出自身拥有的权限"
_REMOVE_DENIED_MESSAGE = "只有任务所有者或管理员可以移除任务协作者"
_EMPTY_PERMISSIONS_MESSAGE = "权限集合不能为空"
_UNSUPPORTED_VALUE_MESSAGE = "存在不支持的取值"
_UNKNOWN_TARGET_MESSAGE = "指定的协作者账号不存在"
_NOT_TEACHER_MESSAGE = "协作者必须是教师账号"
_DUPLICATE_MESSAGE = "该教师已是本任务协作者"
_COLLABORATOR_MISSING_MESSAGE = "任务协作者不存在"

_DUPLICATE_CONSTRAINT_NAME = "uq_task_collaborators_task_id_teacher_id"


# --- typed exceptions (router-mapped; T9 seam) ----------------------------------


class DuplicateCollaboratorError(BusinessError):
    """(task_id, teacher_id) already has a TaskCollaborator row.

    The error-code registry has no dedicated collaborator-conflict code
    (unlike identity's ``USERNAME_ALREADY_EXISTS``); this carries
    ``VALIDATION_ERROR`` with HTTP 409 and the T9 router may remap it if
    interfaces.md registers one (doc-first rule) — same posture as
    ``TaskNotFoundError``.
    """

    def __init__(self, task_id: UUID, teacher_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _DUPLICATE_MESSAGE,
            status_code=409,
            details={"task_id": str(task_id), "teacher_id": str(teacher_id)},
        )


class CollaboratorNotFoundError(BusinessError):
    """No TaskCollaborator row for (task_id, teacher_id) on removal."""

    def __init__(self, task_id: UUID, teacher_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _COLLABORATOR_MISSING_MESSAGE,
            status_code=404,
            details={"task_id": str(task_id), "teacher_id": str(teacher_id)},
        )


# --- helpers ---------------------------------------------------------------------


def _normalize_permissions(
    values: Sequence[CollaboratorPermission | str],
) -> list[str]:
    """Upper-case, strip, and de-duplicate a grant, then order it
    canonically.

    Unknown capabilities are rejected before the database CHECK can see
    them; the empty set is rejected here as well (models only enforce
    non-emptiness at the boundary, the friendly error belongs to the
    service).
    """
    normalized: list[str] = []
    for raw in values:
        code = str(raw).strip().upper()
        if code and code not in normalized:
            normalized.append(code)
    unsupported = sorted(set(normalized) - SUPPORTED_COLLABORATOR_PERMISSIONS)
    if unsupported:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _UNSUPPORTED_VALUE_MESSAGE,
            status_code=400,
            details={"unsupported": unsupported},
        )
    if not normalized:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR, _EMPTY_PERMISSIONS_MESSAGE, status_code=400
        )
    return sorted(normalized, key=_PERMISSION_ORDER.__getitem__)


async def _locked_task(db: AsyncSession, task_id: UUID) -> Task:
    """The Task row under FOR UPDATE, or the shared TaskNotFoundError."""
    task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise TaskNotFoundError(task_id)
    return task


def _own_permissions(permissions: list[str] | None) -> set[str] | None:
    """A collaborator row's capability set; ``None`` means no standing."""
    return None if permissions is None else set(permissions)


# --- the service ------------------------------------------------------------------


class TaskCollaboratorService:
    """Grant and remove Task collaborator capability sets (spec §4.2).

    ``user_directory`` is the frozen identity port; the concrete adapter
    is injected by the composition root (T9), tests pass the real
    PostgreSQL-backed adapter or a stub.
    """

    def __init__(self, *, user_directory: UserDirectory) -> None:
        self._directory = user_directory

    async def add_collaborator(
        self,
        db: AsyncSession,
        actor: Actor,
        task_id: UUID,
        teacher_id: UUID,
        permissions: Sequence[CollaboratorPermission | str],
    ) -> TaskCollaborator:
        """Grant ``permissions`` on ``task_id`` to the Teacher
        ``teacher_id`` and return the persisted row.

        Order of checks: standing (who may grant at all) -> permission
        set validity -> grant-within-own-set -> target is a Teacher ->
        duplicate -> insert. Authorization precedes validation so an
        unauthorized caller learns nothing about the request's shape.
        """
        task = await _locked_task(db, task_id)

        grant_ceiling: set[str] | None
        if actor.user_id == task.owner_teacher_id or is_admin(actor.role):
            grant_ceiling = None  # owner/Admin: every capability (exempt)
        else:
            grant_ceiling = _own_permissions(
                await db.scalar(
                    select(TaskCollaborator.permissions).where(
                        TaskCollaborator.task_id == task_id,
                        TaskCollaborator.teacher_id == actor.user_id,
                    )
                )
            )
            if grant_ceiling is None:
                raise BusinessError(
                    ErrorCode.PERMISSION_DENIED,
                    _NO_GRANT_STANDING_MESSAGE,
                    status_code=403,
                )

        granted = _normalize_permissions(permissions)
        if grant_ceiling is not None and not set(granted) <= grant_ceiling:
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _GRANT_BEYOND_OWN_MESSAGE,
                status_code=403,
                details={"granted": granted, "grantor": sorted(grant_ceiling)},
            )

        target_role = await self._directory.get_role(db, teacher_id)
        if target_role is None:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _UNKNOWN_TARGET_MESSAGE,
                status_code=400,
                details={"teacher_id": str(teacher_id)},
            )
        if target_role is not Role.TEACHER:
            raise BusinessError(
                ErrorCode.VALIDATION_ERROR,
                _NOT_TEACHER_MESSAGE,
                status_code=400,
                details={"teacher_id": str(teacher_id), "role": target_role.value},
            )

        existing = await db.scalar(
            select(TaskCollaborator).where(
                TaskCollaborator.task_id == task_id,
                TaskCollaborator.teacher_id == teacher_id,
            )
        )
        if existing is not None:
            raise DuplicateCollaboratorError(task_id, teacher_id)

        row = TaskCollaborator(
            task_id=task_id, teacher_id=teacher_id, permissions=granted
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError as exc:
            # Only the duplicate constraint is expected here (the task
            # lock serializes mutations; the CHECKs were pre-validated);
            # anything else propagates as an unknown database failure.
            if _DUPLICATE_CONSTRAINT_NAME in str(exc):
                raise DuplicateCollaboratorError(task_id, teacher_id) from exc
            raise
        await db.commit()
        return row

    async def remove_collaborator(
        self, db: AsyncSession, actor: Actor, task_id: UUID, teacher_id: UUID
    ) -> None:
        """Remove the (task_id, teacher_id) collaborator row.

        Owner/Admin only — see the module docstring for the spec-silent
        self-removal decision. Removing an absent row is the typed
        ``CollaboratorNotFoundError`` (NOT_FOUND), matching the aggregate
        semantics of ``TaskNotFoundError``.
        """
        task = await _locked_task(db, task_id)
        if actor.user_id != task.owner_teacher_id and not is_admin(actor.role):
            raise BusinessError(
                ErrorCode.PERMISSION_DENIED,
                _REMOVE_DENIED_MESSAGE,
                status_code=403,
            )

        row = await db.scalar(
            select(TaskCollaborator).where(
                TaskCollaborator.task_id == task_id,
                TaskCollaborator.teacher_id == teacher_id,
            )
        )
        if row is None:
            raise CollaboratorNotFoundError(task_id, teacher_id)

        await db.delete(row)
        await db.commit()
