"""Admin adjustments must carry a reason (spec §15; the T1 review fold).

`points_ledger` rows of ledger_type ADMIN_ADJUSTMENT are the manual
correction channel, and spec §15 makes their reason mandatory ("Admin
调整必须有 reason"). 0007 modeled `reason` as nullable (other types
carry it only as optional context) and left the requirement to the
service boundary; this migration adds the database backstop the spec's
wording promises, so an adjustment without a reason is unrepresentable
even when the service forgets (backend-engineering §6).

The CHECK is additive and validated per-row against existing data:
every ADMIN_ADJUSTMENT row written through the current (service-gated)
paths already carries a reason, and the table is empty in every
deployed environment at this point in the plan sequence, so no backfill
is needed.

Constraint naming follows the 0003/0004 gotcha: alembic ops compose the
short name with the `ck_%(table_name)s_%(constraint_name)s` convention
(verified by `alembic check` after the upgrade), so both operations
below pass "admin_reason_required" and address the live constraint
`ck_points_ledger_admin_reason_required` — the same name the ORM
metadata's CheckConstraint in models.py composes.

Revision ID: 0011
Revises: 0008
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "admin_reason_required"
_CONDITION = "ledger_type <> 'ADMIN_ADJUSTMENT' OR reason IS NOT NULL"


def upgrade() -> None:
    op.create_check_constraint(_CONSTRAINT_NAME, "points_ledger", _CONDITION)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "points_ledger", type_="check")
