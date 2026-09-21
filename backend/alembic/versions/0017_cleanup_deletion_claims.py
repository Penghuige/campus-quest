"""Cleanup deletion claims (PR #2 hardening pass 4b; spec §13/§27; the
cleanup TOCTOU closure ruling).

Two nullable timestamp columns turn the file-retention cleanup from a
boolean-snapshot pipeline into a declarative deletion claim:

- ``submissions.cleanup_claimed_at`` — the cleanup worker's claim of
  the DELETION RIGHT over this row's object. NULL while nobody holds
  it; set (to the claim instant) by the single conditional UPDATE that
  re-evaluates every §13/§27 guard against CURRENT committed state
  before taking the claim (single statement = guards and claim are
  atomic; the pre-fix scan snapshot could no longer be raced against).
  It stays set after a completed deletion (``deleted_at`` records the
  completion) and is cleared to release the claim after a provider
  failure so the next scan retries.
- ``upload_intents.cleanup_deleted_at`` — the orphan-intent cleanup's
  combined claim + done marker: the conditional UPDATE claims the
  intent (``expires_at`` past, never finalized, not yet claimed) and
  the same column doubles as the deletion record — for intents there
  is no separate fact row whose ``deleted_at`` could carry it, and no
  protection-side transition needs to distinguish claim-in-flight
  from done (finalize refuses intents at/after ``expires_at`` under
  the intent-row lock it already takes). A provider failure clears it
  (release); ``FileNotFoundError`` keeps it (idempotent success).

Why a claim and not a row lock: the S3 delete sits BETWEEN the claim
and the completion mark, and a row lock must never be held across a
provider call (the owner ruling). The claim is instead DECLARATIVE: the
protection writers (legal_hold, claim -> VALIDATING / UNDER_REVIEW)
reject with a typed 409 while an unfinished claim exists
(``cleanup_claimed_at IS NOT NULL AND deleted_at IS NULL``) —
protection must win, deletion is the retryable side (the claim window
is seconds). legal_hold has NO service write point today (operator /
DBA actions only): whoever sets it must retry on the 409 the same way;
recorded here because a migration is the frozen-history place for the
contract.

Indexing: none added. The claim is by primary key; the protection-side
EXISTS scans one claim's few submissions through the existing
``submissions.claim_id`` index.

Revision numbering: down_revision is 0015, NOT 0016 — 0016 is
pre-allocated to the parallel audit stream; the controller reparents
this revision onto 0016 at the merge.

Revision ID: 0017
Revises: 0015
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "cleanup_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Cleanup-worker deletion claim (hardening pass 4b): the single "
                "conditional UPDATE that re-evaluates every §13/§27 guard "
                "against CURRENT state sets this before the object delete — "
                "single statement, so guards and claim are atomic and the "
                "pre-claim scan snapshot is never raced against. NULL while "
                "unclaimed; cleared to release after a provider failure so the "
                "next scan retries; stays set after a completed deletion "
                "(deleted_at records the completion). Protection writers "
                "(legal_hold, claim -> VALIDATING/UNDER_REVIEW) must reject "
                "with a typed 409 while this is set with deleted_at NULL: "
                "protection must win, deletion is the retryable side. legal_hold "
                "itself has NO service write point today (operator/DBA action) "
                "— whoever sets it retries on that 409 the same way."
            ),
        ),
    )
    op.add_column(
        "upload_intents",
        sa.Column(
            "cleanup_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Orphan-intent cleanup claim + done marker (hardening pass 4b): "
                "the conditional UPDATE claiming the intent (expires_at past, "
                "never finalized, unclaimed) sets this, and the same column "
                "doubles as the deletion record — an intent has no separate "
                "fact row, and no protection transition races it (finalize "
                "refuses expired intents under the intent-row FOR UPDATE it "
                "already takes, so a claim and a finalize serialize on the "
                "row). Cleared to release after a provider failure; kept on "
                "FileNotFoundError (idempotent success). Cleanup only ever "
                "claims intents past expires_at: the presigned URL TTL is "
                "configured shorter than the intent TTL, so no legal PUT can "
                "land after expires_at."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("upload_intents", "cleanup_deleted_at")
    op.drop_column("submissions", "cleanup_claimed_at")
