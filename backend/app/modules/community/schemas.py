# backend/app/modules/community/schemas.py
"""Community module commands and read DTOs (spec §21-§23;
backend-engineering §9).

Single responsibility: the dataclass commands the community services
consume and the read DTOs they produce. They are NOT wire shapes — task
9's router builds its own Pydantic models, and it MUST build them field
by field from these: the DTOs are the privacy boundary for author
identity (spec §21.4/§40).

- ``CommentPublic`` has NO ``user_id`` field at all — not for anonymous
  comments (the author renders as 匿名用户) and not for named ones (the
  nickname rides ``author_display``; student number, phone, email, and
  the raw user id are unrepresentable in the shape, so they cannot leak
  through serialization, nesting, or an accidental ORM dump).
  ``content`` is ``str | None`` (task 3): None exactly on tombstones —
  a deleted parent kept for thread anchoring carries NO content and the
  uniform 该评论已删除 display, so the deleted text is unrepresentable
  on the public surface the same way identity is.
- ``ModerationComment`` is the task-8 placeholder: the same base shape
  minus any author display, plus a ``moderation_key`` seam that stays
  ``None`` until the moderation task derives a pseudonymous key, and the
  task-3 ``hard_hidden`` flag so moderation review can distinguish
  Admin privacy/legal escalations from ordinary soft deletes. It
  deliberately carries no nickname either — Teacher moderation views may
  not show directly identifying material (spec §21.4), and the Admin
  reveal is a separate audited operation, never a field here.
- ``deleted`` exists on both shapes so the tombstone decision (task 3)
  does not change the DTO contract; the rulings live in
  comment_service.py — deleted PARENTS with surviving children render as
  tombstones, deleted leaves vanish, and replies to a tombstone are
  rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CreateComment:
    """Command for ``CommentService.create_comment`` (spec §21.1-§21.2).

    The untrusted HTTP surface constructs this after its own parsing; the
    service is the single validation/normalization authority, so
    ``content`` arrives raw and is trimmed/control-stripped/length-checked
    there. ``parent_id`` None means a root comment; a provided parent is
    validated for existence, same-Task membership, and non-deleted state.
    ``is_anonymous`` is the display attribute of THIS comment only (spec
    §21.4): it never changes what the row stores — the real ``user_id``
    always lands in persistence regardless.
    """

    task_id: UUID
    content: str
    parent_id: UUID | None = None
    is_anonymous: bool = False


@dataclass(frozen=True, slots=True)
class CommentPublic:
    """Privacy-safe comment read DTO (spec §21, §21.4, §40).

    ``author_display`` is the ONLY identity material: the nickname for a
    named comment, ``匿名用户`` for an anonymous one, and 该评论已删除 for
    a tombstone (in which case ``content`` is None). There is no
    ``user_id`` field, no username, no contact fields — by construction,
    not by filtering. ``edited`` reflects CommentRevision existence (spec
    §21.3 显示“已编辑”); ``deleted`` is the tombstone marker the thread
    rendering decides on.
    """

    id: UUID
    task_id: UUID
    parent_id: UUID | None
    content: str | None
    is_anonymous: bool
    author_display: str
    created_at: datetime
    updated_at: datetime
    edited: bool
    deleted: bool


@dataclass(frozen=True, slots=True)
class ModerationComment:
    """Moderation-queue read DTO, base shape (spec §21.4; task 8 extends).

    Everything ``CommentPublic`` carries except the author display, plus
    the ``moderation_key`` seam (None until task 8 derives the stable
    pseudonymous key for governance correlation) and ``hard_hidden``
    (task 3): True on Admin hard-hidden comments, which the public
    surface renders nothing of but moderation still reviews with content
    intact. The key must never equal a student number and must never
    appear on student surfaces — those rules are task 8's to implement;
    the shape refuses author identity from the start.
    """

    id: UUID
    task_id: UUID
    parent_id: UUID | None
    content: str | None
    is_anonymous: bool
    created_at: datetime
    updated_at: datetime
    edited: bool
    deleted: bool
    moderation_key: str | None = None
    hard_hidden: bool = False
