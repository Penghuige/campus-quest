"""Private innovation project preparation, separate from Tasks and publication.

The original creation fingerprint remains immutable when draft content is
edited so a delayed create retry cannot overwrite the user's later work.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ie_project_drafts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creation_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creation_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("direction", sa.String(120), server_default="", nullable=False),
        sa.Column("stage", sa.String(80), server_default="", nullable=False),
        sa.Column("team_status", sa.Text(), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ie_project_drafts")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_ie_project_drafts_users_owner_user_id"),
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "creation_request_id",
            name="uq_ie_project_drafts_owner_request",
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120 AND btrim(title) <> ''",
            name=op.f("ck_ie_project_drafts_title_length"),
        ),
        sa.CheckConstraint(
            "char_length(summary) <= 2000",
            name=op.f("ck_ie_project_drafts_summary_length"),
        ),
        sa.CheckConstraint(
            "char_length(direction) <= 120",
            name=op.f("ck_ie_project_drafts_direction_length"),
        ),
        sa.CheckConstraint(
            "char_length(stage) <= 80",
            name=op.f("ck_ie_project_drafts_stage_length"),
        ),
        sa.CheckConstraint(
            "char_length(team_status) <= 1000",
            name=op.f("ck_ie_project_drafts_team_status_length"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_ie_project_drafts_version_positive"),
        ),
        sa.CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64",
            name=op.f("ck_ie_project_drafts_fingerprint_length"),
        ),
    )
    op.create_index(
        "ix_ie_project_drafts_owner_updated_at",
        "ie_project_drafts",
        ["owner_user_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ie_project_drafts_owner_updated_at", table_name="ie_project_drafts"
    )
    op.drop_table("ie_project_drafts")
