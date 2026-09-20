# backend/app/modules/community/comment_service.py
"""Comment creation and the public comment list (spec §21, §21.1-§21.2,
§21.4, §40; plan 06 task 2).

Design decisions:

- **Publish-immediately (spec §21.1 无需预审，发布即展示).** ``create_comment``
  performs writer/task/content/parent validation and commits — no
  moderation step, no draft state. The rate limit the spec pairs with
  this is NOT here by plan: task 9 wires the Redis limiter at the router,
  so this service stays pure (no Redis, no clock) and reusable by tests
  and future surfaces.
- **Writer gate: role and status are judged on the users ROW, not on the
  Actor fields.** Community writes are a Student surface (spec §4.1 —
  Teacher/Admin read and govern through the moderation surfaces of tasks
  3 and 8), and an ACTIVE account is required (§5.7). Judging the row
  means direct service callers cannot bypass the transport guard, exactly
  like the claim service. Order is role-first, then status: a suspended
  staff account answers PERMISSION_DENIED (capability), not
  ACCOUNT_NOT_ACTIVE — the claim service's precedent. Unlike claiming
  there is no count-then-insert race to protect, so the read carries no
  FOR UPDATE; a comment committed while the writer's role changes
  concurrently simply exists (no per-user write invariant in V1).
- **Task gate: PUBLISHED-only, and invisibility reads as NOT_FOUND.**
  DRAFT is invisible (spec §6.2) and PAUSED/CLOSED/ARCHIVED are off the
  public task surface, so all of them — and unknown ids — raise the
  shared ``TaskNotFoundError`` (404): the surface answers "not visible",
  never "why". The task row is read without a lock: a comment that
  commits while the task pauses concurrently stays (publish-immediately
  semantics; removing it would be moderation's job, not the writer's).
- **Content rules (spec §21.1).** One pure function,
  ``normalize_comment_content``: strip dangerous control characters,
  trim, reject whitespace-only, enforce the configurable length cap.
  Rulings, documented once here:
  * Control characters are Unicode category Cc EXCEPT ``\\n`` and
    ``\\t`` — newlines keep multi-line comments expressible and tabs are
    ordinary text, while NUL/CR/ESC/DEL (terminal and log-forging
    material) disappear. ``\\r`` removal also normalizes CRLF to LF.
  * The cap is measured on the NORMALIZED content, in code points, and
    the boundary is inclusive (2000 exactly passes). The default 2000 is
    spec-fixed; deployments override it through
    ``Settings.comment_max_length``, injected here as a scalar at the
    composition root (the AbandonService ``daily_abandon_limit`` pattern)
    so the service never imports configuration machinery.
  * XSS: nothing is escaped or stripped as markup. Content is stored
    literally as plain text and serialized as JSON text; this module
    never produces HTML (spec §33.1 所有用户文本按纯文本处理).
- **Parent validation (spec §21.2).** A provided ``parent_id`` must
  exist, belong to the SAME task, and not be soft-deleted. The
  cross-task check is the V1 cycle guard: a create-only parent pointer
  cannot reference the new row (its id does not exist yet), so a cycle
  cannot form at create; edit APIs must NEVER allow changing
  ``parent_id`` (task 3's contract) or that guarantee breaks. Ruling on
  tombstones: a soft-deleted parent is not a discussion anchor —
  replying under 已删除/彻底隐藏 comments is refused with a typed error,
  while already-existing children survive (spec §21.3, shown by task 3).
- **The public list.** Newest first (``created_at DESC``) with
  ``id ASC`` as the tiebreak (the repo's pagination convention — UUIDs
  carry no order, the tiebreak only makes pages deterministic), offset
  pagination with the non-deleted total, and the ``edited`` flag computed
  from CommentRevision existence in the same query (spec §21.3: an edit
  appends a revision row, so the flag has one source of truth). V1
  ruling: soft-deleted comments are EXCLUDED from the public list; task
  3 revisits tombstone rendering — the DTO already carries ``deleted``
  for that decision.
- **Identity seam.** Nickname (and the writer's role/status) are read
  through a typed Core-level light ``users`` table, the claim service's
  precedent: interfaces.md's ``UserDirectory`` port carries no nickname
  and no status+role-by-id read, and identity ORM models stay behind the
  module seam. If the port grows those reads, these queries move behind
  it.
- **No Clock.** ``created_at``/``updated_at`` are database ``now()``
  server defaults and no rule on this surface depends on business time
  (the query_service precedent); revisit with the first time-dependent
  rule (e.g. hot ranking, task 9).

Transaction shape per backend-engineering §5: the row reads, the
invariants, one insert, and exactly one ``commit`` at the end of
``create_comment``; ``list_comments`` is read-only.
"""

