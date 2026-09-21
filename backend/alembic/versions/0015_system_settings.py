"""System settings current-value store (PR #2 hardening step 8: the
admin-configurable, audited CURRENT_ACADEMIC_TERM).

``system_settings`` — one row per known setting key, holding the
CURRENT value. This table is deliberately NOT append-only (the opposite
of ``audit_logs``, 0014): UPDATE is the normal write path because each
row is the present state of one configuration knob, and the HISTORY of
every change lives in ``audit_logs`` — one ``SYSTEM_SETTING_UPDATED``
row per applied write, committed in the same transaction by
``SystemSettingService.set`` with the old value preserved in
``details.old_value``. The database enforces nothing here for the same
reason as everywhere: the discipline is the single writer path, not a
trigger.

``updated_by_user_id`` carries NO foreign key on purpose: the row
records who last set the value, and that attribution must survive the
actor's account deletion (the ``audit_logs.actor_user_id`` ruling).

No seed rows and no secondary indexes: a missing row is the meaningful
state "not configured here — use the deployment seed" (G7: the settings
row is the fact, the env var CURRENT_ACADEMIC_TERM is the initial
seed), read per request by points' ``SystemAcademicTermProvider`` with
``Settings.current_academic_term`` as the fallback.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        # No FK on purpose: the attribution survives the actor's account
        # deletion (see the migration docstring).
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
