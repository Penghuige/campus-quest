"""Submissions: submission versions, validation runs, review history,
and reward-lock audit.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (verified by `alembic check` after a clean
upgrade). CHECK constraints carry SHORT names ("validation_status"):
Alembic applies target_metadata's naming convention to them, and the "ck"
convention interpolates %(constraint_name)s into the final name — a full
"ck_submissions_validation_status" here would render doubled (the 0002/0003
gotcha).

Plan-numbering note: the plan drafted this as 0004_submissions, but
0004_pin_grace_period landed first through PR review; this migration is
0005 by controller ruling.

Retention contract (spec §13), stated here because psql readers of these
columns have no models.py:

- `retention_until` is a snapshot computed from the Task's retention
  policy at submission time; later Task edits must not rewrite it.
- Permanent retention is ALWAYS the explicit `retention_permanent` flag —
  never a huge sentinel date in `retention_until`.
- The coherence CHECK enforces exactly one of the two: neither set
  (undecided retention) and both set (ambiguous permanence) are
  unrepresentable.
- `legal_hold` blocks the cleanup worker regardless of the deadline;
  `deleted_at` records the raw object's removal while business metadata,
  validation reports, and audit rows survive.

`assignment_claims.latest_submission_id` stays a plain UUID with no FK:
submissions already FK to claims, and a reciprocal FK pair would be
circular; consistency is kept transactionally by the service with
UNIQUE(claim_id, version) as the database-side anchor.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("declared_type", sa.String(length=16), nullable=False),
        sa.Column("detected_type", sa.String(length=16), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "validation_status",
            sa.String(length=24),
            server_default=sa.text("'UPLOADED'"),
            nullable=False,
        ),
        sa.Column(
            "review_status",
            sa.String(length=24),
            server_default=sa.text("'PENDING_REVIEW'"),
            nullable=False,
        ),
        sa.Column("validation_report", JSONB(), nullable=True),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "retention_until",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Retention snapshot computed from the Task's retention policy at "
                "submission time (spec §13); later Task edits must not rewrite it. "
                "NULL exactly when retention_permanent is true."
            ),
        ),
        sa.Column(
            "retention_permanent",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment=(
                "Explicit permanent-retention flag (spec §13): permanence is "
                "always this flag, never a huge sentinel date in retention_until."
            ),
        ),
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment=(
                "Blocks the cleanup worker regardless of retention_until "
                "(spec §13/§27)."
            ),
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the raw upload was removed from object storage by the "
                "cleanup worker (spec §13/§27); business metadata, validation "
                "reports, and audit rows survive the file. NULL while the "
                "object exists."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submissions"),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["assignment_claims.id"],
            name="fk_submissions_assignment_claims_claim_id",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"], ["users.id"], name="fk_submissions_users_reviewer_id"
        ),
        sa.CheckConstraint(
            "validation_status IN "
            "('UPLOADED', 'VALIDATING', 'VALIDATED', 'VALIDATION_FAILED')",
            name="validation_status",
        ),
        sa.CheckConstraint(
            "review_status IN "
            "('PENDING_REVIEW', 'UNDER_REVIEW', 'APPROVED', 'REVISION_REQUIRED')",
            name="review_status",
        ),
        sa.CheckConstraint(
            "declared_type IN ('CSV', 'XLSX', 'SQLITE')", name="declared_type"
        ),
        sa.CheckConstraint(
            "detected_type IS NULL OR detected_type IN ('CSV', 'XLSX', 'SQLITE')",
            name="detected_type",
        ),
        sa.CheckConstraint("file_size >= 0", name="file_size"),
        sa.CheckConstraint("version >= 1", name="version"),
        sa.CheckConstraint(
            "(retention_permanent AND retention_until IS NULL) "
            "OR (NOT retention_permanent AND retention_until IS NOT NULL)",
            name="retention_exclusive",
        ),
        sa.UniqueConstraint(
            "claim_id", "version", name="uq_submissions_claim_id_version"
        ),
        sa.UniqueConstraint("object_key", name="uq_submissions_object_key"),
    )
    op.create_index(
        "ix_submissions_claim_id", "submissions", ["claim_id"], unique=False
    )
    op.create_index(
        "ix_submissions_reviewer_id", "submissions", ["reviewer_id"], unique=False
    )
    op.create_table(
        "submission_validations",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("report", JSONB(), nullable=True),
        sa.Column("parser_version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_submission_validations"),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_submission_validations_submissions_submission_id",
        ),
        sa.CheckConstraint(
            "status IN ('VALIDATING', 'VALIDATED', 'VALIDATION_FAILED')",
            name="status",
        ),
    )
    op.create_index(
        "ix_submission_validations_submission_id",
        "submission_validations",
        ["submission_id"],
        unique=False,
    )
    op.create_table(
        "submission_reviews",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submission_reviews"),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_submission_reviews_submissions_submission_id",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["users.id"],
            name="fk_submission_reviews_users_reviewer_id",
        ),
        sa.CheckConstraint(
            "action IN ('APPROVE', 'REQUIRE_REVISION', 'INVALIDATE_LOCK')",
            name="action",
        ),
    )
    op.create_index(
        "ix_submission_reviews_submission_id",
        "submission_reviews",
        ["submission_id"],
        unique=False,
    )
    op.create_table(
        "reward_lock_history",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=True),
        sa.Column("lock_status_from", sa.String(length=16), nullable=True),
        sa.Column("lock_status_to", sa.String(length=16), nullable=False),
        sa.Column("reward_tier_locked", sa.Integer(), nullable=True),
        sa.Column("locked_reward_points", sa.Integer(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reward_lock_history"),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["assignment_claims.id"],
            name="fk_reward_lock_history_assignment_claims_claim_id",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_reward_lock_history_submissions_submission_id",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"], ["users.id"], name="fk_reward_lock_history_users_changed_by"
        ),
        sa.CheckConstraint(
            "lock_status_from IS NULL OR lock_status_from "
            "IN ('NONE', 'PROVISIONAL', 'CONFIRMED', 'INVALIDATED')",
            name="lock_status_from",
        ),
        sa.CheckConstraint(
            "lock_status_to IN ('NONE', 'PROVISIONAL', 'CONFIRMED', 'INVALIDATED')",
            name="lock_status_to",
        ),
        sa.CheckConstraint(
            "locked_reward_points IS NULL OR locked_reward_points >= 0",
            name="locked_reward_points",
        ),
    )
    op.create_index(
        "ix_reward_lock_history_claim_id",
        "reward_lock_history",
        ["claim_id"],
        unique=False,
    )
    op.create_index(
        "ix_reward_lock_history_submission_id",
        "reward_lock_history",
        ["submission_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reward_lock_history_submission_id", table_name="reward_lock_history"
    )
    op.drop_index("ix_reward_lock_history_claim_id", table_name="reward_lock_history")
    op.drop_table("reward_lock_history")
    op.drop_index(
        "ix_submission_reviews_submission_id", table_name="submission_reviews"
    )
    op.drop_table("submission_reviews")
    op.drop_index(
        "ix_submission_validations_submission_id",
        table_name="submission_validations",
    )
    op.drop_table("submission_validations")
    op.drop_index("ix_submissions_reviewer_id", table_name="submissions")
    op.drop_index("ix_submissions_claim_id", table_name="submissions")
    op.drop_table("submissions")
