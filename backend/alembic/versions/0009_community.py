"""Community: comments, edit history, votes, reactions, reports, ratings.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata. CHECK constraints carry SHORT names
("category", "rating"): Alembic applies target_metadata's naming
convention to them, and the "ck" convention interpolates
%(constraint_name)s into the final name -- a full "ck_comment_reports_
category" here would render doubled (the 0002/0003 gotcha).

Numbering note: this migration is 0009 with down_revision 0006 by
parallel-stream controller ruling; 0007/0008 belong to sibling streams,
and the controller reparents the chain when the streams merge.

Design decisions (see app/modules/community/models.py for the full list):

- enum-like columns are VARCHAR + CHECK, not native PostgreSQL enums;
- `comments.parent_id` self-FKs, so replies to nonexistent comments are
  rejected by the database; same-Task and acyclicity stay in the service;
- the comment soft-delete trio (deleted_at/deleted_by/delete_reason)
  stores but does not CHECK-enforce coherence (service writes all three);
- `comments.is_hard_hidden` is the Admin hard-hide flag (spec §21.3
  彻底隐藏): added to this migration in place by task-3 controller ruling
  (this stream owns 0009 and the branch is unshared) so the privacy/legal
  subtree hide has a persistent marker distinct from plain soft delete;
- `comment_revisions` snapshots the FULL SUPERSEDED content (the previous
  version) plus edited_at and is append-only by service rule (spec §21.3
  修改历史; the comment row itself stays the latest version);
- `comment_votes.value` is INTEGER CHECK IN (1, -1) (spec §22);
- `comment_reactions.emoji` has NO database whitelist — the emoji set is
  Admin-configured at runtime (spec §22) and service-enforced;
- `comment_reports` duplicate guard is the plain UNIQUE(comment_id,
  reporter_user_id, category): a moderator decision per (comment,
  reporter, category) is final in V1, so re-reporting the same category
  after HANDLED/DISMISSED stays blocked while other categories remain
  filable (spec §23 SHOULD);
- `task_ratings` enforces UNIQUE(task_id, user_id) with rating CHECKed
  to 1-5 (spec §20); the completer gate is a service predicate.

Revision ID: 0009
Revises: 0012
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "is_anonymous",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.Column("delete_reason", sa.Text(), nullable=True),
        sa.Column(
            "is_hard_hidden",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_comments"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_comments_tasks_task_id"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_comments_users_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["comments.id"], name="fk_comments_comments_parent_id"
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"], ["users.id"], name="fk_comments_users_deleted_by"
        ),
    )
    op.create_index("ix_comments_task_id", "comments", ["task_id"], unique=False)
    op.create_index("ix_comments_user_id", "comments", ["user_id"], unique=False)
    op.create_index("ix_comments_parent_id", "comments", ["parent_id"], unique=False)
    op.create_table(
        "comment_revisions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "edited_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_comment_revisions"),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name="fk_comment_revisions_comments_comment_id",
        ),
    )
    op.create_index(
        "ix_comment_revisions_comment_id",
        "comment_revisions",
        ["comment_id"],
        unique=False,
    )
    op.create_table(
        "comment_votes",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_comment_votes"),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name="fk_comment_votes_comments_comment_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_comment_votes_users_user_id"
        ),
        sa.CheckConstraint("value IN (1, -1)", name="value"),
        sa.UniqueConstraint(
            "comment_id", "user_id", name="uq_comment_votes_comment_id_user_id"
        ),
    )
    op.create_index(
        "ix_comment_votes_comment_id", "comment_votes", ["comment_id"], unique=False
    )
    op.create_index(
        "ix_comment_votes_user_id", "comment_votes", ["user_id"], unique=False
    )
    op.create_table(
        "comment_reactions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("emoji", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_comment_reactions"),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name="fk_comment_reactions_comments_comment_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_comment_reactions_users_user_id"
        ),
        sa.UniqueConstraint(
            "comment_id",
            "user_id",
            "emoji",
            name="uq_comment_reactions_comment_id_user_id_emoji",
        ),
    )
    op.create_index(
        "ix_comment_reactions_comment_id",
        "comment_reactions",
        ["comment_id"],
        unique=False,
    )
    op.create_index(
        "ix_comment_reactions_user_id",
        "comment_reactions",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "comment_reports",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("comment_id", sa.Uuid(), nullable=False),
        sa.Column("reporter_user_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'OPEN'"),
            nullable=False,
        ),
        sa.Column("handled_by", sa.Uuid(), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_comment_reports"),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["comments.id"],
            name="fk_comment_reports_comments_comment_id",
        ),
        sa.ForeignKeyConstraint(
            ["reporter_user_id"],
            ["users.id"],
            name="fk_comment_reports_users_reporter_user_id",
        ),
        sa.ForeignKeyConstraint(
            ["handled_by"], ["users.id"], name="fk_comment_reports_users_handled_by"
        ),
        sa.CheckConstraint(
            "category IN ('SPAM', 'HARASSMENT', 'PRIVACY', 'OTHER')",
            name="category",
        ),
        sa.CheckConstraint("status IN ('OPEN', 'HANDLED', 'DISMISSED')", name="status"),
        sa.UniqueConstraint(
            "comment_id",
            "reporter_user_id",
            "category",
            name="uq_comment_reports_comment_id_reporter_user_id_category",
        ),
    )
    op.create_index(
        "ix_comment_reports_comment_id",
        "comment_reports",
        ["comment_id"],
        unique=False,
    )
    op.create_index(
        "ix_comment_reports_reporter_user_id",
        "comment_reports",
        ["reporter_user_id"],
        unique=False,
    )
    op.create_table(
        "task_ratings",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_ratings"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_task_ratings_tasks_task_id"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_task_ratings_users_user_id"
        ),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="rating"),
        sa.UniqueConstraint(
            "task_id", "user_id", name="uq_task_ratings_task_id_user_id"
        ),
    )
    op.create_index(
        "ix_task_ratings_task_id", "task_ratings", ["task_id"], unique=False
    )
    op.create_index(
        "ix_task_ratings_user_id", "task_ratings", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_task_ratings_user_id", table_name="task_ratings")
    op.drop_index("ix_task_ratings_task_id", table_name="task_ratings")
    op.drop_table("task_ratings")
    op.drop_index("ix_comment_reports_reporter_user_id", table_name="comment_reports")
    op.drop_index("ix_comment_reports_comment_id", table_name="comment_reports")
    op.drop_table("comment_reports")
    op.drop_index("ix_comment_reactions_user_id", table_name="comment_reactions")
    op.drop_index("ix_comment_reactions_comment_id", table_name="comment_reactions")
    op.drop_table("comment_reactions")
    op.drop_index("ix_comment_votes_user_id", table_name="comment_votes")
    op.drop_index("ix_comment_votes_comment_id", table_name="comment_votes")
    op.drop_table("comment_votes")
    op.drop_index("ix_comment_revisions_comment_id", table_name="comment_revisions")
    op.drop_table("comment_revisions")
    op.drop_index("ix_comments_parent_id", table_name="comments")
    op.drop_index("ix_comments_user_id", table_name="comments")
    op.drop_index("ix_comments_task_id", table_name="comments")
    op.drop_table("comments")
