# backend/app/modules/community/comment_service.py
"""Comment creation, the public comment list, edit history, soft delete,
and the moderation boundaries (spec §21, §21.1-§21.4, §40; plan 06 tasks
2-3).

Design decisions:

- **Publish-immediately (spec §21.1 无需预审，发布即展示).** ``create_comment``
  performs writer/task/content/parent validation and commits — no
  moderation step, no draft state. The rate limit the spec pairs with
  this is NOT here by plan: task 9 wires the Redis limiter at the router,
  so this service stays pure (no Redis) and reusable by tests and future
  surfaces.
- **Writer gate: the shared community gate (gates.py, task 6).**
  Community writes are a Student surface (spec §4.1 — Teacher/Admin read
  and govern through the moderation surfaces of tasks 3 and 8), and an
  ACTIVE account is required (§5.7). Role and status are judged on the
  users ROW, not on the Actor fields, so direct service callers cannot
  bypass the transport guard, exactly like the claim service. Order is
  role-first, then status: a suspended staff account answers
  PERMISSION_DENIED (capability), not ACCOUNT_NOT_ACTIVE — the claim
  service's precedent. Unlike claiming there is no count-then-insert race
  to protect, so the read carries no FOR UPDATE; a comment committed
  while the writer's role changes concurrently simply exists (no per-user
  write invariant in V1). The gate and its typed errors live in
  ``gates.require_student_writer`` since task 6 (vote, reaction, and
  report share them); this module re-exports the errors so existing
  importers see one set of codes either way.
- **Task gate: PUBLISHED-only, and invisibility reads as NOT_FOUND.**
  DRAFT is invisible (spec §6.2) and PAUSED/CLOSED/ARCHIVED are off the
  public task surface, so all of them — and unknown ids — raise the
  shared ``TaskNotFoundError`` (404): the surface answers "not visible",
  never "why". The task row is read without a lock: a comment that
  commits while the task pauses concurrently stays (publish-immediately
  semantics; removing it would be moderation's job, not the writer's).
  ``_require_visible_task`` guards create/list; the moderation paths
  (task 3) intentionally do NOT — a Teacher moderating their task's
  comments is governance, not public browsing, and must work on a
  PAUSED/CLOSED task's history too.
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
  ``parent_id`` — ``edit_comment`` takes no parent argument at all, which
  IS that contract — or that guarantee breaks. Ruling on tombstones: a
  soft-deleted parent is not a discussion anchor — replying under
  已删除/彻底隐藏 comments is refused with a typed error (the hard hide
  sets the soft-delete trio, so one guard covers both), while
  already-existing children survive (spec §21.3).
- **Edit history (spec §21.3 编辑).** ``edit_comment`` is owner-only
  (§21.2 越权修改别人评论 MUST), revalidates content through the same
  normalizer/cap as creation, and appends ONE ``CommentRevision`` row
  holding the PREVIOUS version plus ``edited_at`` — the comment row
  itself stays the latest version, so 普通用户只看到最新版 remains a plain
  row read and history walks backwards through the revisions. The
  ``updated_at`` bump is set explicitly from the injected Clock (the
  column's ``onupdate`` stays as the backstop for direct row mutations):
  edit timestamps become deterministic and observable rather than
  database-transaction-pinned. Ruling: the edit/delete authority is
  OWNERSHIP (§21.3 用户可编辑/删除自己的评论) — the Student/ACTIVE writer
  gate is the publish-surface rule for NEW thread content, not a
  re-authorization every owner action repeats; a suspended owner's
  session is the transport layer's problem (task 9).
- **Soft delete and tombstones (spec §21.3 删除).** ``delete_own_comment``
  is owner-only and writes the full trio; with no reason supplied the
  sentinel code ``owner`` lands in ``delete_reason`` (a blank reason is
  treated as absent). The public list renders a deleted PARENT as a
  tombstone — ``deleted: true``, null content, uniform 该评论已删除
  display (identity gone with the content) — exactly when it still
  anchors SURVIVING children; deleted leaves vanish, and a deleted
  comment whose children all vanished vanishes with them (a tombstone
  exists for the children, not for itself). That is a greatest-fixpoint
  over the thread, computed in Python after one full-thread fetch:
  recursive SQL cannot express the NOT EXISTS shape, thread depth is
  unbounded, and per-task thread size makes the fetch cheap in V1 —
  revisit with the hot-ranking work (task 9) if profiling disagrees.
- **Teacher moderation boundary (spec §21.4 治理).**
  ``moderate_delete_comment`` is a TEACHER-surface: the task's owner or
  a collaborator holding ``MODERATE_COMMUNITY`` (spec §4.2), nobody
  else — not a student (not even the author, whose path is the owner
  delete), not an unrelated teacher, not a collaborator without the
  capability. Admin is refused HERE by design: the Admin community-
  removal tool is the audited hard hide below, keeping the two powers on
  separate, separately-audited paths. The reason is MANDATORY (blank is
  rejected) and the deletion emits an audit-grade ``DomainEvent``
  through the publisher port before commit (the audit/outbox module
  later swaps the adapter for the persistent AuditLog write, spec §21.3
  AuditLog 仍保留操作记录).
- **Admin hard hide (spec §21.3 彻底隐藏).**
  ``admin_hard_hide_subtree`` is Admin-only with a mandatory reason. It
  flags every comment of the target's subtree ``is_hard_hidden`` AND
  writes the soft-delete trio with the distinct reason code
  ``ADMIN_HARD_HIDE: <reason>`` (the human reason rides the audit event
  verbatim; the row keeps the machine-scannable prefix). Visibility
  only: rows, content, ids, and relations are preserved — the public
  list renders hard-hidden comments NOTHING (no tombstone, unlike soft
  delete) while the moderation DTO (task 8 shape) still sees the row and
  its ``hard_hidden`` flag. Hard hide is the STRONGER removal: it may
  escalate an already-soft-deleted tombstone (the point is disappearing
  the surviving descendants), so — unlike the two delete paths — it does
  not reject an already-deleted target. Subtree collection is a BFS over
  ``parent_id`` batches; descendants are read without locks (V1 ruling:
  a reply racing the BFS and committing after the parent's trio lands is
  refused by the tombstone parent guard anyway; re-running the hide
  closes the gap).
- **Clock and event seams (task 3).** The constructor takes ``clock``
  and ``events`` optionally; None means the interim defaults
  (``SystemClock`` / ``LoggingEventPublisher`` — the identity-module
  pattern while the audit outbox is pending). Creation/listing still
  need neither: ``created_at`` stays a database ``now()`` server default
  and no rule on those surfaces reads business time. Edit/delete/
  moderation timestamps and audit ``occurred_at`` come from the Clock.
- **Identity seam.** Nickname reads go through a typed Core-level light
  ``users`` table, the claim service's precedent: interfaces.md's
  ``UserDirectory`` port carries no nickname read, and identity ORM
  models stay behind the module seam. Role/status moved to
  ``gates.require_student_writer`` with the task-6 extraction; this
  module keeps the nickname read for the author display. If the port
  grows those reads, these queries move behind it.

Transaction shape per backend-engineering §5: the row reads (FOR UPDATE
on the target comment — one serialization point for concurrent
edit/delete/moderate on the same row), the invariants, the writes, and
exactly one ``commit`` per command; ``list_comments`` is read-only.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import cast
from uuid import UUID

from sqlalchemy import String, Uuid, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin
from app.modules.community.gates import (
    CommentDeletedError,
    CommenterAccountNotActiveError,
    CommenterNotFoundError,
    CommenterNotStudentError,
    CommentNotFoundError,
    require_student_writer,
    require_task_moderation_site,
)
from app.modules.community.models import Comment, CommentRevision
from app.modules.community.schemas import CommentPublic, CreateComment
from app.modules.community.serializers import (
    serialize_public_comment,
    serialize_tombstone_comment,
)
from app.modules.identity.events import (
    Actor,
    DomainEvent,
    DomainEventPublisher,
    LoggingEventPublisher,
)
from app.modules.tasks.enums import TaskStatus
from app.modules.tasks.models import Task
from app.modules.tasks.service import TaskNotFoundError

__all__ = [
    "COMMENT_HARD_HIDDEN",
    "COMMENT_MODERATION_DELETED",
    "DEFAULT_COMMENT_MAX_LENGTH",
    "CommentService",
    "CommentAdminRequiredError",
    "CommentDeletedError",
    "CommentModerationDeniedError",
    "CommentNotFoundError",
    "CommentOwnerRequiredError",
    "CommenterAccountNotActiveError",
    "CommenterNotFoundError",
    "CommenterNotStudentError",
    "HARD_HIDDEN_REASON_CODE",
    "ModerationReasonRequiredError",
    "OWNER_DELETE_REASON",
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

# Owner self-delete without an explicit reason stores this sentinel code
# (spec §21.3 trio is always written together; a blank reason counts as
# no reason).
OWNER_DELETE_REASON = "owner"

# The hard-hide reason code prefix stored on the row (see module
# docstring): distinct from any human reason so queries can scan for
# privacy/legal removals; the verbatim reason rides the audit event.
HARD_HIDDEN_REASON_CODE = "ADMIN_HARD_HIDE"

# Audit-stream action names (the identity-module audit-vs-notification
# distinction): consumed by the audit/outbox module's AuditService.
COMMENT_MODERATION_DELETED = "COMMENT_MODERATION_DELETED"
COMMENT_HARD_HIDDEN = "COMMENT_HARD_HIDDEN"

# Identity seam (see module docstring): a typed Core-level light table,
# NOT the identity ORM model — nickname for the public author display
# (role/status moved to gates.require_student_writer in task 6).
_USERS = table(
    "users",
    column("id", Uuid),
    column("nickname", String),
)

# --- messages (§29 envelope text) ---------------------------------------------------

_EMPTY_CONTENT_MESSAGE = "评论内容不能为空"
_CONTENT_TOO_LONG_MESSAGE = "评论内容超过长度上限"
_PARENT_NOT_FOUND_MESSAGE = "回复的评论不存在"
_PARENT_CROSS_TASK_MESSAGE = "只能回复同一任务下的评论"
_PARENT_DELETED_MESSAGE = "该评论已删除，不能回复"
_NOT_COMMENT_OWNER_MESSAGE = "只能编辑或删除自己的评论"
_MODERATION_REASON_REQUIRED_MESSAGE = "必须提供删除原因"
_MODERATION_DENIED_MESSAGE = "只有任务所有者或拥有社区治理权限的协作者可以删除该评论"
_ADMIN_REQUIRED_MESSAGE = "只有管理员可以彻底隐藏评论"


# --- typed exceptions (router-mapped) ------------------------------------------------


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


class CommentOwnerRequiredError(BusinessError):
    """Spec §21.2 MUST 越权修改别人评论: the actor is not the comment's
    author. Deliberately carries no user ids in ``details`` — the
    anonymous-leakage contract covers error payloads too (task 2)."""

    def __init__(self, comment_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _NOT_COMMENT_OWNER_MESSAGE,
            status_code=403,
            details={"comment_id": str(comment_id)},
        )


class ModerationReasonRequiredError(BusinessError):
    """A moderation delete or hard hide arrived without a usable reason
    (spec §21.3/§21.4: governance actions are reason-mandatory)."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _MODERATION_REASON_REQUIRED_MESSAGE,
            status_code=400,
        )


