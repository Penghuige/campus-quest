"""Cleanup claim fencing tokens (PR #2 hardening final pass A; spec
§13/§27; the owner claim-ownership ruling — final review).

Pass 5a gave the deletion claim a lease, but the lease alone proves only
that the deadline passed — NOT that the old worker died. Two races
survived it (owner final review):

- Race A: worker A sits in a slow S3 delete; its lease expires; the
  round-5 protection rule ("protection wins over an expired lease") lets
  a protection (validation/review entry) commit; A's delete returns and
  completes — a PROTECTED file is deleted (§27 violation).
- Race B (ABA): A's lease expires; B takes over (rewriting the lease
  columns); A finally surfaces from the provider call and runs
  ``release_cleanup_claim()`` / ``mark_deleted()`` whose WHERE clauses
  matched on ``claimed_at IS NOT NULL`` alone — clearing or completing
  B's LIVE claim.

This revision closes both with an ownership token and a semantic ruling:

- ``submissions.cleanup_claim_token`` / ``upload_intents.cleanup_claim_token``
  — a fresh uuid4 written by EVERY claim and EVERY lease takeover (NULL
  while unclaimed, cleared only by the owning worker's release).
  ``release_cleanup_claim``, ``mark_deleted``, ``release_intent`` and
  ``mark_intent_deleted`` now compare-and-set on the token
  (``WHERE ... AND cleanup_claim_token = :token``): a stale worker's
  late release/mark matches ZERO rows, logs, and gives up — it can never
  clear or complete a takeover's claim (ABA closure).
- Claim-ownership semantics (the owner ruling, superseding round-5's
  "protection wins over an expired lease"): an UNFINISHED deletion claim
  blocks protection transitions even after its lease expires. Lease
  expiry authorizes cleanup TAKEOVER only — the takeover rewrites the
  token and resolves the deletion; protection retries after the
  recovery finishes. A slow-but-alive old worker therefore can never
  delete an object after a protection committed. The re-based comments
  on ``submissions.cleanup_claimed_at`` / ``cleanup_lease_expires_at``
  and ``upload_intents.cleanup_claimed_at`` record the same ruling (the
  alembic check gate compares comments).

The second line behind the fencing: the S3 adapter's explicit
connect/read timeouts (this pass) bound how long a claim can sit
live-but-slow in the provider call.

Indexing: none added (the 0017/0018 precedent). Release/mark CAS by
primary key + token equality; the protection guard scans one claim's few
submissions through the existing ``submissions.claim_id`` index.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The pass-5a comments this revision re-bases (restored on downgrade) —
# frozen here so the revision stays standalone history.
_SUBMISSIONS_CLAIMED_5A_COMMENT = (
    "Cleanup-worker deletion claim, lease START (hardening pass 4b, "
    "re-based by pass 5a): set together with cleanup_lease_expires_at "
    "by the claim transaction, which locks this row and then the claim "
    "row (the SAME order the protection paths lock them) and "
    "re-evaluates every §13/§27 guard under both locks before "
    "writing — the unified serialization boundary: a protection "
    "transaction cannot commit a guarded state between this claim's "
    "guard check and its commit, and vice versa. NULL while "
    "unclaimed; cleared to release after a provider failure so the "
    "next scan retries; reset (with the lease) by the next scan's "
    "TAKEOVER once the lease expired (a crashed worker); stays set "
    "after a completed deletion (deleted_at records the completion). "
    "Protection writers (legal_hold, claim -> VALIDATING/UNDER_REVIEW) "
    "must reject with a typed 409 while this is set with deleted_at "
    "NULL AND the lease live — protection wins over a stale lease. "
    "legal_hold itself has NO service write point today "
    "(operator/DBA action) — whoever sets it retries on that 409 the "
    "same way."
)
_SUBMISSIONS_LEASE_5A_COMMENT = (
    "Cleanup deletion-claim lease deadline (hardening pass 5a, "
    "P0-2): pinned to claim instant + the "
    "cleanup_claim_lease_seconds setting when the claim is taken. "
    "The claim is exclusive only while the lease lives (a claim "
    "carrying NULL expiry here is treated as LIVE — fail-safe: an "
    "unknown lease never becomes a takeover reason); once expired, "
    "the row is a crash survivor that re-enters the scan's "
    "candidate set, and the takeover re-evaluates every guard "
    "under the submission->claim row locks before resetting both "
    "timestamps."
)
_INTENTS_CLAIMED_5A_COMMENT = (
    "Orphan-intent cleanup claim, lease START (hardening pass "
    "5a, splitting pass 4b's combined column): set together with "
    "cleanup_lease_expires_at by the conditional UPDATE claiming "
    "the intent (expires_at past, never finalized, not done, no "
    "live lease); cleanup_deleted_at is now the DONE marker only. "
    "Once the lease expires (a worker that died between claim and "
    "delete), the row re-enters the candidate set and the next "
    "scan takes over — resets both timestamps — and converges "
    "(§27); no protection transition exists for intents (finalize "
    "refuses expired intents under the intent-row FOR UPDATE it "
    "already takes, so a claim and a finalize serialize on the "
    "row). Cleared to release after a provider failure."
)


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column(
            "cleanup_claim_token",
            sa.Uuid(),
            nullable=True,
            comment=(
                "Cleanup deletion-claim ownership token (hardening final "
                "pass A, fencing): a fresh uuid4 written by EVERY claim "
                "and EVERY lease takeover, cleared only by the owning "
                "worker's release. release_cleanup_claim and mark_deleted "
                "compare-and-set on it (WHERE cleanup_claim_token = "
                ":token), so a slow-but-alive worker resuming after its "
                "lease expired and a takeover happened matches zero rows "
                "and can never clear or complete another worker's claim "
                "(ABA closure; the second line behind it is the S3 "
                "adapter's bounded connect/read timeouts). NULL while "
                "unclaimed."
            ),
        ),
    )
    op.add_column(
        "upload_intents",
        sa.Column(
            "cleanup_claim_token",
            sa.Uuid(),
            nullable=True,
            comment=(
                "Orphan-intent cleanup ownership token (hardening final "
                "pass A, fencing): a fresh uuid4 written by every claim "
                "and every lease takeover of the intent; release_intent "
                "and mark_intent_deleted compare-and-set on it, so a "
                "stale worker's late release or done-mark cannot touch a "
                "takeover's claim (the same ABA closure as the "
                "submissions side). NULL while unclaimed; cleared on "
                "release."
            ),
        ),
    )
    # Re-base the pass-5a comments to the claim-ownership ruling (the
    # alembic check gate compares comments, so model and database must
    # keep saying the same thing).
    op.alter_column(
        "submissions",
        "cleanup_claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=_SUBMISSIONS_CLAIMED_5A_COMMENT,
        comment=(
            "Cleanup-worker deletion claim, lease START (hardening pass "
            "4b, re-based by pass 5a and the final-pass claim-ownership "
            "ruling): set together with cleanup_lease_expires_at and a "
            "fresh cleanup_claim_token by the claim transaction, which "
            "locks this row and then the claim row (the SAME order the "
            "protection paths lock them) and re-evaluates every §13/§27 "
            "guard under both locks before writing — the unified "
            "serialization boundary: a protection transaction cannot "
            "commit a guarded state between this claim's guard check and "
            "its commit, and vice versa. NULL while unclaimed; cleared "
            "to release after a provider failure so the next scan "
            "retries; reset (with the lease and a fresh token) by the "
            "next scan's TAKEOVER once the lease expired (a crashed "
            "worker); stays set after a completed deletion (deleted_at "
            "records the completion). Protection writers (legal_hold, "
            "claim -> VALIDATING/UNDER_REVIEW) must reject with a typed "
            "409 while this is set with deleted_at NULL, EVEN AFTER the "
            "lease has expired — an UNFINISHED deletion claim blocks "
            "protection until the deletion settles (completion, release, "
            "or a takeover that resolves it); lease expiry authorizes "
            "cleanup takeover only (the owner ruling superseding "
            "round-5's protection-wins-over-stale-lease rule). legal_hold "
            "itself has NO service write point today (operator/DBA "
            "action) — whoever sets it retries on that 409 the same way."
        ),
    )
    op.alter_column(
        "submissions",
        "cleanup_lease_expires_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=_SUBMISSIONS_LEASE_5A_COMMENT,
        comment=(
            "Cleanup deletion-claim lease deadline (hardening pass 5a, "
            "P0-2): pinned to claim instant + the "
            "cleanup_claim_lease_seconds setting when the claim is "
            "taken. The claim is exclusive only while the lease lives "
            "(a claim carrying NULL expiry here is treated as LIVE — "
            "fail-safe: an unknown lease never becomes a takeover "
            "reason); once expired, the row is a crash survivor that "
            "re-enters the scan's candidate set, and the takeover "
            "re-evaluates every guard under the submission->claim row "
            "locks before resetting both timestamps and rewriting the "
            "claim token. Expiry changes NOTHING for the protection "
            "writers (the claim-ownership ruling): an expired lease "
            "authorizes the cleanup-side takeover only, never a "
            "protection transition past an unfinished claim."
        ),
    )
    op.alter_column(
        "upload_intents",
        "cleanup_claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=_INTENTS_CLAIMED_5A_COMMENT,
        comment=(
            "Orphan-intent cleanup claim, lease START (hardening pass "
            "5a, splitting pass 4b's combined column): set together "
            "with cleanup_lease_expires_at and a fresh "
            "cleanup_claim_token by the conditional UPDATE claiming the "
            "intent (expires_at past, never finalized, not done, no "
            "live lease); cleanup_deleted_at is now the DONE marker "
            "only. Once the lease expires (a worker that died between "
            "claim and delete), the row re-enters the candidate set and "
            "the next scan takes over — resets both timestamps and "
            "rewrites the claim token — and converges (§27); no "
            "protection transition exists for intents (finalize refuses "
            "expired intents under the intent-row FOR UPDATE it already "
            "takes, so a claim and a finalize serialize on the row). "
            "Cleared (with the token) to release after a provider "
            "failure."
        ),
    )


def downgrade() -> None:
    op.alter_column(
        "upload_intents",
        "cleanup_claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=(
            "Orphan-intent cleanup claim, lease START (hardening pass "
            "5a, splitting pass 4b's combined column): set together "
            "with cleanup_lease_expires_at and a fresh "
            "cleanup_claim_token by the conditional UPDATE claiming the "
            "intent (expires_at past, never finalized, not done, no "
            "live lease); cleanup_deleted_at is now the DONE marker "
            "only. Once the lease expires (a worker that died between "
            "claim and delete), the row re-enters the candidate set and "
            "the next scan takes over — resets both timestamps and "
            "rewrites the claim token — and converges (§27); no "
            "protection transition exists for intents (finalize refuses "
            "expired intents under the intent-row FOR UPDATE it already "
            "takes, so a claim and a finalize serialize on the row). "
            "Cleared (with the token) to release after a provider "
            "failure."
        ),
        comment=_INTENTS_CLAIMED_5A_COMMENT,
    )
    op.alter_column(
        "submissions",
        "cleanup_lease_expires_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=(
            "Cleanup deletion-claim lease deadline (hardening pass 5a, "
            "P0-2): pinned to claim instant + the "
            "cleanup_claim_lease_seconds setting when the claim is "
            "taken. The claim is exclusive only while the lease lives "
            "(a claim carrying NULL expiry here is treated as LIVE — "
            "fail-safe: an unknown lease never becomes a takeover "
            "reason); once expired, the row is a crash survivor that "
            "re-enters the scan's candidate set, and the takeover "
            "re-evaluates every guard under the submission->claim row "
            "locks before resetting both timestamps and rewriting the "
            "claim token. Expiry changes NOTHING for the protection "
            "writers (the claim-ownership ruling): an expired lease "
            "authorizes the cleanup-side takeover only, never a "
            "protection transition past an unfinished claim."
        ),
        comment=_SUBMISSIONS_LEASE_5A_COMMENT,
    )
    op.alter_column(
        "submissions",
        "cleanup_claimed_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        previous_comment=(
            "Cleanup-worker deletion claim, lease START (hardening pass "
            "4b, re-based by pass 5a and the final-pass claim-ownership "
            "ruling): set together with cleanup_lease_expires_at and a "
            "fresh cleanup_claim_token by the claim transaction, which "
            "locks this row and then the claim row (the SAME order the "
            "protection paths lock them) and re-evaluates every §13/§27 "
            "guard under both locks before writing — the unified "
            "serialization boundary: a protection transaction cannot "
            "commit a guarded state between this claim's guard check and "
            "its commit, and vice versa. NULL while unclaimed; cleared "
            "to release after a provider failure so the next scan "
            "retries; reset (with the lease and a fresh token) by the "
            "next scan's TAKEOVER once the lease expired (a crashed "
            "worker); stays set after a completed deletion (deleted_at "
            "records the completion). Protection writers (legal_hold, "
            "claim -> VALIDATING/UNDER_REVIEW) must reject with a typed "
            "409 while this is set with deleted_at NULL, EVEN AFTER the "
            "lease has expired — an UNFINISHED deletion claim blocks "
            "protection until the deletion settles (completion, release, "
            "or a takeover that resolves it); lease expiry authorizes "
            "cleanup takeover only (the owner ruling superseding "
            "round-5's protection-wins-over-stale-lease rule). legal_hold "
            "itself has NO service write point today (operator/DBA "
            "action) — whoever sets it retries on that 409 the same way."
        ),
        comment=_SUBMISSIONS_CLAIMED_5A_COMMENT,
    )
    op.drop_column("upload_intents", "cleanup_claim_token")
    op.drop_column("submissions", "cleanup_claim_token")
