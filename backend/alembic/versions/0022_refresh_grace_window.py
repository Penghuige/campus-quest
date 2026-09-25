"""Points: the refresh-rotation grace window timestamp.

`user_sessions.replaced_at` records WHEN a row was retired by rotation
(set atomically with `replaced_by`). SessionService.rotate_refresh uses
it for the OAuth refresh-token-rotation BCP: a retired token presented
again within REFRESH_GRACE_SECONDS (default 30, 0 disables) resumes the
chain tip instead of failing — covering non-browser clients that cannot
serialize their refreshes the way browser tabs now do via Web Locks
(the cross-tab race fix in PR #9). Outside the window the §5.6
rotate-once rejection stands unchanged; a logout-revoked tip is never
resurrected by grace.

Contract notes for psql readers:

- Rotation sets `revoked_at` AND `replaced_by` AND `replaced_at`
  together; logout revocation sets only `revoked_at`. Grace applies
  exclusively to the replaced (retired) shape.
- Legacy rows have `replaced_by` set with `replaced_at` NULL: the
  service treats NULL as "retirement time unknown" = outside every
  window (fail closed; no backfill guess).
- One lineage per login is the invariant: a grace resume rotates the
  CURRENT tip and re-links it, so the chain converges and never forks.
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
