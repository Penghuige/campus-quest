# backend/app/modules/community/gates.py
"""Shared community write/visibility gates (spec §21.1, §21.4, §5.7;
extracted in plan 06 task 6).

The comment, vote, and reaction services each grew a private copy of the
same two reads; the report service (task 6) would have been the third
and fourth. They live here now, once:

- ``require_community_writer`` — the community WRITE gate: Student or
  Teacher role and ACTIVE status judged on the users ROW (role-first,
  then status), never on Actor fields, so direct service callers
  cannot bypass the transport guard (the claim-service precedent; a
  suspended account answers PERMISSION_DENIED, capability, before
  ACCOUNT_NOT_ACTIVE). Spec §4.2 opens with "除普通社区能力外，可："
  — Teacher holds §4.1's ordinary community capabilities, so the
  participant family is Student + Teacher (the PR #2 hardening
  ruling); Admin is deliberately NOT a participant (Admin's community
  powers are the governance surfaces: moderation queue, hard hide,
  identity reveal). It hands back the nickname because the comment
  create path needs it for the author display; vote/react/report call
  it for the gate alone. Unlocked read — no per-user write invariant
  exists to protect (the row the write inserts IS the invariant).
- ``require_student_writer`` — the rating gate (spec §20): Student
  role only, same row-judged shape. Rating eligibility is the
  completed-claim predicate and only Students can hold a claim (spec
  §4.1), so Student-only here is §20's gate restated — the deliberate
  exception to the participant family above.
- ``require_visible_comment`` — the public-surface comment gate: the
  comment exists, is not a tombstone (one ``deleted_at`` guard covers
  soft delete AND the Admin hard hide, which writes the same trio), and
  sits on a PUBLISHED task; anything else is the shared
  invisibility-reads-as-NOT_FOUND ruling (spec §6.2 — the surface
  answers visibility, never the reason). Unlocked reads:
  publish-immediately semantics — a write that commits while the comment
  is concurrently removed simply survives as a row on a tombstone;
  removing it is moderation's business, not the writer's.
- ``require_task_moderation_site`` — the moderation-site standing gate
  (final-review fix I1): the owner/MODERATE_COMMUNITY predicate that
  three surfaces grew privately (comment_service's moderate-delete,
  report_service's report queue, the router's moderation listing).
  Standing: the task's owner Teacher or a collaborator holding
  MODERATE_COMMUNITY (spec §4.2/§21.4/§23). The two documented
  policies differ only on Admin, and ``admit_admin`` selects between
  them: the destructive moderate-delete REFUSES Admin (the T3 ruling —
  Admin's community-removal power is the separately audited hard
  hide), the two read surfaces ADMIT Admin (review hides nothing and
  duplicates nothing). ``error_factory`` keeps each surface's own
  typed denial (CommentModerationDeniedError / ReportViewDeniedError)
  so the §29 envelopes stay put. Check order travels with the policy
  and is load-bearing: the write variant denies non-Teachers BEFORE
  the task read (authorization before validation — an unauthorized
  caller learns nothing about the task), the read variants answer
  existence first (unknown task -> the shared 404 for anyone) before
  the Admin admission. Like every moderation path, NOT gated on
  PUBLISHED — governance reaches paused/closed history.

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

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import has_any_role, is_admin
from app.modules.community.models import Comment
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.tasks.collaborator_service import CollaboratorPermission
from app.modules.tasks.enums import TaskStatus
from app.modules.tasks.models import Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "CommentDeletedError",
    "CommentNotFoundError",
    "CommenterAccountNotActiveError",
    "CommenterNotFoundError",
    "CommenterNotParticipantError",
    "CommenterNotStudentError",
    "require_community_writer",
    "require_student_writer",
    "require_task_moderation_site",
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
_NOT_PARTICIPANT_MESSAGE = "仅学生或教师账号可参与社区互动"
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


class CommenterNotParticipantError(BusinessError):
    """The writer's role is outside the participant family (spec §4.1/
    §4.2: ordinary community capabilities belong to Student + Teacher;
    the PR #2 hardening ruling keeps Admin on the governance surfaces
    only)."""

    def __init__(self, user_id: UUID, role: str) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOT_PARTICIPANT_MESSAGE,
            status_code=403,
            details={"user_id": str(user_id), "role": role},
        )


class CommenterNotStudentError(BusinessError):
    """The rater's role is not STUDENT (spec §20: rating eligibility is
    the completed-claim predicate, and only Students hold claims — see
    ``rating_service``)."""

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


async def require_community_writer(db: AsyncSession, user_id: UUID) -> str:
    """The community write gate (see module docstring): the participant
    family is Student + Teacher (spec §4.2's "普通社区能力"), role-first,
    then status, both on the users row. Admin is refused — Admin's
    community powers are the governance surfaces. Returns the writer's
    nickname for the comment author display (the column's NOT NULL
    contract makes it a real nickname); vote/react/report callers
    ignore it. A Teacher participant goes through EXACTLY the same
    anonymous/named display semantics as a Student (the author display
    is role-blind by construction)."""
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
    if not has_any_role(str(role), Role.STUDENT, Role.TEACHER):
        raise CommenterNotParticipantError(user_id, str(role))
    if status != UserStatus.ACTIVE.value:
        raise CommenterAccountNotActiveError(user_id)
    # Row unpacks arrive as Any; the column is NOT NULL VARCHAR.
    return str(nickname)


async def require_student_writer(db: AsyncSession, user_id: UUID) -> str:
    """The rating gate (spec §20; see module docstring): Student-only,
    same row-judged shape as the community gate above. Kept as its own
    predicate — rating eligibility is the completed-claim predicate,
    which only Students can satisfy (spec §4.1), so admitting Teachers
    here would change nothing while blurring the §20 gate."""
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


async def require_task_moderation_site(
    db: AsyncSession,
    actor: Actor,
    task_id: UUID,
    *,
    admit_admin: bool,
    error_factory: Callable[[UUID], BusinessError],
) -> None:
    """The moderation-site standing gate (see module docstring): the
    task's owner Teacher or a collaborator holding MODERATE_COMMUNITY —
    one predicate behind the three surfaces that grew it privately
    (comment_service's moderate-delete, report_service's report queue,
    the router's moderation listing; final-review fix I1).

    ``admit_admin`` selects the documented policy, and with it the
    check ORDER, which is load-bearing:

    - ``False`` (moderate-delete): Admin is refused — the T3 ruling
      keeps Admin's community-removal power on the separately audited
      hard-hide path — and a non-Teacher is denied BEFORE the task
      read (the collaborator-service precedent: authorization before
      validation, so an unauthorized caller learns nothing about the
      task).
    - ``True`` (the report queue and the moderation listing): Admin is
      admitted — review hides nothing and duplicates nothing — and
      existence answers first: an unknown task is the shared
      ``TaskNotFoundError`` (404) for any caller before standing is
      judged.

    ``error_factory`` builds the caller's typed denial (same envelope
    the private copy raised: ``CommentModerationDeniedError`` on the
    two comment surfaces, ``ReportViewDeniedError`` on the report
    queue), so each surface keeps its §29 codes and details unchanged.

    Standing is judged on the Actor's server-resolved role (never
    client-supplied). Like every moderation path this is deliberately
    NOT gated on PUBLISHED — governance reaches paused/closed history
    too.
    """
    if not admit_admin and actor.role is not Role.TEACHER:
        # Write-variant order: the denial precedes the task read.
        raise error_factory(task_id)
    owner_id = await db.scalar(select(Task.owner_teacher_id).where(Task.id == task_id))
    if owner_id is None:
        raise TaskNotFoundError(task_id)
    if admit_admin and is_admin(actor.role):
        return
    if actor.role is not Role.TEACHER:
        # Read-variant order: existence has already answered; the
        # non-Teacher (Admin included when unadmitted) is denied now.
        raise error_factory(task_id)
    if owner_id == actor.user_id:
        return
    permissions = await db.scalar(
        select(TaskCollaborator.permissions).where(
            TaskCollaborator.task_id == task_id,
            TaskCollaborator.teacher_id == actor.user_id,
        )
    )
    if permissions is None or CollaboratorPermission.MODERATE_COMMUNITY not in (
        permissions or []
    ):
        raise error_factory(task_id)