from __future__ import annotations

import unicodedata
from uuid import UUID

from sqlalchemy import String, Uuid, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.models import Comment, CommentRevision
from app.modules.community.schemas import CommentPublic, CreateComment
from app.modules.community.serializers import serialize_public_comment
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.tasks.enums import TaskStatus
from app.modules.tasks.models import Task
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "DEFAULT_COMMENT_MAX_LENGTH",
    "CommentService",
    "CommenterAccountNotActiveError",
    "CommenterNotFoundError",
    "CommenterNotStudentError",
    "ParentCommentCrossTaskError",
    "ParentCommentDeletedError",
    "ParentCommentNotFoundError",
    "normalize_comment_content",
]

# spec §21.1: 单条长度默认上限 2000 个字符，可配置. This is the service-side
# copy the composition root overrides from ``Settings.comment_max_length``
# (same field/constant parity as AbandonService's daily_abandon_limit).
DEFAULT_COMMENT_MAX_LENGTH = 2000

# Control characters that survive normalization (see module docstring).
_KEPT_CONTROL_CHARACTERS = frozenset({"\n", "\t"})

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

_EMPTY_CONTENT_MESSAGE = "评论内容不能为空"
_CONTENT_TOO_LONG_MESSAGE = "评论内容超过长度上限"
_COMMENTER_NOT_FOUND_MESSAGE = "用户不存在"
_ACCOUNT_NOT_ACTIVE_MESSAGE = "账号当前状态不允许执行该操作"
_NOT_STUDENT_MESSAGE = "仅学生账号可发表评论"
_PARENT_NOT_FOUND_MESSAGE = "回复的评论不存在"
_PARENT_CROSS_TASK_MESSAGE = "只能回复同一任务下的评论"
_PARENT_DELETED_MESSAGE = "该评论已删除，不能回复"


# --- typed exceptions (router-mapped) ------------------------------------------------


