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

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("allowed_file_types", "tasks", type_="check")
    op.create_check_constraint(
        "allowed_file_types",
        "tasks",
        "allowed_file_types <@ ARRAY['CSV', 'XLSX', 'SQLITE', 'DOCX', 'PDF']::varchar[]",
    )


def downgrade() -> None:
    op.drop_constraint("allowed_file_types", "tasks", type_="check")
    op.create_check_constraint(
        "allowed_file_types",
        "tasks",
        "allowed_file_types <@ ARRAY['CSV', 'XLSX', 'SQLITE']::varchar[]",
    )
