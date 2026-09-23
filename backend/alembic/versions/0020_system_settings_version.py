"""System settings version column (Plan 08 T5: versioned setting
values).

``system_settings.version`` — a per-key optimistic counter, BIGINT NOT
NULL DEFAULT 1. Every ``SystemSettingService.set`` bumps it by one
(first write = 1 via the server default), and the change's
``SYSTEM_SETTING_UPDATED`` audit row carries the new value in
``details.version`` — so the audit trail can say "this row is the 7th
change of this key" without replaying the whole log.

Why a plain optimistic increment, not a compare-and-swap guard: the
write path is already serialized INSIDE the database (0015: INSERT ...
ON CONFLICT DO NOTHING, then the loser locks the winner's row FOR
UPDATE before updating). ``version`` observes that serialization, it
does not add a second concurrency mechanism — there is no
``WHERE version = :expected`` because there is no caller-supplied
expectation to honor (last-writer-wins remains the settings contract;
the §30 snapshot pair records what each write overwrote).

Existing rows backfill to 1 (``DEFAULT 1`` applies during ADD COLUMN);
their next write bumps to 2. No index, no constraint beyond NOT NULL:
the column is an observation channel for audit, not a lookup key.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "system_settings",
        sa.Column(
            "version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("system_settings", "version")
