"""Staff login convenience: login email challenges + remembered devices (V1.1 §2).

Two new tables and one column flip:

- ``login_email_challenges`` — the emailed 6-digit staff login code's
  challenge row (hashed code, expiry, verify-attempt counter), keyed by
  the normalized email. Its own table by design: the phone-OTP
  challenge lives in Redis under a per-challenge id; the email flow is
  newest-row-per-mailbox, DB-resident, and joined to no other state.
- ``remembered_devices`` — the 30-day "remember this device" cookie
  credential for ADMIN email-code login. Only the SHA-256 digest of the
  cookie value is stored; see the model docstring for the theft/revoke
  reasoning.
- ``users.password_hash`` goes NULLABLE: a TEACHER accepting a V1.1
  invitation may set no password (email code becomes the only login
  channel, §2.3), so the column can no longer be NOT NULL. Existing
  rows all carry a hash; ``downgrade`` restores the constraint and is
  safe only while no passwordless staff account exists.
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_email_challenges",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email_normalized", sa.String(length=320), nullable=False),
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
        "ix_login_email_challenges_email_normalized",
        "login_email_challenges",
        ["email_normalized"],
    )
    op.create_table(
        "remembered_devices",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_remembered_devices_user_id", "remembered_devices", ["user_id"])
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_index("ix_remembered_devices_user_id", table_name="remembered_devices")
    op.drop_table("remembered_devices")
    op.drop_index(
        "ix_login_email_challenges_email_normalized",
        table_name="login_email_challenges",
    )
    op.drop_table("login_email_challenges")
