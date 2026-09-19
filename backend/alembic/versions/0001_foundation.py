"""Foundation revision: app_metadata table.

A harmless key/value table that proves the migration pipeline end to end
(explicit constraint and index names, deterministic upgrade and downgrade).

Revision ID: 0001
Revises:
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_metadata",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_app_metadata"),
    )
    op.create_index(
        "ix_app_metadata_created_at",
        "app_metadata",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_app_metadata_created_at", table_name="app_metadata")
    op.drop_table("app_metadata")
