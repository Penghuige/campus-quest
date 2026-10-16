"""Registration approval + mobile login (V1.1 auth-extension II spec).

- users.status CHECK gains PENDING_APPROVAL (open enrollment's landing
  state); approved_at / rejected_reason record the admin verdict.
- login_mobile_challenges: the SMS login-code challenge table (the
  LoginEmailChallenge mirror — keyed digest, newest-row-per-phone).

Downgrade restores the four-value CHECK and drops both additions; it
fails loudly while any PENDING_APPROVAL row exists (the restored CHECK
rejects the value), which is the deliberate guard.
"""

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users DROP CONSTRAINT ck_users_status, ADD CONSTRAINT "
        "ck_users_status CHECK (status IN ('PENDING_PHONE', 'ACTIVE', "
        "'SUSPENDED', 'BANNED', 'PENDING_APPROVAL'))"
    )
    op.add_column(
        "users",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("rejected_reason", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "login_mobile_challenges",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("phone_e164", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_login_mobile_challenges_phone_e164",
        "login_mobile_challenges",
        ["phone_e164"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_login_mobile_challenges_phone_e164",
        table_name="login_mobile_challenges",
    )
    op.drop_table("login_mobile_challenges")
    op.drop_column("users", "rejected_reason")
    op.drop_column("users", "approved_at")
    # Loud on any PENDING_APPROVAL row: the transactional DDL rolls back.
    op.execute(
        "ALTER TABLE users DROP CONSTRAINT ck_users_status, ADD CONSTRAINT "
        "ck_users_status CHECK (status IN ('PENDING_PHONE', 'ACTIVE', "
        "'SUSPENDED', 'BANNED'))"
    )
