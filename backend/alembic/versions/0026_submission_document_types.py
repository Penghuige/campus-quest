"""Submissions: the document family joins the type CHECKs (spec §10.1).

The declared/detected type CHECKs on both ``upload_intents`` and
``submissions`` pin the closed universe; PR #54 widened the enum and
the tasks-side allowed set but the type CHECKs still refused DOCX/PDF
rows — surfaced by the first integration test seeding a DOCX
submission. Both tables widen symmetrically (the intent carries the
declaration, the row the outcome).
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_STRUCTURED = "('CSV', 'XLSX', 'SQLITE')"
_DOCUMENT = "('CSV', 'XLSX', 'SQLITE', 'DOCX', 'PDF')"


def _widen(table: str, types: str, *, has_detected: bool) -> None:
    op.drop_constraint("declared_type", table, type_="check")
    op.create_check_constraint("declared_type", table, f"declared_type IN {types}")
    if not has_detected:
        return
    op.drop_constraint("detected_type", table, type_="check")
    op.create_check_constraint(
        "detected_type",
        table,
        f"detected_type IS NULL OR detected_type IN {types}",
    )


def upgrade() -> None:
    _widen("upload_intents", _DOCUMENT, has_detected=False)
    _widen("submissions", _DOCUMENT, has_detected=True)


def downgrade() -> None:
    _widen("upload_intents", _STRUCTURED, has_detected=False)
    _widen("submissions", _STRUCTURED, has_detected=True)
