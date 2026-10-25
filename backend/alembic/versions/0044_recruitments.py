"""Recruitment drafts, immutable revisions, revocable contacts and receipts."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0044_recruitments"
down_revision = "0043_review_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ie_recruitment_drafts",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("creation_request_id", sa.Uuid(), nullable=False),
        sa.Column("creation_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("moderation_state", sa.String(length=16), nullable=False),
        sa.Column("public_revision_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_remind_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "fields->>'headcount' IS NULL OR "
            "(jsonb_typeof(fields->'headcount') = 'number' "
            "AND (fields->>'headcount')::numeric BETWEEN 1 AND 999 "
            "AND (fields->>'headcount')::numeric "
            "= trunc((fields->>'headcount')::numeric))",
            name=op.f("ck_ie_recruitment_drafts_headcount_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fields) = 'object' "
            "AND jsonb_typeof(fields->'contacts') = 'array' "
            "AND jsonb_array_length(fields->'contacts') <= 4",
            name=op.f("ck_ie_recruitment_drafts_fields_shape"),
        ),
        sa.CheckConstraint(
            "moderation_state IN ('NORMAL','TAKEN_DOWN')",
            name=op.f("ck_ie_recruitment_drafts_moderation_valid"),
        ),
        sa.CheckConstraint(
            "status = 'DRAFT' OR "
            "(public_revision_id IS NOT NULL AND cycle_id IS NOT NULL)",
            name=op.f("ck_ie_recruitment_drafts_published_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','OPEN','CLOSED','EXPIRED')",
            name=op.f("ck_ie_recruitment_drafts_status_valid"),
        ),
        sa.CheckConstraint(
            "char_length(creation_fingerprint) = 64",
            name=op.f("ck_ie_recruitment_drafts_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_ie_recruitment_drafts_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["ie_project_drafts.id"],
            name=op.f("fk_ie_recruitment_drafts_ie_project_drafts_project_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ie_recruitment_drafts")),
        sa.UniqueConstraint(
            "project_id", "creation_request_id", name="uq_ie_recruitment_creation"
        ),
    )
    op.create_index(
        "ix_ie_recruitment_deadline",
        "ie_recruitment_drafts",
        ["deadline_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'OPEN' AND moderation_state = 'NORMAL'"),
    )
    op.create_index(
        "ix_ie_recruitment_due",
        "ie_recruitment_drafts",
        ["next_remind_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'OPEN' AND moderation_state = 'NORMAL'"),
    )
    op.create_index(
        "ix_ie_recruitment_project",
        "ie_recruitment_drafts",
        ["project_id", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_ie_recruitment_public_revision",
        "ie_recruitment_drafts",
        ["public_revision_id", "id"],
        unique=False,
    )
    op.create_table(
        "ie_recruitment_receipts",
        sa.Column("recruitment_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("applied_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('update','publish','close','reopen',"
            "'confirm_active','revoke_contact')",
            name=op.f("ck_ie_recruitment_receipts_action_valid"),
        ),
        sa.CheckConstraint(
            "applied_version > 0",
            name=op.f("ck_ie_recruitment_receipts_version_positive"),
        ),
        sa.CheckConstraint(
            "char_length(fingerprint) = 64",
            name=op.f("ck_ie_recruitment_receipts_fingerprint_length"),
        ),
        sa.ForeignKeyConstraint(
            ["recruitment_id"],
            ["ie_recruitment_drafts.id"],
            name=op.f(
                "fk_ie_recruitment_receipts_ie_recruitment_drafts_recruitment_id"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "recruitment_id", "request_id", name=op.f("pk_ie_recruitment_receipts")
        ),
    )
    op.create_table(
        "ie_recruitment_revisions",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("recruitment_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(content->>'headcount')::integer BETWEEN 1 AND 999",
            name=op.f("ck_ie_recruitment_revisions_headcount_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object' AND NOT content ? 'contacts'",
            name=op.f("ck_ie_recruitment_revisions_contact_free"),
        ),
        sa.CheckConstraint(
            "number > 0", name=op.f("ck_ie_recruitment_revisions_number_positive")
        ),
        sa.ForeignKeyConstraint(
            ["recruitment_id"],
            ["ie_recruitment_drafts.id"],
            name=op.f(
                "fk_ie_recruitment_revisions_ie_recruitment_drafts_recruitment_id"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ie_recruitment_revisions")),
        sa.UniqueConstraint(
            "id", "recruitment_id", name="uq_ie_recruitment_revision_identity"
        ),
        sa.UniqueConstraint(
            "recruitment_id", "number", name="uq_ie_recruitment_revision_number"
        ),
    )
    op.create_foreign_key(
        "fk_ie_recruitment_public",
        "ie_recruitment_drafts",
        "ie_recruitment_revisions",
        ["public_revision_id", "id"],
        ["id", "recruitment_id"],
    )
    op.create_table(
        "ie_recruitment_contacts",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("recruitment_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('WECHAT','QQ','EMAIL','PHONE')",
            name=op.f("ck_ie_recruitment_contacts_channel_valid"),
        ),
        sa.CheckConstraint(
            "char_length(value) BETWEEN 1 AND 254",
            name=op.f("ck_ie_recruitment_contacts_value_length"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= authorized_at",
            name=op.f("ck_ie_recruitment_contacts_revocation_time"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "recruitment_id"],
            ["ie_recruitment_revisions.id", "ie_recruitment_revisions.recruitment_id"],
            name="fk_ie_recruitment_contact_revision",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ie_recruitment_contacts")),
        sa.UniqueConstraint(
            "revision_id", "channel", name="uq_ie_recruitment_contact_channel"
        ),
    )
    op.create_index(
        "ix_ie_recruitment_contact_parent",
        "ie_recruitment_contacts",
        ["recruitment_id", "revision_id"],
        unique=False,
    )
    # Literal schema bounds, deliberately independent of application imports.
    bounds = {
        "project_title": 120,
        "direction": 120,
        "stage": 80,
        "introduction": 2000,
        "goal": 2000,
        "initial_plan": 2000,
        "role_name": 120,
        "duties": 2000,
        "requirements": 2000,
        "time_commitment": 500,
    }
    for table, column in (
        ("ie_recruitment_drafts", "fields"),
        ("ie_recruitment_revisions", "content"),
    ):
        for key, limit in bounds.items():
            op.create_check_constraint(
                op.f(f"ck_{table}_{key}_length"),
                table,
                f"{column} ? '{key}' "
                f"AND jsonb_typeof({column}->'{key}') = 'string' "
                f"AND char_length({column}->>'{key}') <= {limit}",
            )
    for table in ("ie_recruitment_revisions", "ie_recruitment_receipts"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ie_immutable_revision()"
        )
    op.execute("""CREATE FUNCTION ie_guard_recruitment_contact() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF ROW(OLD.id, OLD.recruitment_id, OLD.revision_id, OLD.channel,
               OLD.value, OLD.authorized_at)
           IS DISTINCT FROM
           ROW(NEW.id, NEW.recruitment_id, NEW.revision_id, NEW.channel,
               NEW.value, NEW.authorized_at)
           OR (OLD.revoked_at IS NOT NULL
               AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
        THEN RAISE EXCEPTION 'contact authorization is immutable'; END IF;
        RETURN NEW; END; $$""")
    op.execute("""CREATE TRIGGER ie_recruitment_contact_immutable BEFORE UPDATE
        ON ie_recruitment_contacts FOR EACH ROW
        EXECUTE FUNCTION ie_guard_recruitment_contact()""")


def downgrade() -> None:
    op.drop_constraint(
        "fk_ie_recruitment_public", "ie_recruitment_drafts", type_="foreignkey"
    )
    op.drop_index(
        "ix_ie_recruitment_contact_parent", table_name="ie_recruitment_contacts"
    )
    op.drop_table("ie_recruitment_contacts")
    op.execute("DROP FUNCTION ie_guard_recruitment_contact()")
    op.drop_table("ie_recruitment_revisions")
    op.drop_table("ie_recruitment_receipts")
    op.drop_index(
        "ix_ie_recruitment_public_revision", table_name="ie_recruitment_drafts"
    )
    op.drop_index("ix_ie_recruitment_project", table_name="ie_recruitment_drafts")
    op.drop_index(
        "ix_ie_recruitment_due",
        table_name="ie_recruitment_drafts",
        postgresql_where=sa.text("status = 'OPEN' AND moderation_state = 'NORMAL'"),
    )
    op.drop_index(
        "ix_ie_recruitment_deadline",
        table_name="ie_recruitment_drafts",
        postgresql_where=sa.text("status = 'OPEN' AND moderation_state = 'NORMAL'"),
    )
    op.drop_table("ie_recruitment_drafts")
