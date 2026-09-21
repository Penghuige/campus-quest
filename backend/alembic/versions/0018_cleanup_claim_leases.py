"""Cleanup claim leases + takeover (PR #2 hardening pass 5a, P0-1/P0-2;
spec §13/§27; the unified serialization boundary ruling).

Pass 4b made the deletion right a declarative claim, but the claim had
no lease: a worker crashing between the claim commit and the S3 delete
stranded the row forever (claimed, never re-claimed, protection writers
409 forever — P0-2), and the claim's single-statement WHERE could still
commit between a protection transaction's guard check and its status
commit because the two sides serialized on different locks (P0-1). This
revision adds the lease columns both fixes need:

- ``submissions.cleanup_lease_expires_at`` — the lease deadline pinned
  when the claim is taken (claim instant + the
  ``cleanup_claim_lease_seconds`` setting). ``cleanup_claimed_at``
  becomes the lease START (comment re-based here to say so). Semantics:

  * claim/lease live (``claimed_at`` set, ``deleted_at`` NULL, lease
    expiry in the future or NULL) — the claim is exclusive and the
    protection writers (claim -> VALIDATING / UNDER_REVIEW) still
    reject with the typed 409;
  * lease expired — the row is a crash survivor: the next scan RE-claims
    it (takeover resets both timestamps) after re-evaluating every
    §13/§27 guard under the same row locks the protection paths take
    (submission first, then the claim row — the unified serialization
    boundary that closes the P0-1 window), and the protection guard no
    longer treats it as active (protection wins over a stale lease).

  A claimed row with NULL lease expiry is treated as LIVE by every
  reader (fail-safe: an unknown lease never authorizes deletion-side
  wins); the takeover path is the only writer that clears it.

- ``upload_intents.cleanup_claimed_at`` +
  ``upload_intents.cleanup_lease_expires_at`` — the same lease primitive
  for the orphan-intent cleanup, splitting pass 4b's combined column:
  ``cleanup_deleted_at`` becomes the DONE marker only (comment re-based
  here; set after the provider delete settled, idempotently), while the
  two new columns carry the claim/lease. Intents have no protection
  transition (finalize refuses expired intents under the intent-row
  lock), so their claim stays a single conditional UPDATE — the lease
  adds only the crash takeover: an expired lease re-enters the candidate
  set and the next scan resets the timestamps and converges (§27).

Indexing: none added (the 0017 precedent). Candidates are found by the
retention/expiry scan predicates; the claim is by primary key; the
protection-side EXISTS scans one claim's few submissions through the
existing ``submissions.claim_id`` index.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The pass-4b comments of the two re-based columns (restored on
# downgrade) — frozen here so the revision stays standalone history.
_SUBMISSIONS_CLAIMED_4B_COMMENT = (
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
)
_INTENTS_DONE_4B_COMMENT = (
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
)


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "cleanup_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Cleanup deletion-claim lease deadline (hardening pass "
                "5a, P0-2): pinned to claim instant + the "
                "cleanup_claim_lease_seconds setting when the claim is "
                "taken. The claim is exclusive only while the lease lives "
                "(a claim carrying NULL expiry here is treated as LIVE — "
                "fail-safe: an unknown lease never becomes a takeover "
                "reason); once expired, the row is a crash survivor that "
                "re-enters the scan's candidate set, and the takeover "
                "re-evaluates every guard under the submission->claim row "
                "locks before resetting both timestamps."
            ),
        ),
    )
    op.add_column(
        "upload_intents",
        sa.Column(
            "cleanup_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Orphan-intent cleanup claim, lease START (hardening pass "
                "5a, splitting pass 4b's combined column): set together "
                "with cleanup_lease_expires_at by the conditional UPDATE "
                "claiming the intent (expires_at past, never finalized, "
                "not done, no live lease); cleanup_deleted_at is now the "
                "DONE marker only. Once the lease expires (a worker that "
                "died between claim and delete), the row re-enters the "
                "candidate set and the next scan takes over — resets both "
                "timestamps — and converges (§27); no protection "
                "transition exists for intents (finalize refuses expired "
                "intents under the intent-row FOR UPDATE it already takes, "
                "so a claim and a finalize serialize on the row). Cleared "
                "to release after a provider failure."
            ),
        ),
    )
    op.add_column(
        "upload_intents",
        sa.Column(
            "cleanup_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Orphan-intent cleanup claim lease deadline (hardening "
                "pass 5a): pinned to claim instant + the "
                "cleanup_claim_lease_seconds setting; the claim is "
                "exclusive only while it lives. NULL while unclaimed; a "
                "claimed row with NULL expiry is treated as LIVE "
                "(fail-safe)."
            ),
        ),
    )
    # Re-base the two pass-4b columns' comments to their pass-5a
    # semantics (lease start / done marker), keeping the model and the
    # database saying the same thing (the alembic check gate compares
    # comments).
    op.alter_column(
        "submissions",
        "cleanup_claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=_SUBMISSIONS_CLAIMED_4B_COMMENT,
        comment=(
            "Cleanup-worker deletion claim, lease START (hardening pass "
            "4b, re-based by pass 5a): set together with "
            "cleanup_lease_expires_at by the claim transaction, which "
            "locks this row and then the claim row (the SAME order the "
            "protection paths lock them) and re-evaluates every §13/§27 "
            "guard under both locks before writing — the unified "
            "serialization boundary: a protection transaction cannot "
            "commit a guarded state between this claim's guard check and "
            "its commit, and vice versa. NULL while unclaimed; cleared to "
            "release after a provider failure so the next scan retries; "
            "reset (with the lease) by the next scan's TAKEOVER once the "
            "lease expired (a crashed worker); stays set after a "
            "completed deletion (deleted_at records the completion). "
            "Protection writers (legal_hold, claim -> "
            "VALIDATING/UNDER_REVIEW) must reject with a typed 409 while "
            "this is set with deleted_at NULL AND the lease live — "
            "protection wins over a stale lease. legal_hold itself has NO "
            "service write point today (operator/DBA action) — whoever "
            "sets it retries on that 409 the same way."
        ),
    )
    op.alter_column(
        "upload_intents",
        "cleanup_deleted_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=_INTENTS_DONE_4B_COMMENT,
        comment=(
            "Orphan-intent cleanup DONE marker (hardening pass 4b, "
            "claim-half split off to cleanup_claimed_at by pass 5a): set "
            "after the object delete settled — including the "
            "FileNotFoundError idempotent-success shape (most expired "
            "intents were never uploaded) — and never re-cleared. NULL "
            "means the intent's object is still believed present or its "
            "deletion never settled. Cleanup only ever claims intents "
            "past expires_at: the presigned URL TTL is configured shorter "
            "than the intent TTL, so no legal PUT can land after "
            "expires_at."
        ),
    )


def downgrade() -> None:
    op.alter_column(
        "upload_intents",
        "cleanup_deleted_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=(
            "Orphan-intent cleanup DONE marker (hardening pass 4b, "
            "claim-half split off to cleanup_claimed_at by pass 5a): set "
            "after the object delete settled — including the "
            "FileNotFoundError idempotent-success shape (most expired "
            "intents were never uploaded) — and never re-cleared. NULL "
            "means the intent's object is still believed present or its "
            "deletion never settled. Cleanup only ever claims intents "
            "past expires_at: the presigned URL TTL is configured shorter "
            "than the intent TTL, so no legal PUT can land after "
            "expires_at."
        ),
        comment=_INTENTS_DONE_4B_COMMENT,
    )
    op.alter_column(
        "submissions",
        "cleanup_claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=(
            "Cleanup-worker deletion claim, lease START (hardening pass "
            "4b, re-based by pass 5a): set together with "
            "cleanup_lease_expires_at by the claim transaction, which "
            "locks this row and then the claim row (the SAME order the "
            "protection paths lock them) and re-evaluates every §13/§27 "
            "guard under both locks before writing — the unified "
            "serialization boundary: a protection transaction cannot "
            "commit a guarded state between this claim's guard check and "
            "its commit, and vice versa. NULL while unclaimed; cleared to "
            "release after a provider failure so the next scan retries; "
            "reset (with the lease) by the next scan's TAKEOVER once the "
            "lease expired (a crashed worker); stays set after a "
            "completed deletion (deleted_at records the completion). "
            "Protection writers (legal_hold, claim -> "
            "VALIDATING/UNDER_REVIEW) must reject with a typed 409 while "
            "this is set with deleted_at NULL AND the lease live — "
            "protection wins over a stale lease. legal_hold itself has NO "
            "service write point today (operator/DBA action) — whoever "
            "sets it retries on that 409 the same way."
        ),
        comment=_SUBMISSIONS_CLAIMED_4B_COMMENT,
    )
    op.drop_column("upload_intents", "cleanup_lease_expires_at")
    op.drop_column("upload_intents", "cleanup_claimed_at")
    op.drop_column("submissions", "cleanup_lease_expires_at")
