"""Tasks: task definitions, collaborators, assignments, and claims.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (verified by diffing a clean `alembic upgrade`
schema against `Base.metadata.create_all`). CHECK constraints carry SHORT
names ("status", "platform"): Alembic applies target_metadata's naming
convention to them, and the "ck" convention interpolates %(constraint_name)s
into the final name -- a full "ck_tasks_status" here would render as the
doubled "ck_tasks_ck_tasks_status" (the 0002 gotcha).

Design decisions (see app/modules/tasks/models.py for the full list):

- enum-like columns are VARCHAR + CHECK, not native PostgreSQL enums;
- collaborator `permissions`, `allowed_file_types`, and
  `notification_channels` are ARRAY(VARCHAR) guarded by `<@` CHECKs against
  closed value sets;
- claim uniqueness is two partial unique indexes over the non-terminal
  status set: one active Claim per Assignment and one non-terminal Claim
  per user per Task (spec §31.4-31.5); terminal rows (COMPLETED/ABANDONED/
  EXPIRED) drop out of both so they can be followed by a fresh claim;
- `assignment_claims.latest_submission_id` carries no FK: the submissions
  table arrives with plan 04, which will add it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHERE_CLAIM_IS_ACTIVE = (
    "status IN ('CLAIMED', 'VALIDATING', 'UNDER_REVIEW', 'REVISION_REQUIRED')"
)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_teacher_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("rarity", sa.String(length=16), nullable=False),
        sa.Column("base_reward_points", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deadline_mode", sa.String(length=16), nullable=False),
        sa.Column("fixed_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "grace_period_minutes",
            sa.Integer(),
            server_default=sa.text("1440"),
            nullable=False,
        ),
        sa.Column(
            "claim_cutoff_minutes",
            sa.Integer(),
            server_default=sa.text("240"),
            nullable=False,
        ),
        sa.Column("submission_schema", JSONB(), nullable=True),
        sa.Column("submission_schema_version", sa.Integer(), nullable=True),
        sa.Column("allowed_file_types", ARRAY(sa.String(length=16)), nullable=False),
        sa.Column("max_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "notify_24h", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "notify_4h", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("notification_channels", ARRAY(sa.String(length=16)), nullable=False),
        sa.Column(
            "retention_policy",
            sa.String(length=16),
            server_default=sa.text("'DAYS_180'"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.ForeignKeyConstraint(
            ["owner_teacher_id"],
            ["users.id"],
            name="fk_tasks_users_owner_teacher_id",
        ),
        sa.CheckConstraint("task_type IN ('DATA_CRAWL')", name="task_type"),
        sa.CheckConstraint(
            "rarity IN ('NORMAL', 'RARE', 'EPIC', 'LEGENDARY')", name="rarity"
        ),
        sa.CheckConstraint("base_reward_points > 0", name="base_reward_points"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'PAUSED', 'CLOSED', 'ARCHIVED')",
            name="status",
        ),
        sa.CheckConstraint(
            "deadline_mode IN ('FIXED', 'RELATIVE')", name="deadline_mode"
        ),
        sa.CheckConstraint(
            "duration_minutes IS NULL OR duration_minutes > 0",
            name="duration_minutes",
        ),
        sa.CheckConstraint("grace_period_minutes > 0", name="grace_period_minutes"),
        sa.CheckConstraint("claim_cutoff_minutes >= 0", name="claim_cutoff_minutes"),
        sa.CheckConstraint(
            "allowed_file_types <@ ARRAY['CSV', 'XLSX', 'SQLITE']::varchar[]",
            name="allowed_file_types",
        ),
        sa.CheckConstraint("max_file_size_bytes > 0", name="max_file_size_bytes"),
        sa.CheckConstraint(
            "notification_channels <@ ARRAY['SMS', 'EMAIL', 'IN_APP']::varchar[]",
            name="notification_channels",
        ),
        sa.CheckConstraint(
            "retention_policy IN ('DAYS_30', 'DAYS_90', 'DAYS_180', 'PERMANENT')",
            name="retention_policy",
        ),
    )
    op.create_index(
        "ix_tasks_owner_teacher_id", "tasks", ["owner_teacher_id"], unique=False
    )
    op.create_table(
        "task_collaborators",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("permissions", ARRAY(sa.String(length=32)), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_collaborators"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_task_collaborators_tasks_task_id"
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
            name="fk_task_collaborators_users_teacher_id",
        ),
        sa.CheckConstraint(
            "permissions <@ ARRAY['VIEW_TASK', 'MANAGE_ASSIGNMENTS', "
            "'REVIEW_SUBMISSIONS', 'MODERATE_COMMUNITY']::varchar[]",
            name="permissions",
        ),
        sa.CheckConstraint(
            "array_length(permissions, 1) > 0", name="permissions_nonempty"
        ),
        sa.UniqueConstraint(
            "task_id", "teacher_id", name="uq_task_collaborators_task_id_teacher_id"
        ),
    )
    op.create_index(
        "ix_task_collaborators_teacher_id",
        "task_collaborators",
        ["teacher_id"],
        unique=False,
    )
    op.create_table(
        "assignments",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("availability_status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignments"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_assignments_tasks_task_id"
        ),
        sa.CheckConstraint(
            "platform IN ('xiaohongshu', 'douyin', 'zhihu')", name="platform"
        ),
        sa.CheckConstraint(
            "availability_status IN ('AVAILABLE', 'OCCUPIED', 'COMPLETED', 'RETIRED')",
            name="availability_status",
        ),
        sa.UniqueConstraint(
            "task_id",
            "platform",
            "keyword",
            name="uq_assignments_task_id_platform_keyword",
        ),
    )
    op.create_index("ix_assignments_task_id", "assignments", ["task_id"], unique=False)
    op.create_table(
        "assignment_claims",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grace_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reward_policy_snapshot", JSONB(), nullable=False),
        sa.Column("base_reward_points_snapshot", sa.Integer(), nullable=False),
        sa.Column("reward_lock_status", sa.String(length=16), nullable=False),
        sa.Column("reward_tier_locked", sa.Integer(), nullable=True),
        sa.Column("reward_locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_reward_points", sa.Integer(), nullable=True),
        sa.Column("latest_submission_id", sa.Uuid(), nullable=True),
        sa.Column("revision_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_claims"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.id"],
            name="fk_assignment_claims_assignments_assignment_id",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_assignment_claims_tasks_task_id"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_assignment_claims_users_user_id"
        ),
        sa.CheckConstraint(
            "status IN ('CLAIMED', 'VALIDATING', 'UNDER_REVIEW', "
            "'REVISION_REQUIRED', 'COMPLETED', 'ABANDONED', 'EXPIRED')",
            name="status",
        ),
        sa.CheckConstraint(
            "reward_lock_status IN ('NONE', 'PROVISIONAL', 'CONFIRMED', 'INVALIDATED')",
            name="reward_lock_status",
        ),
        sa.CheckConstraint(
            "locked_reward_points IS NULL OR locked_reward_points >= 0",
            name="locked_reward_points",
        ),
    )
    op.create_index(
        "ix_assignment_claims_assignment_id",
        "assignment_claims",
        ["assignment_id"],
        unique=False,
    )
    op.create_index(
        "ix_assignment_claims_task_id", "assignment_claims", ["task_id"], unique=False
    )
    op.create_index(
        "ix_assignment_claims_user_id", "assignment_claims", ["user_id"], unique=False
    )
    op.create_index(
        "uq_assignment_claims_active_assignment",
        "assignment_claims",
        ["assignment_id"],
        unique=True,
        postgresql_where=sa.text(_WHERE_CLAIM_IS_ACTIVE),
    )
    op.create_index(
        "uq_assignment_claims_active_user_task",
        "assignment_claims",
        ["user_id", "task_id"],
        unique=True,
        postgresql_where=sa.text(_WHERE_CLAIM_IS_ACTIVE),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_assignment_claims_active_user_task", table_name="assignment_claims"
    )
    op.drop_index(
        "uq_assignment_claims_active_assignment", table_name="assignment_claims"
    )
    op.drop_index("ix_assignment_claims_user_id", table_name="assignment_claims")
    op.drop_index("ix_assignment_claims_task_id", table_name="assignment_claims")
    op.drop_index("ix_assignment_claims_assignment_id", table_name="assignment_claims")
    op.drop_table("assignment_claims")
    op.drop_index("ix_assignments_task_id", table_name="assignments")
    op.drop_table("assignments")
    op.drop_index("ix_task_collaborators_teacher_id", table_name="task_collaborators")
    op.drop_table("task_collaborators")
    op.drop_index("ix_tasks_owner_teacher_id", table_name="tasks")
    op.drop_table("tasks")
