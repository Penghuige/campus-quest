"""Wallet overdraft: reversals of spent rewards may drive available_points
negative (plan 05 task 5 controller ruling — USER VETO POINT AT PR).

0007 enforced `available_points >= 0` on point_wallets as the §31.12
spendable-balance backstop. Task 5's reversal use case breaks that
reading of the spec, and the spec itself never promised it:

- Spec §17.2 (冲销): a confirmed-cheating reward is corrected by posting a
  negative PointsLedger entry — 不应自动扣用户历史积分；若已经错误发放则通过
  反向流水冲销 — and the entry must exist even when the student already
  SPENT the points. The wallet projection must follow the ledger exactly
  (models.py: the projection is rebuildable as SUM(amount) WHERE
  affects_balance), so a -200 reversal against a 50-point available
  balance lands at -150. Clamping to 0 would silently break the
  ledger==wallet invariant; rejecting would leave the mandated reversal
  unrepresentable.
- Spec §31.12's actual wording is narrower: "Reward redemption 不能使
  spendable points 为负" — REDEMPTION cannot make spendable points
  negative. That protection is enforced where the decision is made: the
  redemption-service gate recomputes spendable under the wallet row FOR
  UPDATE lock before freezing points (plan 05 task 4), the same lock
  every ledger post takes, so no spending path can exploit a negative
  balance.

RULING: drop `ck_point_wallets_available_points` entirely (BigInteger
column, no lower bound); `ck_point_wallets_earned_points` (>= 0) stays —
earned only ever accumulates positive task contributions (spec §15.1),
so a negative earned value would be corruption, not policy.

Constraint naming follows the 0003/0004 gotcha: alembic ops compose the
short name with the `ck_%(table_name)s_%(constraint_name)s` convention,
so the operations below pass "available_points" and address the live
constraint `ck_point_wallets_available_points` — the same name the ORM
metadata in models.py (which drops the CheckConstraint) no longer
composes; `alembic check` stays clean after the upgrade.

The downgrade re-creates the CHECK and will fail while any wallet row
is overdrafted — intentionally: un-applying this ruling first requires
deciding what happens to those reversals.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "available_points"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, "point_wallets", type_="check")


def downgrade() -> None:
    op.create_check_constraint(
        _CONSTRAINT_NAME, "point_wallets", "available_points >= 0"
    )
