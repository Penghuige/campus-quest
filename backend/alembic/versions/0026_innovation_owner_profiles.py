"""Private owner profiles; saving does not grant qualification.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ie_owner_profiles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("student_no", sa.String(40), nullable=False),
        sa.Column("major", sa.String(120), nullable=False),
        sa.Column("grade", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_ie_owner_profiles")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_ie_owner_profiles_user_id_users")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80 AND btrim(name) <> ''",
            name=op.f("ck_ie_owner_profiles_name_length"),
        ),
        sa.CheckConstraint(
            "char_length(student_no) BETWEEN 1 AND 40 AND btrim(student_no) <> ''",
            name=op.f("ck_ie_owner_profiles_student_no_length"),
        ),
        sa.CheckConstraint(
            "char_length(major) BETWEEN 1 AND 120 AND btrim(major) <> ''",
            name=op.f("ck_ie_owner_profiles_major_length"),
        ),
        sa.CheckConstraint(
            "char_length(grade) BETWEEN 1 AND 40 AND btrim(grade) <> ''",
            name=op.f("ck_ie_owner_profiles_grade_length"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_ie_owner_profiles_version_positive")
        ),
    )


def downgrade() -> None:
    op.drop_table("ie_owner_profiles")
