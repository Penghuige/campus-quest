"""
Identity: the refresh-rotation grace window timestamps.

`user_sessions.replaced_at` records WHEN a row was retired by rotation
(set atomically with `replaced_by`); `user_sessions.replay_envelope`
(added in 0023) carries the successor's encrypted refresh secret for
the stable-successor replay. Together they implement the OAuth
rotation-BCP grace window for PR #10: a retired token presented again
within REFRESH_GRACE_SECONDS resolves to the CURRENT live generation
and is re-issued — concurrent callers converge on one lineage.

DEFAULT 0 = strict rotate-once, unchanged product behavior. Flipping
the default accepts the section 5.6 contract amendment
(docs/superpowers/specs/2026-09-30-refresh-grace-amendment.md) — that
flip is the owner ruling, not an implementation detail.

Contract notes for psql readers:

- Rotation sets `revoked_at` AND `replaced_by` AND `replaced_at`
  together; logout revocation sets only `revoked_at`. Grace applies
  exclusively to the replaced (retired) shape, and `revoke_session`
  walks the chain to revoke the live tip — a stale generation's
  logout is never a silent no-op.
- Legacy rows have `replaced_by` set with `replaced_at` NULL: the
  service treats NULL as "retirement time unknown" = outside every
  window (fail closed; no backfill guess).
- The chain is acyclic (replaced_by is written once, always forward),
  which is what the unbounded logout walk relies on.
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_sessions",
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_sessions", "replaced_at")
