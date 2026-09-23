# backend/app/modules/submissions/cleanup_claim.py
"""Protection-side guard for the cleanup deletion claim (hardening pass
4b, closed by pass 5a; spec §13/§27; the unified serialization boundary
ruling; the final-pass claim-ownership ruling).

The file-retention cleanup deletes only under a claimed deletion right:
``submissions.cleanup_claimed_at`` + ``cleanup_lease_expires_at`` + the
``cleanup_claim_token`` ownership column (a LEASE with a fencing token).
The claim transaction locks the submission row and then the claim row —
the SAME order the protection paths lock them — re-evaluates every
retain guard under both locks, writes the lease and a fresh token, and
commits; the provider delete runs outside the transaction. Because both
sides serialize on the same row locks, a claim can no longer commit
between a protection transaction's guard check (below) and its own
status commit: whichever side takes the locks first wins, the other
side's guard sees the committed outcome.

This module is the protection half: while a claim is UNFINISHED
(claimed, not completed), the writers that move a submission's file
INTO the protected set must refuse with a typed conflict instead of
silently landing on an object that is being deleted:

- claim -> VALIDATING (the validation service's tx1),
- claim -> UNDER_REVIEW (the reward-lock service's review entry).

CLAIM-OWNERSHIP SEMANTICS (owner ruling, final review — it SUPERSEDES
round-5's "protection wins over an expired lease" rule): an unfinished
deletion claim blocks protection transitions EVEN AFTER its lease
expires. A lease expiring proves only that the deadline passed, never
that the old worker died — a worker sitting in a slow S3 delete could
still complete it. Lease expiry therefore authorizes exactly one thing:
the CLEANUP-SIDE takeover, which re-claims under the same serialization
boundary, REWRITES the ownership token, and resolves the deletion (the
old worker's late release/mark then loses the token CAS and is
abandoned). Protection retries after that recovery finishes — the
conflict stays a RETRY-LATER answer: the validation job carries this
error in its bounded autoretry, and an operator setting ``legal_hold``
retries the same way. The claim window is seconds to one lease length
(plus the provider-call bound the S3 adapter's explicit connect/read
timeouts enforce), so the retry converges.

``legal_hold`` has NO service write point today (operator/DBA actions
only — recorded in the 0017 migration and on the model column): whoever
sets it must apply the same rule, re-checking for an unfinished claim
(or retrying on the 409) before committing the hold.

Import discipline: importing this module must stay sqlalchemy-free at
MODULE level (``app.workers.jobs.validate_submission`` pins that its
import loads neither sqlalchemy nor app.db; the DB imports live inside
the guard function, the cleanup-repo precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CleanupClaimConflictError",
    "ensure_no_active_cleanup_claim",
]

_CONFLICT_MESSAGE = "文件清理正在进行，请稍后重试"


class CleanupClaimConflictError(BusinessError):
    """A submission under this claim holds an UNFINISHED cleanup claim:
    its object is being deleted right now, or was when a worker's lease
    expired without the deletion settling — the claim-ownership ruling
    treats both as in-flight. Typed 409 — the caller retries after the
    claim settles (completion, release, or the takeover's recovery; the
    seconds-to-lease-length window closes on its own).

    ``code`` is ``CONFLICT`` (registered Plan 08 T9): a concurrent
    ownership refusal at 409, no longer riding ``VALIDATION_ERROR`` —
    the reuse gap this class's docstring used to document.
    """

    def __init__(self, claim_id: UUID, submission_ids: list[UUID]) -> None:
        super().__init__(
            ErrorCode.CONFLICT,
            _CONFLICT_MESSAGE,
            status_code=409,
            details={
                "claim_id": str(claim_id),
                "submission_ids": [str(sid) for sid in submission_ids],
            },
        )


async def ensure_no_active_cleanup_claim(db: AsyncSession, claim_id: UUID) -> None:
    """Raise ``CleanupClaimConflictError`` when ANY submission under the
    claim holds an UNFINISHED cleanup claim — SAFETY-FIRST (the
    claim-ownership ruling): the lease clock is NOT consulted, because
    an expired lease does not prove the old worker died. Any of
    ``cleanup_claimed_at`` / ``cleanup_claim_token`` set with
    ``deleted_at`` NULL is a conflict, however stale the lease reads.

    Scope is the WHOLE claim, matching the cleanup predicate's scope:
    the candidate filter excludes submissions whose claim is inside the
    review pipeline, so entering those states must protect every version
    under the claim, not just the submission being processed. Not a
    conflict: a finished deletion (``deleted_at`` set — the object is
    gone, protection is moot) and a released claim (all claim columns
    NULL). Lease expiry changes nothing here — it authorizes the
    cleanup-side takeover only (which rewrites the token and resolves
    the deletion); this guard passes again once the takeover completes,
    releases, or the original worker does.

    Call this INSIDE the caller's transaction while holding the
    assignment-claim row lock (both existing write points do), before
    writing the protected status; raising rolls the transition back.
    """

    from sqlalchemy import or_, select

    from app.modules.submissions.models import Submission

    claimed = (
        (
            await db.execute(
                select(Submission.id).where(
                    Submission.claim_id == claim_id,
                    Submission.deleted_at.is_(None),
                    # Unfinished claim, fail-safe on EITHER claim marker:
                    # a row with a token but no timestamp (or the reverse)
                    # is still a claim nobody proved finished — it blocks.
                    or_(
                        Submission.cleanup_claimed_at.is_not(None),
                        Submission.cleanup_claim_token.is_not(None),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if claimed:
        raise CleanupClaimConflictError(claim_id, list(claimed))
