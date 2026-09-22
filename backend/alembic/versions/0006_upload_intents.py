"""Submissions: single-use presigned upload intents.

Constraint and index names are explicit and match the naming-convention
output of the ORM metadata (verified by `alembic check` after a clean
upgrade). CHECK constraints carry SHORT names ("declared_type"): the
"ck" convention interpolates %(constraint_name)s, so a full
"ck_upload_intents_declared_type" here would render doubled (the
0002/0003/0005 gotcha).

Intent lifecycle (spec §10 steps 1-5, §32), stated here because psql
readers of these columns have no models.py:

- One row per issued presigned grant: the server-generated object key
  (UNIQUE — one stored object backs at most one intent), the declared
  type/size finalize must re-verify, and the sanitized display filename
  (spec §10: display-only metadata, never a path).
- `expires_at` bounds the grant; finalize refuses at/after it.
- `consumed_at` is the single-use marker: set exactly once, on success
  (together with `finalized_submission_id`) or on the burn path (a
  stored object that contradicted the declared metadata — consumed with
  NO submission, remedy is a fresh intent). The coherence CHECK makes
  "finalized but never consumed" unrepresentable.
- `finalized_submission_id` is the idempotent-replay pointer (spec §32):
  replays of a successful finalize return THAT submission, never a
  version N+1.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_intents",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("declared_type", sa.String(length=16), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_submission_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_upload_intents"),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["assignment_claims.id"],
            name="fk_upload_intents_assignment_claims_claim_id",
        ),
        sa.ForeignKeyConstraint(
            ["finalized_submission_id"],
            ["submissions.id"],
            name="fk_upload_intents_submissions_finalized_submission_id",
        ),
        sa.CheckConstraint(
            "declared_type IN ('CSV', 'XLSX', 'SQLITE')", name="declared_type"
        ),
        sa.CheckConstraint("declared_size >= 0", name="declared_size"),
        sa.CheckConstraint(
            "finalized_submission_id IS NULL OR consumed_at IS NOT NULL",
            name="finalized_implies_consumed",
        ),
        sa.UniqueConstraint("object_key", name="uq_upload_intents_object_key"),
    )
    op.create_index(
        "ix_upload_intents_claim_id", "upload_intents", ["claim_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_upload_intents_claim_id", table_name="upload_intents")
    op.drop_table("upload_intents")
