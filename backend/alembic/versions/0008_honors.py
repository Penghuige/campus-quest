"""Honors: fixed-rule honor definitions, grants, and the display honor.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (verified by `alembic check` after a clean
upgrade). CHECK constraints carry SHORT names ("honor_type"): the "ck"
convention interpolates %(constraint_name)s, so a full
"ck_honors_honor_type" here would render doubled (the 0003 gotcha).

Plan-numbering note: the plan drafted this as 0006_honors, but
0005_submissions, 0006_upload_intents, and 0007_points_rewards landed
first through earlier plans; this migration is 0008 by controller
ruling.

Contract notes for psql readers (no models.py there):

- `honors` holds DEFINITION rows. The five first-version auto honor
  types are frozen rules owned by
  rankings/honor_service.FIXED_HONOR_DEFINITIONS; auto rows are created
  lazily per definition — and per PERIOD for DAILY_RANK/MONTHLY_RANK
  (the 2026-09 月度 Top 3 is its own row, distinct from 2026-10's),
  which is what lets UNIQUE(user_id, honor_id) grants stay idempotent
  across periods. `is_auto = false` marks Admin commemorative rows
  (honor_type COMMEMORATIVE).
- The partial unique indexes over `is_auto` rows are the auto
  definition identity (honor_type, name, period). The plan drafted
  UNIQUE(honor_type, period) for periodic rows, but MONTHLY_RANK
  carries TWO definitions per period (rank 1 卷王 and rank<=3 Top 3,
  the spec §18 example list), so `name` discriminates. Admin rows sit
  outside both indexes — commemorative names may repeat.
- The `period_required` CHECK is biconditional: DAILY_RANK/MONTHLY_RANK
  MUST carry a period (spec §18: 周期性荣誉必须带 period，例如 2026-09)
  and every other type MUST NOT.
- `user_honors` is append-only grant history (no UPDATE path; honors
  are never revoked in V1). UNIQUE(user_id, honor_id) is the
  idempotency key replayed evaluations converge onto.
- `users.display_honor_id` is the single display honor a user may
  choose (spec §18: 只能设置一个 display_honor_id). Ownership is
  enforced by honor_service.set_display_honor (a UserHonor row must
  exist), deliberately not by a composite FK.
- NOTHING here touches points_ledger or the Redis ranking boards: an
  honor grant — including Admin commemorative grants — can never mutate
  ranking points (spec §18: 不允许该操作自动篡改排行榜积分).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "honors",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("honor_type", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("period", sa.String(length=16), nullable=True),
        sa.Column("is_auto", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_honors"),
        sa.CheckConstraint(
            "honor_type IN "
            "("
            "'TOTAL_COMPLETED', "
            "'ON_TIME_STREAK', "
            "'DAILY_RANK', "
            "'MONTHLY_RANK', "
            "'TOTAL_EARNED_POINTS', "
            "'COMMEMORATIVE'"
            ")",
            name="honor_type",
        ),
        sa.CheckConstraint(
            "(honor_type IN ('DAILY_RANK', 'MONTHLY_RANK')) = (period IS NOT NULL)",
            name="period_required",
        ),
    )
    op.create_index(
        "uq_honors_periodic_definition",
        "honors",
        ["honor_type", "name", "period"],
        unique=True,
        postgresql_where=sa.text("period IS NOT NULL AND is_auto"),
    )
    op.create_index(
        "uq_honors_lifetime_definition",
        "honors",
        ["honor_type", "name"],
        unique=True,
        postgresql_where=sa.text("period IS NULL AND is_auto"),
    )
    op.create_table(
        "user_honors",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("honor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_honors"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_honors_users_user_id"
        ),
        sa.ForeignKeyConstraint(
            ["honor_id"], ["honors.id"], name="fk_user_honors_honors_honor_id"
        ),
        sa.UniqueConstraint(
            "user_id", "honor_id", name="uq_user_honors_user_id_honor_id"
        ),
    )
    op.create_index("ix_user_honors_user_id", "user_honors", ["user_id"], unique=False)
    op.create_index(
        "ix_user_honors_honor_id", "user_honors", ["honor_id"], unique=False
    )
    op.add_column("users", sa.Column("display_honor_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_users_honors_display_honor_id",
        "users",
        "honors",
        ["display_honor_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_honors_display_honor_id", "users")
    op.drop_column("users", "display_honor_id")
    op.drop_index("ix_user_honors_honor_id", table_name="user_honors")
    op.drop_index("ix_user_honors_user_id", table_name="user_honors")
    op.drop_table("user_honors")
    op.drop_index("uq_honors_lifetime_definition", table_name="honors")
    op.drop_index("uq_honors_periodic_definition", table_name="honors")
    op.drop_table("honors")
