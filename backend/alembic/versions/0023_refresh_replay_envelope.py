"""Points: the stable-successor replay envelope column.

`user_sessions.replay_envelope` (nullable TEXT) carries the successor
generation's refresh secret, Fernet-encrypted with a `replay:v1:`
domain prefix, written onto the retiring row in the same rotation
transaction (PR #10 review rework: the first grace design rotated the
chain tip again on replay, which instantly invalidated the earlier
caller's freshly issued generation — `find_with_live_session` requires
`replaced_by IS NULL` — converting the race into a chain of mutually
invalidating credentials).

The envelope inverts that: a within-window replay resolves THROUGH the
envelope chain to the CURRENT live generation and re-issues it (same
refresh secret, freshly minted access token bound to the live row).
No rotation happens on replay, so N concurrent callers converge on ONE
live generation.

Contract notes for psql readers:

- Written only when REFRESH_GRACE_SECONDS > 0 (default 0 — strict
  rotate-once, no envelopes, no behavior change until the §5.6
  contract amendment is owner-approved; flipping the default IS the
  ruling).
- Reads require: presenting row retired by rotation (replaced_by set),
  replaced_at inside the window, envelope decryptable, and every walked
  successor still rotation-alive (a logout-revoked tip refuses).
- Envelopes are bearer secrets at rest: encrypted with the deployment
  Fernet key, domain-prefixed, never logged, never in any DTO.
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_sessions",
        sa.Column("replay_envelope", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_sessions", "replay_envelope")
