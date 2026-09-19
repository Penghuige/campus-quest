"""Identity revision: users, whitelist, sessions, staff invitations, 2FA.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (verified by comparing a clean `alembic upgrade`
schema against `Base.metadata.create_all`). CHECK constraints carry SHORT
names ("role", "status"): Alembic applies target_metadata's naming
convention to them, and the "ck" convention interpolates %(constraint_name)s
into the final name -- a full "ck_users_role" here would render as the
doubled "ck_users_ck_users_role".

Design decisions (see app/modules/identity/models.py for the full list):

- `role` / `status` are VARCHAR + CHECK constraints, not native PostgreSQL
  enums: adding a member later is a constraint swap instead of ALTER TYPE.
- `users.phone_e164` and `users.email_normalized` are nullable with partial
  unique indexes (`WHERE ... IS NOT NULL`): PENDING_PHONE students and staff
  may have no phone, unbound emails never collide, and every bound value
  stays globally unique (spec §5.4, §5.5, §31.1-31.2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("nickname", sa.String(length=255), nullable=False),
        sa.Column("phone_e164", sa.String(length=32), nullable=True),
        sa.Column("email_normalized", sa.String(length=320), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.CheckConstraint("role IN ('STUDENT', 'TEACHER', 'ADMIN')", name="role"),
        sa.CheckConstraint(
            "status IN ('PENDING_PHONE', 'ACTIVE', 'SUSPENDED', 'BANNED')",
            name="status",
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index(
        "uq_users_phone_e164",
        "users",
        ["phone_e164"],
        unique=True,
        postgresql_where=sa.text("phone_e164 IS NOT NULL"),
    )
    op.create_index(
        "uq_users_email_normalized",
        "users",
        ["email_normalized"],
        unique=True,
        postgresql_where=sa.text("email_normalized IS NOT NULL"),
    )
    op.create_table(
        "student_whitelist",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("student_number", sa.String(length=64), nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_student_whitelist"),
        sa.UniqueConstraint(
            "student_number", name="uq_student_whitelist_student_number"
        ),
    )
    op.create_table(
        "user_sessions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_sessions_users_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by"],
            ["user_sessions.id"],
            name="fk_user_sessions_user_sessions_replaced_by",
        ),
        sa.UniqueConstraint(
            "refresh_token_hash", name="uq_user_sessions_refresh_token_hash"
        ),
    )
    op.create_index(
        "ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False
    )
    op.create_table(
        "staff_invitations",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_staff_invitations"),
        sa.CheckConstraint("role IN ('TEACHER', 'ADMIN')", name="role"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_staff_invitations_users_created_by",
        ),
        sa.UniqueConstraint("token_hash", name="uq_staff_invitations_token_hash"),
    )
    op.create_index(
        "ix_staff_invitations_email_normalized",
        "staff_invitations",
        ["email_normalized"],
        unique=False,
    )
    op.create_table(
        "totp_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_totp_credentials"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_totp_credentials_users_user_id",
        ),
    )
    op.create_table(
        "recovery_codes",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_recovery_codes"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_recovery_codes_users_user_id"
        ),
        sa.UniqueConstraint(
            "user_id", "code_hash", name="uq_recovery_codes_user_id_code_hash"
        ),
    )
    op.create_index(
        "ix_recovery_codes_user_id", "recovery_codes", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_recovery_codes_user_id", table_name="recovery_codes")
    op.drop_table("recovery_codes")
    op.drop_table("totp_credentials")
    op.drop_index(
        "ix_staff_invitations_email_normalized", table_name="staff_invitations"
    )
    op.drop_table("staff_invitations")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("student_whitelist")
    op.drop_index("uq_users_email_normalized", table_name="users")
    op.drop_index("uq_users_phone_e164", table_name="users")
    op.drop_table("users")