class CommenterNotFoundError(BusinessError):
    """No users row for the actor's id (the session token outlived the
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


class ParentCommentNotFoundError(BusinessError):
    """``parent_id`` matches no comment (spec §21.2 回复不存在; the database
    self-FK is the backstop, this is the friendly error)."""

    def __init__(self, parent_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _PARENT_NOT_FOUND_MESSAGE,
            status_code=400,
            details={"parent_id": str(parent_id)},
        )


class ParentCommentCrossTaskError(BusinessError):
    """The parent belongs to a different Task (spec §21.2 MUST; also the
    V1 cycle guard — see the module docstring)."""

    def __init__(self, parent_id: UUID, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _PARENT_CROSS_TASK_MESSAGE,
            status_code=400,
            details={"parent_id": str(parent_id), "task_id": str(task_id)},
        )


class ParentCommentDeletedError(BusinessError):
    """The parent is a soft-deleted tombstone: not a discussion anchor
    (spec §21.2/§21.3; existing children survive, new replies do not)."""

    def __init__(self, parent_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _PARENT_DELETED_MESSAGE,
            status_code=400,
            details={"parent_id": str(parent_id)},
        )


# --- content normalization (spec §21.1) ----------------------------------------------


def _strip_control_characters(content: str) -> str:
    """Drop every Cc character except newline and tab (module docstring
    ruling); everything else, including potential markup, passes through
    as plain text."""
    return "".join(
        character
        for character in content
        if character in _KEPT_CONTROL_CHARACTERS
        or unicodedata.category(character) != "Cc"
    )


def normalize_comment_content(content: str, max_length: int) -> str:
    """Validate and normalize comment content (spec §21.1).

    Order: strip dangerous control characters -> trim -> reject empty ->
    reject over-limit. Returns the stored form; raises ``BusinessError``
    (VALIDATION_ERROR, 400) otherwise. Pure on purpose: unit-testable
    without a database, and the same rule where a future edit path needs
    it (task 3).
    """
    if not isinstance(content, str):
        raise BusinessError(ErrorCode.VALIDATION_ERROR, _EMPTY_CONTENT_MESSAGE, 400)
    normalized = _strip_control_characters(content).strip()
    if not normalized:
        raise BusinessError(ErrorCode.VALIDATION_ERROR, _EMPTY_CONTENT_MESSAGE, 400)
    if len(normalized) > max_length:
        raise BusinessError(
            ErrorCode.VALIDATION_ERROR,
            _CONTENT_TOO_LONG_MESSAGE,
            400,
            details={"max_length": max_length},
        )
    return normalized


# --- the service ----------------------------------------------------------------------


class CommentService:
    """Comment write and public-read use cases (spec §21).

    ``comment_max_length`` is injectable for tests; production wires
    ``Settings.comment_max_length`` at the composition root. Task 9 adds
    the router (and its rate limiter) on top; nothing here changes for
    that.
    """

    def __init__(self, *, comment_max_length: int = DEFAULT_COMMENT_MAX_LENGTH) -> None:
        self._comment_max_length = comment_max_length

    async def create_comment(
        self, db: AsyncSession, actor: Actor, command: CreateComment
    ) -> CommentPublic:
        """Publish one comment immediately (spec §21.1) and return its
        public DTO — the write path hands back the privacy-safe shape, so
        the stored identity is unreachable without going to the row."""
        commenter = await self._require_student_writer(db, actor)
        content = normalize_comment_content(command.content, self._comment_max_length)
        await self._require_visible_task(db, command.task_id)
        if command.parent_id is not None:
            await self._validate_parent(db, command.task_id, command.parent_id)

        comment = Comment(
            task_id=command.task_id,
            # The real identity ALWAYS lands in the row (spec §21.4):
            # anonymity is a display attribute only.
            user_id=actor.user_id,
            parent_id=command.parent_id,
            content=content,
            is_anonymous=command.is_anonymous,
        )
        db.add(comment)
        await db.commit()
        await db.refresh(comment)  # load the now() server defaults
        return serialize_public_comment(
            comment, author_nickname=commenter, edited=False
        )

    async def list_comments(
        self, db: AsyncSession, task_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[CommentPublic], int]:
        """One offset page of a visible task's public comments, newest
        first, soft-deleted excluded (task 2 ruling; task 3 revisits
        tombstones), with the non-deleted total.

        ``limit``/``offset`` arrive already bounded (the route owns the
        caps, task 9).
        """
        await self._require_visible_task(db, task_id)

        total = int(
            await db.scalar(
                select(func.count())
                .select_from(Comment)
                .where(Comment.task_id == task_id, Comment.deleted_at.is_(None))
            )
            or 0
        )
        if total == 0 or offset >= total:
            return [], total

        edited_flag = (
            select(CommentRevision.comment_id)
            .where(CommentRevision.comment_id == Comment.id)
            .exists()
        )
        rows = (
            await db.execute(
                select(Comment, _USERS.c.nickname, edited_flag)
                .select_from(Comment)
                .join(_USERS, _USERS.c.id == Comment.user_id)
                .where(Comment.task_id == task_id, Comment.deleted_at.is_(None))
                .order_by(Comment.created_at.desc(), Comment.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return [
            serialize_public_comment(comment, author_nickname=nickname, edited=edited)
            for comment, nickname, edited in rows
        ], total

    # -- internals ----------------------------------------------------------------

    @staticmethod
    async def _require_visible_task(db: AsyncSession, task_id: UUID) -> Task:
        """The task must be PUBLISHED; anything else is the shared
        NOT_FOUND (spec §6.2 — the public surface answers visibility,
        not reason)."""
        task = await db.scalar(
            select(Task).where(
                Task.id == task_id, Task.status == TaskStatus.PUBLISHED.value
            )
        )
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    @staticmethod
    async def _require_student_writer(db: AsyncSession, actor: Actor) -> str:
        """Judge role and status on the users row (role-first), and hand
        back the nickname for the author display."""
        row = (
            await db.execute(
                select(
                    _USERS.c.role,
                    _USERS.c.status,
                    _USERS.c.nickname,
                ).where(_USERS.c.id == actor.user_id)
            )
        ).first()
        if row is None:
            raise CommenterNotFoundError(actor.user_id)
        role, status, nickname = row
        if role != Role.STUDENT.value:
            raise CommenterNotStudentError(actor.user_id, str(role))
        if status != UserStatus.ACTIVE.value:
            raise CommenterAccountNotActiveError(actor.user_id)
        # Row unpacks arrive as Any; the column is NOT NULL VARCHAR.
        return str(nickname)

    @staticmethod
    async def _validate_parent(
        db: AsyncSession, task_id: UUID, parent_id: UUID
    ) -> Comment:
        """Spec §21.2 guards: the parent exists, sits in the same task,
        and is not a tombstone. Unlocked read — see the module docstring
        for why no FOR UPDATE and why cycles are impossible at create."""
        parent = await db.scalar(select(Comment).where(Comment.id == parent_id))
        if parent is None:
            raise ParentCommentNotFoundError(parent_id)
        if parent.task_id != task_id:
            raise ParentCommentCrossTaskError(parent_id, task_id)
        if parent.deleted_at is not None:
            raise ParentCommentDeletedError(parent_id)
        return parent
