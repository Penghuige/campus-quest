"""Wave 2a §4.2 migration: the invitation revocation timestamp.

REVOKED is a stored lifecycle fact (like ACCEPTED), not a derived one:
the admin surface needs the state to survive after the row's expiry
window has passed, so the flag cannot ride ``expires_at``. The accept
path rejects revoked rows explicitly — the token hash stays on the row
(one-way, superseded) so invitation history keeps its shape.
"""

import sqlalchemy as sa

from alembic import op

revision = "0032_invitation_revoked_at"
down_revision = "0031_last_admin_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "staff_invitations",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("staff_invitations", "revoked_at")
