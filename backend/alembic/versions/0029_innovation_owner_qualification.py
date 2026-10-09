"""Explicit owner application and admin activation, no new identity material.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ie_owner_qualifications",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("student_no", sa.String(40), nullable=False),
        sa.Column("major", sa.String(120), nullable=False),
        sa.Column("grade", sa.String(40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_ie_owner_qualifications")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ie_owner_qualifications_user_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name=op.f("fk_ie_owner_qualifications_approved_by_users"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED')",
            name=op.f("ck_ie_owner_qualifications_status_valid"),
        ),
        sa.CheckConstraint(
            "version > 0 AND profile_version > 0",
            name=op.f("ck_ie_owner_qualifications_versions_positive"),
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND approved_at IS NULL AND approved_by IS NULL) OR "
            "(status = 'APPROVED' AND approved_at IS NOT NULL "
            "AND approved_by IS NOT NULL)",
            name=op.f("ck_ie_owner_qualifications_approval_consistent"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80 AND btrim(name) <> ''",
            name=op.f("ck_ie_owner_qualifications_name_length"),
        ),
        sa.CheckConstraint(
            "char_length(student_no) BETWEEN 1 AND 40 AND btrim(student_no) <> ''",
            name=op.f("ck_ie_owner_qualifications_student_no_length"),
        ),
        sa.CheckConstraint(
            "char_length(major) BETWEEN 1 AND 120 AND btrim(major) <> ''",
            name=op.f("ck_ie_owner_qualifications_major_length"),
        ),
        sa.CheckConstraint(
            "char_length(grade) BETWEEN 1 AND 40 AND btrim(grade) <> ''",
            name=op.f("ck_ie_owner_qualifications_grade_length"),
        ),
    )
    op.create_index(
        "ix_ie_owner_qualifications_pending",
        "ie_owner_qualifications",
        ["requested_at", "user_id"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_table("ie_owner_qualifications")