class CommentModerationDeniedError(BusinessError):
    """The actor is not the task's owner Teacher and not a collaborator
    holding MODERATE_COMMUNITY (spec §4.2/§21.4)."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _MODERATION_DENIED_MESSAGE,
            status_code=403,
            details={"task_id": str(task_id)},
        )


class CommentAdminRequiredError(BusinessError):
    """The hard hide is Admin-only (spec §21.3 隐私/违法场景; Teachers use
    the moderate-delete path)."""

    def __init__(self, comment_id: UUID) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _ADMIN_REQUIRED_MESSAGE,
            status_code=403,
            details={"comment_id": str(comment_id)},
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
    without a database, and the same rule the edit path reuses (task 3).
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


def _require_moderation_reason(reason: str) -> str:
    """Strip the reason; blank is a typed rejection (never a silent
    moderation). No length cap: moderator input is audit material, not
    public thread content — §21.1's caps do not apply."""
    normalized = reason.strip() if isinstance(reason, str) else ""
    if not normalized:
        raise ModerationReasonRequiredError()
    return normalized


# --- the service ----------------------------------------------------------------------


class CommentService:
    """Comment write and public-read use cases (spec §21), plus the edit
    history, soft delete, and moderation boundaries (task 3).

    ``comment_max_length`` is injectable for tests; production wires
    ``Settings.comment_max_length`` at the composition root. ``clock`` and
    ``events`` are optional with interim defaults (SystemClock /
    LoggingEventPublisher) so the create/list surface stays
    dependency-free; tests freeze time with ``FrozenClock`` and assert
    audit events through ``InMemoryEventCollector``. Task 9 adds the
    router (and its rate limiter) on top; nothing here changes for that.
    """

    def __init__(
        self,
        *,
        comment_max_length: int = DEFAULT_COMMENT_MAX_LENGTH,
        clock: Clock | None = None,
        events: DomainEventPublisher | None = None,
    ) -> None:
        self._comment_max_length = comment_max_length
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._events: DomainEventPublisher = (
            events if events is not None else LoggingEventPublisher()
        )

    async def create_comment(
        self, db: AsyncSession, actor: Actor, command: CreateComment
    ) -> CommentPublic:
        """Publish one comment immediately (spec §21.1) and return its
        public DTO — the write path hands back the privacy-safe shape, so
        the stored identity is unreachable without going to the row."""
        commenter = await require_student_writer(db, actor.user_id)
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
        first, with the rendered total.

        Rendering rule (task 3, spec §21.3): live comments render
        normally; a soft-deleted PARENT renders as a tombstone (null
        content, 该评论已删除) exactly while it anchors surviving children;
        deleted leaves — and deleted comments whose entire subtree
        vanished — leave the list; hard-hidden comments never render at
        all. See the module docstring for why the fixpoint runs in Python
        over one full-thread fetch.

        ``limit``/``offset`` arrive already bounded (the route owns the
        caps, task 9).
        """
        await self._require_visible_task(db, task_id)

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
                .where(Comment.task_id == task_id)
                .order_by(Comment.created_at.desc(), Comment.id)
            )
        ).all()

        rendered = self._rendered_comment_ids(
            cast("Sequence[tuple[Comment, str, bool]]", rows)
        )
        page = [
            self._serialize_row(comment, nickname=nickname, edited=edited)
            for comment, nickname, edited in rows
            if comment.id in rendered
        ][offset : offset + limit]
        return page, len(rendered)

    # -- edit history (spec §21.3 编辑) -------------------------------------------

    async def edit_comment(
        self, db: AsyncSession, actor: Actor, comment_id: UUID, content: str
    ) -> CommentPublic:
        """Owner-only edit (spec §21.2/§21.3): revalidate content with the
        creation rules, snapshot the PREVIOUS version into an append-only
        CommentRevision, bump ``updated_at`` from the Clock, and return
        the public DTO with ``edited=True``.

        ``parent_id`` is immutable by construction — this command accepts
        no parent argument, preserving the create-only cycle guard (see
        the module docstring).
        """
        comment = await self._locked_comment(db, comment_id)
        if comment.user_id != actor.user_id:
            raise CommentOwnerRequiredError(comment_id)
        if comment.deleted_at is not None:
            raise CommentDeletedError(comment_id)
        new_content = normalize_comment_content(content, self._comment_max_length)

        now = self._clock.now()
        db.add(
            CommentRevision(
                comment_id=comment.id, content=comment.content, edited_at=now
            )
        )
        comment.content = new_content
        # Explicit clock-owned bump (deterministic, observable); the
        # column's onupdate stays as the backstop elsewhere.
        comment.updated_at = now
        nickname = await self._nickname(db, comment.user_id)
        await db.commit()
        await db.refresh(comment)
        return serialize_public_comment(comment, author_nickname=nickname, edited=True)

    # -- owner soft delete (spec §21.3 删除) ---------------------------------------

    async def delete_own_comment(
        self,
        db: AsyncSession,
        actor: Actor,
        comment_id: UUID,
        *,
        reason: str | None = None,
    ) -> None:
        """Owner-only soft delete: the full trio lands on the row and the
        content survives in storage; children keep their anchor (the
        public list decides tombstone vs vanish, spec §21.3). A missing or
        blank ``reason`` stores the ``owner`` sentinel code."""
        comment = await self._locked_comment(db, comment_id)
        if comment.user_id != actor.user_id:
            raise CommentOwnerRequiredError(comment_id)
        if comment.deleted_at is not None:
            raise CommentDeletedError(comment_id)

        stored_reason = (reason or "").strip() or OWNER_DELETE_REASON
        comment.deleted_at = self._clock.now()
        comment.deleted_by = actor.user_id
        comment.delete_reason = stored_reason
        await db.commit()

    # -- Teacher moderation (spec §21.4 治理) --------------------------------------

    async def moderate_delete_comment(
        self, db: AsyncSession, actor: Actor, comment_id: UUID, reason: str
    ) -> None:
        """Teacher moderation delete: owner-of-the-task or
        MODERATE_COMMUNITY collaborator only, reason mandatory, soft-delete
        trio with the moderator as ``deleted_by``, and one audit-grade
        DomainEvent published inside the transaction.

        Check order (the collaborator-service precedent: authorization
        before validation, so an unauthorized caller learns nothing about
        the request's shape): comment exists -> moderator standing ->
        not already deleted -> reason -> mutate.
        """
        comment = await self._locked_comment(db, comment_id)
        await self._require_task_moderator(db, actor, comment.task_id)
        if comment.deleted_at is not None:
            raise CommentDeletedError(comment_id)
        stored_reason = _require_moderation_reason(reason)

        now = self._clock.now()
        comment.deleted_at = now
        comment.deleted_by = actor.user_id
        comment.delete_reason = stored_reason
        await db.flush()
        self._events.publish(
            DomainEvent(
                event_type=COMMENT_MODERATION_DELETED,
                aggregate_type="Comment",
                aggregate_id=comment.id,
                occurred_at=now,
                payload={
                    "comment_id": str(comment.id),
                    "task_id": str(comment.task_id),
                    "deleted_by": str(actor.user_id),
                    "reason": stored_reason,
                },
            )
        )
        await db.commit()

    # -- Admin hard hide (spec §21.3 彻底隐藏) -------------------------------------

    async def admin_hard_hide_subtree(
        self, db: AsyncSession, actor: Actor, comment_id: UUID, reason: str
    ) -> None:
        """Admin-only hard hide of a whole comment subtree for privacy or
        legal removal: every subtree comment (target included) gets
        ``is_hard_hidden`` plus the soft-delete trio carrying the
        ``ADMIN_HARD_HIDE: <reason>`` code, an audit DomainEvent records
        the verbatim reason, and NOTHING is deleted — rows, content, ids,
        and relations survive for AuditLog-pending moderation review (the
        public list renders the subtree nothing; task 8's moderation query
        still sees it, flagged).

        Unlike the delete paths this may target an ALREADY-deleted comment
        (escalating a tombstone to disappear its surviving descendants),
        so there is no deleted-state guard here — see the module
        docstring.
        """
        if not is_admin(actor.role):
            raise CommentAdminRequiredError(comment_id)
        root = await self._locked_comment(db, comment_id)
        stored_reason = _require_moderation_reason(reason)

        subtree = await self._collect_subtree(db, root)
        now = self._clock.now()
        row_reason = f"{HARD_HIDDEN_REASON_CODE}: {stored_reason}"
        for comment in subtree:
            comment.is_hard_hidden = True
            comment.deleted_at = now
            comment.deleted_by = actor.user_id
            comment.delete_reason = row_reason
        await db.flush()
        self._events.publish(
            DomainEvent(
                event_type=COMMENT_HARD_HIDDEN,
                aggregate_type="Comment",
                aggregate_id=root.id,
                occurred_at=now,
                payload={
                    "comment_id": str(root.id),
                    "task_id": str(root.task_id),
                    "deleted_by": str(actor.user_id),
                    "reason": stored_reason,
                    "hidden_comment_ids": sorted(
                        str(comment.id) for comment in subtree
                    ),
                },
            )
        )
        await db.commit()

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _serialize_row(
        comment: Comment, *, nickname: str, edited: bool
    ) -> CommentPublic:
        """Live rows render normally; deleted-but-rendered rows (parents
        anchoring surviving children) render as tombstones."""
        if comment.deleted_at is not None:
            return serialize_tombstone_comment(comment)
        return serialize_public_comment(
            comment, author_nickname=nickname, edited=edited
        )

    @staticmethod
    def _rendered_comment_ids(rows: Sequence[tuple[Comment, str, bool]]) -> set[UUID]:
        """The greatest fixpoint of "renders in the public list" over one
        task's thread (see the module docstring for the full ruling):

        - hard-hidden comments never render;
        - live comments render;
        - a soft-deleted comment renders (as a tombstone) exactly when at
          least one of its children renders.

        Monotone False->True flips bounded by the row count, so even a
        maliciously seeded parent cycle terminates (and renders nothing).
        """
        children: dict[UUID, list[UUID]] = {}
        for comment, _nickname, _edited in rows:
            if comment.parent_id is not None:
                children.setdefault(comment.parent_id, []).append(comment.id)
        rendered = {
            comment.id
            for comment, _nickname, _edited in rows
            if comment.deleted_at is None and not comment.is_hard_hidden
        }
        changed = True
        while changed:
            changed = False
            for comment, _nickname, _edited in rows:
                if (
                    comment.id in rendered
                    or comment.is_hard_hidden
                    or comment.deleted_at is None
                ):
                    continue
                if any(child in rendered for child in children.get(comment.id, ())):
                    rendered.add(comment.id)
                    changed = True
        return rendered

    @staticmethod
    async def _locked_comment(db: AsyncSession, comment_id: UUID) -> Comment:
        """The Comment row under FOR UPDATE, or the typed NOT_FOUND. One
        serialization point for concurrent edit/delete/moderate/hide on
        the same row, so the trio never half-writes."""
        comment = await db.scalar(
            select(Comment).where(Comment.id == comment_id).with_for_update()
        )
        if comment is None:
            raise CommentNotFoundError(comment_id)
        return comment

    @staticmethod
    async def _collect_subtree(db: AsyncSession, root: Comment) -> list[Comment]:
        """The target plus every descendant, BFS over ``parent_id``
        batches (thread depth is unbounded, width is batched). Children
        are read without locks — the module docstring records the V1
        race ruling."""
        subtree = [root]
        frontier = [root.id]
        while frontier:
            children = list(
                (
                    await db.scalars(
                        select(Comment).where(Comment.parent_id.in_(frontier))
                    )
                ).all()
            )
            subtree.extend(children)
            frontier = [child.id for child in children]
        return subtree

    @staticmethod
    async def _require_task_moderator(
        db: AsyncSession, actor: Actor, task_id: UUID
    ) -> None:
        """Spec §4.2/§21.4: the task's owner Teacher, or a collaborator
        holding MODERATE_COMMUNITY. Students are never moderators and
        Admin's removal tool is the hard hide — both are typed denials
        here (the T3 ruling: ``admit_admin=False``). Deliberately NOT
        gated on task visibility (see the module docstring): governance
        reaches paused/closed history too. The predicate lives in
        ``gates.require_task_moderation_site`` since the final review
        (fix I1); the comment's task FK keeps its unknown-task branch
        unreachable in practice."""
        await require_task_moderation_site(
            db,
            actor,
            task_id,
            admit_admin=False,
            error_factory=CommentModerationDeniedError,
        )

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
    async def _nickname(db: AsyncSession, user_id: UUID) -> str:
        """The author's nickname for the edit return path (the identity
        seam — see the module docstring)."""
        nickname = await db.scalar(
            select(_USERS.c.nickname).where(_USERS.c.id == user_id)
        )
        if nickname is None:
            raise CommenterNotFoundError(user_id)
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
