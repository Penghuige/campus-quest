"""Private bounded innovation evidence and attempt leases."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ie_achievement_evidence",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "achievement_id",
            sa.UUID(),
            sa.ForeignKey("ie_achievement_drafts.id"),
            nullable=False,
        ),
        sa.Column("creation_request_id", sa.UUID(), nullable=False),
        sa.Column("creation_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(40), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("sha256", sa.String(64)),
        sa.Column("failure_code", sa.String(40)),
        sa.Column("check_token", sa.UUID()),
        sa.Column("checking_until", sa.DateTime(timezone=True)),
        sa.Column("upload_url", sa.Text(), nullable=False),
        sa.Column("client_headers", postgresql.JSONB(), nullable=False),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "referenced", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "achievement_id",
            "creation_request_id",
            name="uq_ie_evidence_creation_request",
        ),
        sa.UniqueConstraint("object_key", name="uq_ie_evidence_object_key"),
        sa.CheckConstraint("size BETWEEN 1 AND 10485760", name="size_bound"),
        sa.CheckConstraint(
            "content_type IN ('application/pdf','image/png','image/jpeg')",
            name="content_type",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING','CHECKING','READY','REJECTED')", name="state"
        ),
        sa.CheckConstraint(
            "(state = 'READY') = (sha256 IS NOT NULL)", name="ready_hash"
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR char_length(sha256) = 64", name="hash_length"
        ),
        sa.CheckConstraint(
            "(state = 'CHECKING') = (check_token IS NOT NULL) "
            "AND (state = 'CHECKING') = (checking_until IS NOT NULL)",
            name="check_lease",
        ),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64", name="fingerprint_length"
        ),
    )
    op.create_index(
        "ix_ie_evidence_achievement_created",
        "ie_achievement_evidence",
        ["achievement_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("ie_achievement_evidence")
