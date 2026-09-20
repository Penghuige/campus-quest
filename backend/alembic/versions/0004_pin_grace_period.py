"""Pin the V1 grace period to exactly 1440 minutes (spec §6).

V1 fixes the grace period at 24 hours and exposes no product control
over it: every write path sets `DEFAULT_GRACE_PERIOD_MINUTES` (1440) and
the column's server_default is 1440, so every existing row already
satisfies `= 1440` and the constraint swap below is safe without a
table rewrite. If the product later makes the grace period configurable,
the migration introducing that feature relaxes this constraint back to
the value set it needs.

Constraint names are exact: 0003 created the CHECK through the ORM
naming convention, so the live name is `ck_tasks_grace_period_minutes`
(short name "grace_period_minutes" composed with `ck_%(table_name)s_
%(constraint_name)s`). Alembic ops apply that same convention to both
`drop_constraint` and `create_check_constraint` (passing the composed
name renders it doubled), so both operations below pass the short name
and produce the identical database name; `alembic check` stays clean.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "grace_period_minutes"
_V1_PINNED = "grace_period_minutes = 1440"
_PREVIOUS = "grace_period_minutes > 0"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "tasks", type_="check")
    op.create_check_constraint(_CONSTRAINT_NAME, "tasks", _V1_PINNED)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "tasks", type_="check")
    op.create_check_constraint(_CONSTRAINT_NAME, "tasks", _PREVIOUS)
