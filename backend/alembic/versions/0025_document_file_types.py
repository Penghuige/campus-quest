"""Tasks: widen the allowed_file_types CHECK to the document family.

spec §10.1 (owner-approved 2026-10-09): the upload universe gains
DOCX and PDF as the document family — integrity-only machine check,
two-family exclusivity enforced at the service layer. The database
CHECK mirrors the enum universe (`_ALLOWED_FILE_TYPES` in
models.py); this migration widens the contained-by set so a
document-type task row is representable at all.

No data movement: existing rows all sit inside the structured family
and remain valid under the wider constraint.
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

#: The §10/§10.1 universe as the CHECK's contained-by set — one
#: constant per direction so the upgrade/downgrade pair is visibly
#: symmetric.
_DOCUMENT_UNIVERSE = "ARRAY['CSV', 'XLSX', 'SQLITE', 'DOCX', 'PDF']"
_STRUCTURED_UNIVERSE = "ARRAY['CSV', 'XLSX', 'SQLITE']"


def upgrade() -> None:
    op.drop_constraint("allowed_file_types", "tasks", type_="check")
    op.create_check_constraint(
        "allowed_file_types",
        "tasks",
        f"allowed_file_types <@ {_DOCUMENT_UNIVERSE}::varchar[]",
    )


def downgrade() -> None:
    op.drop_constraint("allowed_file_types", "tasks", type_="check")
    op.create_check_constraint(
        "allowed_file_types",
        "tasks",
        f"allowed_file_types <@ {_STRUCTURED_UNIVERSE}::varchar[]",
    )
