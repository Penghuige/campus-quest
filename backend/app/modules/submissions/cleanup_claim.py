# backend/app/modules/submissions/cleanup_claim.py
"""Protection-side guard for the cleanup deletion claim (hardening pass
4b, closed by pass 5a; spec §13/§27; the unified serialization boundary
ruling).

The file-retention cleanup deletes only under a claimed deletion right:
``submissions.cleanup_claimed_at`` + ``cleanup_lease_expires_at`` (a
LEASE, pass 5a). The claim transaction locks the submission row and
then the claim row — the SAME order the protection paths lock them —
re-evaluates every retain guard under both locks, writes the lease, and
commits; the provider delete runs outside the transaction. Because both
sides now serialize on the same row locks, a claim can no longer commit
between a protection transaction's guard check (below) and its own
status commit: whichever side takes the locks first wins, the other
side's guard sees the committed outcome. That closes the pass-4b
residual both-in-flight window (the sub-millisecond check-to-commit gap
owner ruled a §27 correctness violation, not an inherent boundary).

This module is the protection half of the ruling: while a claim is in
flight (claimed, not completed, lease LIVE), the writers that move a
submission's file INTO the protected set must refuse with a typed
conflict instead of silently landing on an object that is being deleted:

- claim -> VALIDATING (the validation service's tx1),
- claim -> UNDER_REVIEW (the reward-lock service's review entry).

Protection must win; deletion is the retryable side. The claim window is
seconds (claim -> S3 delete -> completion mark, no row lock held across
the provider call), so the conflict is a RETRY-LATER answer: the
validation job carries this error in its bounded autoretry, and an
operator setting ``legal_hold`` retries the same way.

Lease semantics (pass 5a, P0-2): an EXPIRED lease is a crashed worker,
not an active deletion. The guard below does not treat it as a conflict
— protection proceeds, and the next cleanup scan's takeover re-evaluates
every guard under the row locks, loses to the committed protection, and
never deletes. A claim whose lease column is NULL is treated as LIVE
(fail-safe: an unknown lease never authorizes the deletion side of any
race).

``legal_hold`` has NO service write point today (operator/DBA actions
only — recorded in the 0017 migration and on the model column): whoever
sets it must apply the same rule, re-checking for a live claim (or
retrying on the 409) before committing the hold.

Import discipline: importing this module must stay sqlalchemy-free at
MODULE level (``app.workers.jobs.validate_submission`` pins that its
import loads neither sqlalchemy nor app.db; the DB imports live inside
the guard function, the cleanup-repo precedent).
"""

from __future__ import annotations

from datetime import datetime
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
    """A submission under this claim holds an unfinished cleanup claim
    with a LIVE lease: its object is being deleted right now. Typed 409
    — the caller retries after the seconds-level claim window closes
    (or the lease expires and a crashed worker's claim goes stale).

    ``code`` reuses ``VALIDATION_ERROR`` (the typed-409 precedent the
    same module family already ships, e.g. SubmissionNotValidatedError):
    the error-registry contract requires every code to exist in
    docs/interfaces.md first, and this hardening pass does not touch
    that frozen registry.
    """

    def __init__(self, claim_id: UUID, submission_ids: list[UUID]) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            _CONFLICT_MESSAGE,
            status_code=409,
            details={
                "claim_id": str(claim_id),
                "submission_ids": [str(sid) for sid in submission_ids],
            },
        )


async def ensure_no_active_cleanup_claim(
    db: AsyncSession, claim_id: UUID, *, now: datetime
) -> None:
    """Raise ``CleanupClaimConflictError`` when ANY submission under the
    claim holds an unfinished cleanup claim with a LIVE lease at ``now``.

    Scope is the WHOLE claim, matching the cleanup predicate's scope:
    the candidate filter excludes submissions whose claim is inside the
    review pipeline, so entering those states must protect every version
    under the claim, not just the submission being processed. Not a
    conflict: a finished deletion (``deleted_at`` set — the object is
    gone, protection is moot), a released claim (both claim columns
    NULL), and a claim whose lease has EXPIRED (a crashed worker;
    protection wins over a stale lease — the takeover will lose to the
    protection this call is about to commit). A claimed row with a NULL
    lease expiry IS a conflict (fail-safe: an unknown lease is live).

    Call this INSIDE the caller's transaction while holding the
    assignment-claim row lock (both existing write points do), before
    writing the protected status; raising rolls the transition back.
    ``now`` should be the caller's already-sampled transaction instant —
    sampled no LATER than this check: judging a lease against an
    earlier instant can only see it as live, which errs toward the
    retryable 409, never toward landing on a being-deleted object.
    """
    from sqlalchemy import or_, select

    from app.modules.submissions.models import Submission

    claimed = (
        (
            await db.execute(
                select(Submission.id).where(
                    Submission.claim_id == claim_id,
                    Submission.cleanup_claimed_at.is_not(None),
                    Submission.deleted_at.is_(None),
                    # Live lease: expiry in the future, or unknown (NULL)
                    # — the fail-safe reading that keeps the deletion
                    # side from ever winning an ambiguous state.
                    or_(
                        Submission.cleanup_lease_expires_at.is_(None),
                        Submission.cleanup_lease_expires_at > now,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if claimed:
        raise CleanupClaimConflictError(claim_id, list(claimed))
