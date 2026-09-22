# backend/app/modules/community/schemas.py
"""Community module commands and read DTOs (spec §21-§23;
backend-engineering §9).

Single responsibility: the dataclass commands the community services
consume and the read DTOs they produce. They are NOT wire shapes — task
9's router builds its own Pydantic models, and it MUST build them field
by field from these: the DTOs are the privacy boundary for author
identity (spec §21.4/§40) and, since task 6, for reporter identity
(spec §23 被举报用户不可看到举报者身份).

- ``CommentPublic`` has NO ``user_id`` field at all — not for anonymous
  comments (the author renders as 匿名用户) and not for named ones (the
  nickname rides ``author_display``; student number, phone, email, and
  the raw user id are unrepresentable in the shape, so they cannot leak
  through serialization, nesting, or an accidental ORM dump).
  ``content`` is ``str | None`` (task 3): None exactly on tombstones —
  a deleted parent kept for thread anchoring carries NO content and the
  uniform 该评论已删除 display, so the deleted text is unrepresentable
  on the public surface the same way identity is.
- ``ModerationComment`` (task 8, spec §21.4): the Teacher-safe
  moderation record — the same base shape plus an ``author_display``
  (the nickname for named comments, 匿名用户 for anonymous ones) and the
  pseudonymous ``moderation_key`` derived server-side from
  (task, author), present exactly on anonymous records. Directly
  identifying material — student number/login username, phone, email,
  the raw user id — is unrepresentable in the shape; the Admin reveal is
  a separate audited operation returning ``RevealedIdentity``, never a
  field here.
- ``deleted`` exists on both shapes so the tombstone decision (task 3)
  does not change the DTO contract; the rulings live in
  comment_service.py — deleted PARENTS with surviving children render as
  tombstones, deleted leaves vanish, and replies to a tombstone are
  rejected.
- ``CommentReportView`` (task 6, spec §23) is the moderation-queue row:
  the report facts, the reported comment in the ``ModerationComment``
  base shape, and the REPORTER identity (``reporter_user_id`` +
  ``reporter_nickname``). It is the ONLY DTO in the module that carries
  reporter identity, on purpose: moderators see reporters (they act on
  the report), the reported user never does — this shape must never
  reach a student-facing surface, and ``report_comment`` returning the
  caller's OWN report row is the sole student-visible report material
  (one's own identity is not a leak). Both reporter fields are ``None``
  exactly on self-reports (task 6 review F2): rendering the anonymous
  author's own nickname beside their comment is the one reporter
  identity the queue must hide.
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
    """Teacher-safe moderation read DTO (spec §21.4; task 8).

    Everything ``CommentPublic`` carries, with the author surface the
    moderation context is allowed: ``author_display`` — the nickname
    for a named comment (already public), 匿名用户 for an anonymous one
    — and ``moderation_key``, the stable pseudonymous key (see
    ``serializers.derive_moderation_key``) that rides exactly the
    anonymous records so a moderator correlates one anonymous author's
    comments within a Task without learning who they are. The key never
    equals or derives from the student number and never appears on
    student surfaces; named records carry ``None`` (their correlation
    handle is the nickname — a key there would link an author's named
    and anonymous comments). ``hard_hidden`` (task 3) flags Admin
    privacy/legal removals, which the public surface renders nothing of
    but moderation still reviews with content intact. The raw user id
    stays unrepresentable; the reveal is the audited
    ``RevealedIdentity`` path, not a field here.
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
    moderation_key: str | None = None
    hard_hidden: bool = False


@dataclass(frozen=True, slots=True)
class RevealedIdentity:
    """The explicit Admin-reveal response (spec §21.4 每次追溯; task 8).

    The ONE shape in the community module where the student number
    (``username``) is representable at all: it is constructed only by
    ``ModerationService.request_identity_reveal`` — an Admin-only,
    reason-mandatory, every-call-audited operation — and must never be
    composed into a student-facing or Teacher moderation-list response.
    The ordinary moderation surfaces stay pseudonymous (匿名用户 +
    ``moderation_key``); the real identity appears here and only here.
    """

    user_id: UUID
    nickname: str
    username: str


@dataclass(frozen=True, slots=True)
class VoteResult:
    """Outcome of one vote toggle (spec §22; plan 06 task 4).

    ``current_value`` is the caller's stance AFTER the transition — 1
    like, -1 dislike, 0 none — and ``likes``/``dislikes`` are the
    comment's totals as of the same transaction, so the surface can echo
    the toggle without a second read. ``current_value`` is a plain int
    (not ``VoteValue``): 0 — the "no row" stance — is not a ``VoteValue``
    member, and the shape must represent all three post-states.
    """

    current_value: int
    likes: int
    dislikes: int


@dataclass(frozen=True, slots=True)
class CommentReportView:
    """Moderation-queue report row (spec §23; plan 06 task 6).

    Report facts (``category``/``note``/``status``/``handled_by``/
    ``handled_at``), the reported comment as the task-8 Teacher-safe
    shape (``comment`` — 匿名用户 plus the pseudonymous key for
    anonymous comments, by the §21.4 ruling that governs
    ``ModerationComment``), and the REPORTER identity
    (``reporter_user_id`` + ``reporter_nickname``; both ``None``
    exactly on self-reports — task 6 review F2 — where the reporter IS
    the comment's author, so the queue must not render the anonymous
    author's own nickname beside their comment). Spec §23 被举报用户
    不可看到举报者身份: this is the one shape reporter identity ever
    renders on, and ``ReportService.list_task_reports`` gates it to the
    owner/MODERATE_COMMUNITY/Admin surface — route layers must never
    compose it into a student-facing response. ``status`` is the raw
    OPEN/HANDLED/DISMISSED string (files land OPEN; the PR #2
    hardening closure endpoints — ``ReportService.dismiss_report`` /
    ``handle_report`` — own the terminal transitions).
    """

    id: UUID
    comment: ModerationComment
    category: str
    note: str | None
    status: str
    reporter_user_id: UUID | None
    reporter_nickname: str | None
    created_at: datetime
    handled_by: UUID | None
    handled_at: datetime | None
