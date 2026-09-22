"""Durable audit log + redemption rejection reason (PR #2 hardening
step 7: P0-5 durable audit, final-review pts-F1).

Two changes:

1. ``audit_logs`` — the G12 durable audit table. V1 writes audit rows
   directly inside the caller's business transaction (controller
   ruling; no event-consumer pipeline — Plan 08's query/UI work may add
   one plus read-side indexes). Append-only by service convention: the
   sole INSERT path is ``AuditLogWriter.append`` (flush-only, the
   caller commits), there is no UPDATE/DELETE path, and the table
   carries no ``updated_at`` — the same deliberate no-database-trigger
   ruling as ``points_ledger`` (0007).

   ``actor_user_id`` deliberately has NO foreign key: an audit row must
   survive the deletion of the user it names (the ``points_ledger``
   polymorphic-source survival argument). ``actor_role`` is a snapshot
   in plain VARCHAR — frozen history must not break when the role
   vocabulary grows, so no CHECK against today's member set.
   ``target_type``/``target_id`` are polymorphic strings (UUID text or
   business key), the ledger ``source_type``/``source_id`` precedent.
   ``details`` is JSONB (structured context; JSON-serializable values).

   No secondary indexes in V1: the table is write-path only until Plan
   08 builds the audit query/UI surface, which adds the indexes its
   read patterns need in their own migration.

2. ``reward_redemptions.rejection_reason TEXT NULL`` — final-review
   pts-F1: the mandatory reject reason was validated and then dropped.
   The service now persists it on the reject transition (exactly REJECTED
   rows carry a value); the staff review DTO exposes it, the student DTO
   does not. Nullable, no backfill: historical REJECTED rows keep NULL
   (their reasons are unrecoverable).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # No FK on purpose: the audit row outlives the actor's account
        # (see the migration docstring).
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.add_column(
        "reward_redemptions",
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reward_redemptions", "rejection_reason")
    op.drop_table("audit_logs")
