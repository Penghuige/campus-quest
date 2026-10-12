"""Persist original private talent upload signing contracts for safe replay."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No prior talent file upload API exists; still tolerate any historical
    # internal rows by giving them an already-expired, unusable signing contract.
    op.add_column(
        "ie_talent_files",
        sa.Column("upload_url", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "ie_talent_files",
        sa.Column(
            "upload_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("'1970-01-01T00:00:00Z'::timestamptz"),
        ),
    )
    op.add_column(
        "ie_talent_files",
        sa.Column(
            "client_headers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    for column in ("upload_url", "upload_expires_at", "client_headers"):
        op.alter_column("ie_talent_files", column, server_default=None)
    op.execute("""CREATE FUNCTION ie_talent_file_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'talent file history is immutable';
        END IF;
        IF ROW(NEW.id, NEW.profile_id, NEW.creation_request_id,
               NEW.creation_fingerprint, NEW.name, NEW.object_key,
               NEW.content_type, NEW.size, NEW.created_at, NEW.upload_url,
               NEW.upload_expires_at, NEW.client_headers)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.profile_id, OLD.creation_request_id,
               OLD.creation_fingerprint, OLD.name, OLD.object_key,
               OLD.content_type, OLD.size, OLD.created_at, OLD.upload_url,
               OLD.upload_expires_at, OLD.client_headers) THEN
            RAISE EXCEPTION 'talent file metadata is immutable';
        END IF;
        IF (OLD.removed_at IS NOT NULL AND
            NEW.removed_at IS DISTINCT FROM OLD.removed_at)
           OR (OLD.revoked_at IS NOT NULL AND
               NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
           THEN RAISE EXCEPTION 'talent file tombstone is permanent';
        END IF;
        IF OLD.state IN ('READY','REJECTED') AND
           ROW(NEW.state, NEW.sha256, NEW.failure_code) IS DISTINCT FROM
           ROW(OLD.state, OLD.sha256, OLD.failure_code) THEN
            RAISE EXCEPTION 'talent file checked result is immutable';
        END IF;
        RETURN NEW; END; $$""")
    op.execute(
        "CREATE TRIGGER ie_talent_file_guard BEFORE UPDATE OR DELETE "
        "ON ie_talent_files FOR EACH ROW EXECUTE FUNCTION ie_talent_file_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ie_talent_file_guard ON ie_talent_files")
    op.execute("DROP FUNCTION IF EXISTS ie_talent_file_guard()")
    for column in ("client_headers", "upload_expires_at", "upload_url"):
        op.drop_column("ie_talent_files", column)
