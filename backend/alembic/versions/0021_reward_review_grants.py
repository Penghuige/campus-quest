"""Points: the RewardReviewGrant table — the scoped-delegation landing
for redemption review (Plan 08 T4; spec §4.2 "按管理员授权审核相关
RewardRedemption").

The PR #2 hardening ruling (P0-4) made the redemption review surfaces
Admin-only "until scoped delegation lands"; this revision is that
delegation's carrier. Row EXISTS = the Teacher holds the global
REWARD_REVIEW authorization; revocation is a DELETE, so the per-request
guard read (no cache, no TTL) keeps revocation immediate. Grant history
lives in audit_logs (REWARD_REVIEW_GRANTED/_REVOKED), never here.

Contract notes for psql readers (no models.py there):

- `teacher_id` is the primary key: at most one live grant per account;
  a racing second grant loses to the PK, which the service translates
  into its typed duplicate conflict.
- `capability` is a closed vocabulary in the TaskCollaborator CHECK
  style; V1 holds exactly 'REWARD_REVIEW'. Widening it is a migration +
  owner ruling (G13), never a service-layer string.
- No FK ondeleted behavior: the collaborator-table precedent (plain
  FKs to users.id — account deletion is not a V1 flow).

Branch note (controller): T5's system-settings wave pre-allocates
revision 0020 in its own worktree; this wave branches 0021 directly
off 0019 so both merge independently, and the controller reparents
0021 onto 0020 when the waves land together. DO NOT add a 0020 here.

Revision ID: 0021
Revises: 0019
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reward_review_grants",
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=32), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("teacher_id", name="pk_reward_review_grants"),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
            name="fk_reward_review_grants_users_teacher_id",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name="fk_reward_review_grants_users_granted_by",
        ),
        sa.CheckConstraint("capability IN ('REWARD_REVIEW')", name="capability"),
    )


def downgrade() -> None:
    op.drop_table("reward_review_grants")
