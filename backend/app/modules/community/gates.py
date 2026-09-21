# backend/app/modules/community/gates.py
"""Shared community write/visibility gates (spec §21.1, §21.4, §5.7;
extracted in plan 06 task 6).

The comment, vote, and reaction services each grew a private copy of the
same two reads; the report service (task 6) would have been the third
and fourth. They live here now, once:

- ``require_student_writer`` — the community WRITE gate: Student role
  and ACTIVE status judged on the users ROW (role-first, then status),
  never on Actor fields, so direct service callers cannot bypass the
  transport guard (the claim-service precedent; a suspended staff
  account answers PERMISSION_DENIED, capability, before
  ACCOUNT_NOT_ACTIVE). It hands back the nickname because the comment
  create path needs it for the author display; vote/react/report call
  it for the gate alone. Unlocked read — no per-user write invariant
  exists to protect (the row the write inserts IS the invariant).
- ``require_visible_comment`` — the public-surface comment gate: the
  comment exists, is not a tombstone (one ``deleted_at`` guard covers
  soft delete AND the Admin hard hide, which writes the same trio), and
  sits on a PUBLISHED task; anything else is the shared
  invisibility-reads-as-NOT_FOUND ruling (spec §6.2 — the surface
  answers visibility, never the reason). Unlocked reads:
  publish-immediately semantics — a write that commits while the comment
  is concurrently removed simply survives as a row on a tombstone;
  removing it is moderation's business, not the writer's.

The shared typed-error family lives here with them (same classes, same
messages — the move is rehoming, not renaming): comment_service
re-exports the five for backward compatibility, and the whole community
write surface keeps answering with one set of codes.

The identity seam is the claim-service precedent carried over: a typed
Core-level light ``users`` table, NOT the identity ORM model — role and
status for the gate, nickname for the display. If the
``UserDirectory`` port grows those reads, these queries move behind it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.models import Comment
from app.modules.identity.enums import Role, UserStatus
from app.modules.tasks.enums import TaskStatus
from app.modules.tasks.models import Task
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "CommentDeletedError",
    "CommentNotFoundError",
    "CommenterAccountNotActiveError",
    "CommenterNotFoundError",
    "CommenterNotStudentError",
    "require_student_writer",
    "require_visible_comment",
]

# Identity seam (see module docstring): a typed Core-level light table,
# NOT the identity ORM model — role/status for the writer gate, nickname
# for the public author display.
_USERS = table(
    "users",
    column("id", Uuid),
    column("role", String),
    column("status", String),
    column("nickname", String),
)

# --- messages (§29 envelope text) ---------------------------------------------------

_COMMENTER_NOT_FOUND_MESSAGE = "用户不存在"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许执行该操作"
_NOT_STUDENT_MESSAGE = "仅学生账号可发表评论"
_COMMENT_NOT_FOUND_MESSAGE = "评论不存在"
_COMMENT_DELETED_MESSAGE = "该评论已删除"


# --- shared typed exceptions (router-mapped) -----------------------------------------


class CommenterNotFoundError(BusinessError):
    """No users row for the caller's id (the session token outlived the
    account; same shape as the claim service's UserNotFoundError)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _COMMENTER_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"user_id": str(user_id)},
        )


class CommenterNotStudentError(BusinessError):
    """The writer's role is not STUDENT (spec §4.1: community writes are a
    Student surface; staff govern via the moderation surfaces)."""

    def __init__(self, user_id: UUID, role: str) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOT_STUDENT_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id), "role": role},
        )


class CommenterAccountNotActiveError(BusinessError):
    """The writer's account is not ACTIVE (spec §5.7 state gate)."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(
            ErrorCode.ACCOUNT_NOT_ACTIVE,
            _ACCOUNT_NOT_ACTIVE_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id)},
        )


class CommentNotFoundError(BusinessError):
    """``comment_id`` matches no comment on a gated path; aggregate
    semantics identical to ``TaskNotFoundError``."""

    def __init__(self, comment_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            _COMMENT_NOT_FOUND_MESSAGE,
            status_code=404,
            details={"comment_id": str(comment_id)},
        )


class CommentDeletedError(BusinessError):
    """The comment is already soft-deleted (or hard-hidden, which sets the
    trio): edit, self-delete, moderate-delete, and new anchored writes
    (replies, votes, reactions, reports) are single-shot."""

    def __init__(self, comment_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _COMMENT_DELETED_MESSAGE,
            status_code=400,
            details={"comment_id": str(comment_id)},
        )


# --- the gates ------------------------------------------------------------------------


async def require_student_writer(db: AsyncSession, user_id: UUID) -> str:
    """The community write gate (see module docstring): role-first, then
    status, both on the users row. Returns the writer's nickname for the
    comment author display (the column's NOT NULL contract makes it a
    real nickname); vote/react/report callers ignore it."""
    row = (
        await db.execute(
            select(
                _USERS.c.role,
                _USERS.c.status,
                _USERS.c.nickname,
            ).where(_USERS.c.id == user_id)
        )
    ).first()
    if row is None:
        raise CommenterNotFoundError(user_id)
    role, status, nickname = row
    if role != Role.STUDENT.value:
        raise CommenterNotStudentError(user_id, str(role))
    if status != UserStatus.ACTIVE.value:
        raise CommenterAccountNotActiveError(user_id)
    # Row unpacks arrive as Any; the column is NOT NULL VARCHAR.
    return str(nickname)


async def require_visible_comment(db: AsyncSession, comment_id: UUID) -> Comment:
    """The public-surface comment gate (see module docstring): exists ->
    not a tombstone -> PUBLISHED task. Returns the comment row; callers
    that only need the ruling ignore it."""
    comment = await db.scalar(select(Comment).where(Comment.id == comment_id))
    if comment is None:
        raise CommentNotFoundError(comment_id)
    if comment.deleted_at is not None:
        raise CommentDeletedError(comment_id)
    published = await db.scalar(
        select(Task.id).where(
            Task.id == comment.task_id,
            Task.status == TaskStatus.PUBLISHED.value,
        )
    )
    if published is None:
        raise TaskNotFoundError(comment.task_id)
    return comment
