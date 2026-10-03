"""Identity: the avatar object-key column.

``users.avatar_object_key`` (nullable TEXT) points at the account's
avatar object in storage (spec amendment D1, QA defect #4; the key is
adapter-minted ``avatars/{user_id}/{uuid}.{ext}``). NULL means the
account shows the frontend's generated-initial default — the seeded
world ships NULL for everyone, so the column is purely additive.

Privacy contract pinned elsewhere and enforced by the service/DTO
layers: the key is storage plumbing only. It never enters any DTO,
log line, or DOM; display goes through the ``/users/{id}/avatar`` byte
proxy, and audit rows record the change event, never the key.
"""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("avatar_object_key", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "avatar_object_key")
