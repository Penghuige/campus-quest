"""Claims: submission_schema_version goes nullable (§10.1 document tasks).

The claim-side snapshot column was NOT NULL by the §6.2 reasoning
"claiming requires a PUBLISHED task, which always carries a schema
version" — true until §10.1: a document-family task (DOCX/PDF only)
publishes with an EMPTY schema by contract, so its version is NULL and
the claim insert would violate the NOT NULL. The frontend's document
e2e surfaced the upstream gate (claim service refused with
missing_submission_schema_version before the insert was ever reached);
this migration removes the now-wrong constraint so a stored claim can
snapshot what the task actually carries.

No data movement: every existing row has a non-null version (all
pre-§10.1 tasks are structured).
"""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "assignment_claims",
        "submission_schema_version",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Safe only while no document-task claim exists; the pre-§10.1
    # world satisfies that by construction.
    op.alter_column(
        "assignment_claims",
        "submission_schema_version",
        existing_type=sa.Integer(),
        nullable=False,
    )
