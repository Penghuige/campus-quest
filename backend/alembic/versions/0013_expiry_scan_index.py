"""Expiry-scan partial index on assignment_claims (PR #2 hardening,
MERGE_CARRIES item 4 + final-review N5; spec §26).

The claim-expiry scan (``app.workers.jobs.expire_claims
.collect_due_claim_ids``) reads:

    WHERE status IN ('CLAIMED', 'REVISION_REQUIRED')   -- actionable
      AND grace_deadline_at <= :now
      AND (revision_deadline_at IS NULL
           OR revision_deadline_at <= :now)
    ORDER BY grace_deadline_at, id
    LIMIT :batch

i.e. ``now >= GREATEST(grace_deadline_at, revision_deadline_at)`` over
the actionable set. No existing index serves it (0003 gives the claims
table only FK lookups and the two active-set partial UNIQUEs), so the
due scan was a sequential scan.

WHY THIS PREDICATE and not the originally deferred one: the S3-branch
carry planned ``(grace_deadline_at) WHERE status IN (actionable) AND
(revision IS NULL OR revision <= grace)`` — but that predicate EXCLUDES
exactly the rows a review extended past grace (``revision > grace AND
revision <= now``, the "宽限被延长" rows, which are among the closest to
expiry and thus the scan's most frequent hits). Those rows would fall
back to a sequential scan precisely when the index was built for them
(final-review finding N5).

The index below keeps the partial predicate at the actionable-status
set only — every row the WHERE can match is indexed, both deadline
shapes — and keys ``(grace_deadline_at, id)`` so the ORDER BY + LIMIT
comes back in index order. The revision conjunct stays a filter over
the (bounded, batch-limited) candidate rows.

Index-only: no column changes.

The downgrade drops the index only.

Revision ID: 0013
Revises: 0010
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_assignment_claims_expiry_due"

# Mirrors claim_service.EXPIRY_ACTIONABLE_STATUSES (CLAIMED,
# REVISION_REQUIRED) as literal strings: migrations are frozen history
# and must not import live code (the 0003 convention).
_ACTIONABLE_STATUSES = "status IN ('CLAIMED', 'REVISION_REQUIRED')"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "assignment_claims",
        ["grace_deadline_at", "id"],
        unique=False,
        postgresql_where=sa.text(_ACTIONABLE_STATUSES),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="assignment_claims")
