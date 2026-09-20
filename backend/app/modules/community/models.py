# backend/app/modules/community/models.py
"""Community module persistence models (spec §20-23; invariants §31.7-31.9).

Design decisions:

- Enum-like columns are VARCHAR with explicitly named CHECK constraints,
  not PostgreSQL native enums — same rationale as the identity and task
  modules (adding a member is a constraint swap, not ALTER TYPE). The
  short CHECK names compose with the naming convention in `app.db.base`
  into e.g. `ck_comment_reports_category`.
- `Comment` content is plain TEXT, never HTML (spec §21.1/§33.1: 所有用户
  文本按纯文本处理). The 2000-character default limit is deliberately NOT
  a database CHECK: the spec calls it configurable (可配置), so it is a
  service-layer write-time validation.
- Soft delete is the three-column trio `deleted_at` / `deleted_by` /
  `delete_reason` (spec §21.3: 默认软删除). Child comments survive a
  deleted parent, so nothing cascades. The service writes all three
  together; there is deliberately no coherence CHECK here so a future
  content-hard-hide flow (Admin 隐私/违法场景) can record a reason with a
  different actor set without a migration. `deleted_by` is the acting
  moderator (or the author for self-deletion).
- `parent_id` carries a self-FK so the database itself rejects replies to
  nonexistent comments (spec §21.2: 回复不存在). The other two §21.2
  guards — parent belongs to the same Task, and no reference cycles —
  need cross-row reads and stay in the service layer; arbitrary depth is
  representable (前端两层视觉结构 is presentation-only).
- `CommentRevision` is the §21.3 修改历史: each edit appends one row with
  the FULL revised content (not a diff) plus `edited_at`, so the latest
  revision is self-contained and 普通用户只看到最新版 stays a plain
  latest-wins read. Append-only is a service-layer rule (no UPDATE path);
  the table has no `updated_at` by design.
- `CommentVote.value` is an INTEGER CHECK-constrained to (1, -1) (spec
  §22), mirrored by the `VoteValue` IntEnum. Toggle transitions
  (none→like, like→none, like→dislike, ...) MUST be atomic — that is the
  vote service's transactional job; the UNIQUE(comment_id, user_id)
  constraint is the database-side anchor (§31.8).
- `CommentReaction.emoji` is VARCHAR with NO database whitelist: the spec
  (§22) makes the emoji set an Admin-configured 白名单 that changes at
  runtime, so enforcing members here would require a migration per
  configuration change. The service layer validates against the
  Admin-configured set; the UNIQUE(comment_id, user_id, emoji) triple
  (§31.9) is what the database guarantees. Multi-codepoint emoji (❤️ is
  two codepoints) fit the 16-character bound with room to spare.
- `CommentReport` duplicate guard is the plain UNIQUE(comment_id,
  reporter_user_id, category): the spec's SHOULD (§23: 同一用户对同一
  评论同类别防止重复刷举报) is per category, and a partial index keyed on
  "status not terminal" would additionally allow one pending report per
  category after each handling cycle. The simpler full UNIQUE was chosen
  deliberately: re-reporting the SAME category after HANDLED/DISMISSED
  stays blocked (a moderator decision already exists for that pair), while
  a different category on the same comment remains filable. A future
  product need to re-open categories should swap this constraint in the
  migration that introduces that flow.
- Report status is OPEN/HANDLED/DISMISSED (see enums.py for the decision)
  with `handled_by`/`handled_at` nullable until a moderator acts.
- `TaskRating` is one row per (task, user) (§20, §31.7): 评分允许修改，
  不允许创建多条 — updates happen in place, which is why the table has
  an `updated_at`. "only completers may rate" (只有至少完成过该 Task 一个
  Claim 的用户可评分) is a service-layer predicate over assignment_claims,
  not a structural constraint: it needs a query, and ratings must survive
  independently of later claim history.
- No ORM relationships are declared yet; navigation joins arrive with the
  services that need them (backend-engineering §8).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Comment(Base):
    """A student comment on a Task, optionally a reply (spec §21)."""

    __tablename__ = "comments"

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    # Self-reference: NULL for root comments. The FK rejects replies to
    # nonexistent comments; same-Task and acyclicity are service rules.
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("comments.id"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    # Display attribute of this one comment only (spec §21.4): it never
    # changes the author's leaderboard identity.
    is_anonymous: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )
    # Soft-delete trio (spec §21.3): all three are written together by the
    # deleting service; children of a deleted parent survive.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    delete_reason: Mapped[str | None] = mapped_column(Text)


class CommentRevision(Base):
    """Append-only edit history entry: the full revised content of one
    edit plus when it was made (spec §21.3 后台保存修改历史)."""

    __tablename__ = "comment_revisions"

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    comment_id: Mapped[UUID] = mapped_column(ForeignKey("comments.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CommentVote(Base):
    """A +1/-1 vote by one user on one comment (spec §22, §31.8).

    Toggling to "none" deletes the row; like↔dislike flips UPDATE `value`
    inside the vote service's transaction.
    """

    __tablename__ = "comment_votes"
    __table_args__ = (
        CheckConstraint(
            "value IN (1, -1)",
            name="value",
        ),
        UniqueConstraint(
            "comment_id", "user_id", name="uq_comment_votes_comment_id_user_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    comment_id: Mapped[UUID] = mapped_column(ForeignKey("comments.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    value: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CommentReaction(Base):
    """An emoji reaction by one user on one comment (spec §22, §31.9).

    The emoji whitelist is Admin-configured at runtime and therefore
    service-enforced; the database guarantees the per-emoji uniqueness.
    """

    __tablename__ = "comment_reactions"
    __table_args__ = (
        UniqueConstraint(
            "comment_id",
            "user_id",
            "emoji",
            name="uq_comment_reactions_comment_id_user_id_emoji",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    comment_id: Mapped[UUID] = mapped_column(ForeignKey("comments.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    emoji: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CommentReport(Base):
    """A user's report of a comment entering the moderation queue
    (spec §23: 举报不自动删除评论)."""

    __tablename__ = "comment_reports"
    __table_args__ = (
        CheckConstraint(
            "category IN ('SPAM', 'HARASSMENT', 'PRIVACY', 'OTHER')",
            name="category",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'HANDLED', 'DISMISSED')",
            name="status",
        ),
        # Plain UNIQUE, not a terminal-status partial index: a moderator
        # decision on (comment, reporter, category) is final in V1, so
        # re-reporting the same category stays blocked after HANDLED or
        # DISMISSED (see module docstring for the full trade-off).
        UniqueConstraint(
            "comment_id",
            "reporter_user_id",
            "category",
            name="uq_comment_reports_comment_id_reporter_user_id_category",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    comment_id: Mapped[UUID] = mapped_column(ForeignKey("comments.id"), index=True)
    reporter_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'OPEN'"))
    handled_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )


class TaskRating(Base):
    """A 1-5 rating of a Task by a user who completed at least one of its
    Claims (spec §20, §31.7; the completer gate is a service predicate)."""

    __tablename__ = "task_ratings"
    __table_args__ = (
        CheckConstraint(
            "rating >= 1 AND rating <= 5",
            name="rating",
        ),
        UniqueConstraint("task_id", "user_id", name="uq_task_ratings_task_id_user_id"),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )
