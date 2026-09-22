"""Notifications: logical notifications, deliveries, and templates.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (same discipline as 0002/0003). CHECK
constraints carry SHORT names ("channel", "status", "attempts"): Alembic
applies target_metadata's naming convention to them, and the "ck"
convention interpolates %(constraint_name)s into the final name — a full
"ck_notification_deliveries_channel" here would render doubled.

Design decisions (see app/modules/notifications/models.py):

- enum-like columns are VARCHAR + CHECK against the frozen member sets,
  not native PostgreSQL enums;
- UNIQUE(event_key, user_id, channel) on notification_deliveries is the
  delivery idempotency boundary (spec §25.3): duplicate events and Celery
  retries collapse onto one row per channel instead of double-sending;
- `attempts` is CHECK-pinned only to >= 0 — the retry ceiling is runtime
  configuration (spec §25.4), not a database invariant;
- (status, scheduled_at) backs the due-delivery scan that the dispatcher
  runs in bounded batches.

Revision ID: 0010
Revises: 0006
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPES = (
    "ASSIGNMENT_DEADLINE_24H",
    "ASSIGNMENT_DEADLINE_4H",
    "REVISION_REQUIRED",
    "SUBMISSION_APPROVED",
    "SUBMISSION_VALIDATION_FAILED",
    "REWARD_REDEMPTION_APPROVED",
    "REWARD_REDEMPTION_REJECTED",
    "ACCOUNT_SECURITY",
)
_EVENT_TYPE_LIST = ", ".join(f"'{value}'" for value in _EVENT_TYPES)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notifications_users_user_id",
        ),
        sa.CheckConstraint(
            f"event_type IN ({_EVENT_TYPE_LIST})",
            name="event_type",
        ),
        sa.UniqueConstraint(
            "event_key", "user_id", name="uq_notifications_event_key_user_id"
        ),
    )
    op.create_index(
        "ix_notifications_user_id", "notifications", ["user_id"], unique=False
    )
    op.create_table(
        "notification_deliveries",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_notification_deliveries"),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_notification_deliveries_notifications_notification_id",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notification_deliveries_users_user_id",
        ),
        sa.CheckConstraint("channel IN ('SMS', 'EMAIL', 'IN_APP')", name="channel"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RETRYABLE', 'SENDING', 'SENT', 'FAILED')",
            name="status",
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts"),
        sa.UniqueConstraint(
            "event_key",
            "user_id",
            "channel",
            name="uq_notification_deliveries_event_key_user_id_channel",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_notification_id",
        "notification_deliveries",
        ["notification_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_user_id",
        "notification_deliveries",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_status_scheduled_at",
        "notification_deliveries",
        ["status", "scheduled_at"],
        unique=False,
    )
    op.create_table(
        "notification_templates",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("template_body", sa.Text(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_notification_templates"),
        sa.CheckConstraint(
            f"event_type IN ({_EVENT_TYPE_LIST})",
            name="event_type",
        ),
        sa.CheckConstraint("channel IN ('SMS', 'EMAIL', 'IN_APP')", name="channel"),
        sa.CheckConstraint("version >= 1", name="version"),
        sa.UniqueConstraint(
            "event_type", "channel", name="uq_notification_templates_event_type_channel"
        ),
    )


def downgrade() -> None:
    op.drop_table("notification_templates")
    op.drop_index(
        "ix_notification_deliveries_status_scheduled_at",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_deliveries_user_id", table_name="notification_deliveries"
    )
    op.drop_index(
        "ix_notification_deliveries_notification_id",
        table_name="notification_deliveries",
    )
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
