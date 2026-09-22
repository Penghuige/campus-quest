"""Points: immutable ledger, wallet projection, reservations, and reward
redemptions.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (verified by `alembic check` after a clean
upgrade). CHECK constraints carry SHORT names ("ledger_type"): the "ck"
convention interpolates %(constraint_name)s, so a full
"ck_points_ledger_ledger_type" here would render doubled (the 0003 gotcha).

Plan-numbering note: the plan drafted this as 0005_points_rewards, but
0005_submissions and 0006_upload_intents landed first through earlier
plans; this migration is 0007 by controller ruling.

Contract notes for psql readers of these tables (no models.py there):

- `points_ledger` is the auditable source of truth for economic history
  and is APPEND-ONLY by service convention: no UPDATE path, no trigger.
  Corrections are new rows linked through `reversal_of_id`. The database
  deliberately does not enforce append-only.
- UNIQUE(source_type, source_id, ledger_type) is the idempotency key for
  ORIGINAL entries: one ASSIGNMENT_REWARD per claim (spec §31.6); a claim's
  reversal (different ledger_type) fits the same source without colliding,
  and the redemption/refund pair works the same way per redemption.
- `ranking_effective_at` is the ranking period attribution and is
  mandatory exactly when affects_ranking (the coherence CHECK): a reversal
  carries the ORIGINAL reward's effective time so it repairs the original
  period (spec §17.2).
- `point_wallets` is a rebuildable projection (spec §15.1), never negative
  (spec §31.12); user_id is the primary key, so one row per user.
- Reserved reward stock is NOT denormalized onto reward_items: occupancy
  is derived as the count of the item's reward_redemptions with status IN
  (REQUESTED, UNDER_REVIEW, APPROVED, FULFILLED) — exactly the member set
  spec §16.1 counts — computed under the reward_items row lock (FOR
  UPDATE) that serializes redemption. REJECTED releases occupancy by the
  status flip alone.
- `reward_redemptions.term_key` is the request-time snapshot of the
  admin-configured current academic term (spec §16.1): per-user-term
  limits count by the snapshot, never by the current term.
- `point_reservations` freezes points for one open redemption: exactly one
  row per redemption (UNIQUE(redemption_id)), transitioning in place
  ACTIVE -> CONSUMED (approved) or ACTIVE -> RELEASED (rejected/timeout),
  with released_at stamped exactly on leaving ACTIVE (the coherence
  CHECK).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "points_ledger",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("affects_balance", sa.Boolean(), nullable=False),
        sa.Column("affects_ranking", sa.Boolean(), nullable=False),
        sa.Column("ranking_effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_of_id", sa.Uuid(), nullable=True),
        sa.Column("operator_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_points_ledger"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_points_ledger_users_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["reversal_of_id"],
            ["points_ledger.id"],
            name="fk_points_ledger_points_ledger_reversal_of_id",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["users.id"], name="fk_points_ledger_users_operator_id"
        ),
        sa.CheckConstraint(
            "ledger_type IN "
            "("
            "'ASSIGNMENT_REWARD', "
            "'ASSIGNMENT_REWARD_REVERSAL', "
            "'REWARD_REDEMPTION', "
            "'REWARD_REDEMPTION_REFUND', "
            "'ADMIN_ADJUSTMENT'"
            ")",
            name="ledger_type",
        ),
        sa.CheckConstraint("amount <> 0", name="amount"),
        sa.CheckConstraint(
            "NOT affects_ranking OR ranking_effective_at IS NOT NULL",
            name="ranking_effective_required",
        ),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "ledger_type",
            name="uq_points_ledger_source_type_source_id_ledger_type",
        ),
    )
    op.create_index(
        "ix_points_ledger_user_id", "points_ledger", ["user_id"], unique=False
    )
    op.create_index(
        "ix_points_ledger_reversal_of_id",
        "points_ledger",
        ["reversal_of_id"],
        unique=False,
    )
    op.create_table(
        "point_wallets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "available_points",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "earned_points",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_point_wallets"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_point_wallets_users_user_id"
        ),
        sa.CheckConstraint("available_points >= 0", name="available_points"),
        sa.CheckConstraint("earned_points >= 0", name="earned_points"),
    )
    op.create_table(
        "reward_items",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("point_cost", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("per_user_term_limit", sa.Integer(), nullable=True),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "requires_manual_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("fulfillment_instructions", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_reward_items"),
        sa.CheckConstraint("point_cost > 0", name="point_cost"),
        sa.CheckConstraint("stock IS NULL OR stock >= 0", name="stock"),
        sa.CheckConstraint(
            "per_user_term_limit IS NULL OR per_user_term_limit >= 0",
            name="per_user_term_limit",
        ),
        sa.CheckConstraint(
            "available_from IS NULL "
            "OR available_until IS NULL "
            "OR available_from < available_until",
            name="window_ordering",
        ),
    )
    op.create_table(
        "reward_redemptions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("reward_item_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'REQUESTED'"),
            nullable=False,
        ),
        sa.Column("term_key", sa.String(length=64), nullable=False),
        sa.Column("points", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fulfillment_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reward_redemptions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_reward_redemptions_users_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["reward_item_id"],
            ["reward_items.id"],
            name="fk_reward_redemptions_reward_items_reward_item_id",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name="fk_reward_redemptions_users_decided_by"
        ),
        sa.CheckConstraint(
            "status IN "
            "('REQUESTED', 'UNDER_REVIEW', 'APPROVED', 'FULFILLED', 'REJECTED')",
            name="status",
        ),
        sa.CheckConstraint("points > 0", name="points"),
    )
    op.create_index(
        "ix_reward_redemptions_user_id",
        "reward_redemptions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_reward_redemptions_reward_item_id",
        "reward_redemptions",
        ["reward_item_id"],
        unique=False,
    )
    op.create_table(
        "point_reservations",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("redemption_id", sa.Uuid(), nullable=False),
        sa.Column("points", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_point_reservations"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_point_reservations_users_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["redemption_id"],
            ["reward_redemptions.id"],
            name="fk_point_reservations_reward_redemptions_redemption_id",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED', 'CONSUMED')", name="status"
        ),
        sa.CheckConstraint("points > 0", name="points"),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND released_at IS NULL) "
            "OR (status IN ('RELEASED', 'CONSUMED') AND released_at IS NOT NULL)",
            name="release_coherence",
        ),
        sa.UniqueConstraint(
            "redemption_id", name="uq_point_reservations_redemption_id"
        ),
    )
    op.create_index(
        "ix_point_reservations_user_id",
        "point_reservations",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_point_reservations_user_id", table_name="point_reservations")
    op.drop_table("point_reservations")
    op.drop_index(
        "ix_reward_redemptions_reward_item_id", table_name="reward_redemptions"
    )
    op.drop_index("ix_reward_redemptions_user_id", table_name="reward_redemptions")
    op.drop_table("reward_redemptions")
    op.drop_table("reward_items")
    op.drop_table("point_wallets")
    op.drop_index("ix_points_ledger_reversal_of_id", table_name="points_ledger")
    op.drop_index("ix_points_ledger_user_id", table_name="points_ledger")
    op.drop_table("points_ledger")
