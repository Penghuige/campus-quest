"""AuditLog reaches spec §30's snapshot contract (PR #2 hardening pass
4a: G11/G12 — the controller's "补齐到 §30" ruling).

Four nullable columns on ``audit_logs``:

- ``before_snapshot`` / ``after_snapshot`` (JSONB) — the target's
  state migration as REDACTED structured JSON (statuses, amounts,
  visibility facts; the G11 rule: never nickname/phone/email/student
  ids). §30 says "at least" these columns, so the existing ``details``
  stays as free additional context beside them — the migration does
  not move or rewrite existing rows: NULL snapshots on history are
  valid (the writer predates the columns), and audit history is
  append-only anyway (no backfill of frozen rows — the 0014 ruling).
- ``ip_address`` (VARCHAR(64)) / ``request_id`` (VARCHAR(128)) — the
  request-scoped correlation pair threaded from the router through
  ``AuditContext`` (the request_id width matches
  ``core.observability.MAX_REQUEST_ID_LENGTH``). Both NULL on
  non-HTTP callers (workers, tests) — that is the documented default,
  not a gap.

No new indexes: the audit read side is Plan 08's query work, which
gets its own migration when the access patterns are known (the 0014
deferred-index ruling).

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("before_snapshot", JSONB(), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("after_snapshot", JSONB(), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("ip_address", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("request_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_logs", "request_id")
    op.drop_column("audit_logs", "ip_address")
    op.drop_column("audit_logs", "after_snapshot")
    op.drop_column("audit_logs", "before_snapshot")
