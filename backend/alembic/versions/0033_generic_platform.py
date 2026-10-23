"""The simplification batch (2026-10-23 spec §3.3): the generic platform.

One row, idempotent by ON CONFLICT — databases created before this
batch already hold the 0029 seeds, and a re-run (or a parallel stack)
must not fail. Sort order 1000 = the registry's tail (the UI appends
it after the seven shipped platforms).
"""

revision = "0033_generic_platform"
down_revision = "0032_invitation_revoked_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op

    op.execute(
        "INSERT INTO platforms (code, display_name, sort_order) "
        "VALUES ('generic', '通用', 1000) "
        "ON CONFLICT (code) DO NOTHING"
    )


def downgrade() -> None:
    from alembic import op

    # Only the row this migration owns; assignments referencing it would
    # refuse deletion by FK — the deliberate guard (published units are
    # content, not schema).
    op.execute("DELETE FROM platforms WHERE code = 'generic'")
