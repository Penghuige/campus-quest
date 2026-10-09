"""Private achievement preparation, separate from project overview.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ie_achievement_drafts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creation_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creation_payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column(
            "description", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("work_url", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "award_text", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ie_achievement_drafts")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["ie_project_drafts.id"],
            name=op.f("fk_ie_achievement_drafts_project_id_ie_project_drafts"),
        ),
        sa.UniqueConstraint(
            "project_id",
            "creation_request_id",
            name="uq_ie_achievement_drafts_project_request",
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120 AND btrim(title) <> ''",
            name=op.f("ck_ie_achievement_drafts_title_length"),
        ),
        sa.CheckConstraint(
            "char_length(description) <= 4000",
            name=op.f("ck_ie_achievement_drafts_description_length"),
        ),
        sa.CheckConstraint(
            "char_length(work_url) <= 2000 AND (work_url = '' OR work_url ~* '^https?://[^/]+')",
            name=op.f("ck_ie_achievement_drafts_work_url_format"),
        ),
        sa.CheckConstraint(
            "char_length(award_text) <= 1000",
            name=op.f("ck_ie_achievement_drafts_award_text_length"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_ie_achievement_drafts_version_positive")
        ),
        sa.CheckConstraint(
            "char_length(creation_payload_fingerprint) = 64",
            name=op.f("ck_ie_achievement_drafts_fingerprint_length"),
        ),
    )
    op.create_index(
        "ix_ie_achievement_drafts_project_updated",
        "ie_achievement_drafts",
        ["project_id", sa.text("updated_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_table("ie_achievement_drafts")
