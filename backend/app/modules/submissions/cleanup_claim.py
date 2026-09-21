# backend/app/modules/submissions/cleanup_claim.py
"""Protection-side guard for the cleanup deletion claim (hardening pass
4b; spec §13/§27; the cleanup TOCTOU closure ruling).

The file-retention cleanup no longer deletes on a scanned boolean
snapshot: a single conditional UPDATE re-evaluates every retain guard
against CURRENT committed state and claims the deletion right by setting
``submissions.cleanup_claimed_at`` (guards and claim are one atomic
statement). A protection that is already committed when the claim runs
therefore WINS — the claim's WHERE fails and the object survives.

This module is the other half of the ruling, for the opposite order:
while a claim is in flight (claimed but not yet completed —
``cleanup_claimed_at IS NOT NULL AND deleted_at IS NULL``), the writers
that move a submission's file INTO the protected set must refuse with a
typed conflict instead of silently landing on an object that is being
deleted:

- claim -> VALIDATING (the validation service's tx1),
- claim -> UNDER_REVIEW (the reward-lock service's review entry).

Protection must win; deletion is the retryable side. The claim window is
seconds (claim -> S3 delete -> completion mark, no row lock held across
the provider call), so the conflict is a RETRY-LATER answer: the
validation job carries this error in its bounded autoretry, and an
operator setting ``legal_hold`` retries the same way.

``legal_hold`` has NO service write point today (operator/DBA actions
only — recorded in the 0017 migration and on the model column): whoever
sets it must apply the same rule, re-checking for a live claim (or
retrying on the 409) before committing the hold.

Isolation note (READ COMMITTED): the guard runs UNDER the claim-row FOR
UPDATE lock both transitions already take, and every claim-committed
before the guard statement is visible to its fresh statement snapshot.
The one residual interleaving is the both-in-flight window: a claim
committing BETWEEN this guard's check and the transition's own commit
slips past both sides (the claim's WHERE read the claim status from its
own statement snapshot, taken before the protection committed). The
window is the sub-millisecond check-to-commit gap — versus the
scan-to-delete gap of seconds the pass-4b claim closed — and its harm
is bounded to an overdue OLD version's object; a takeover-based fix
(re-reading status under the claim-row lock) would abandon the ruling's
single-statement claim. Stranded-claim crashes aside, no larger window
exists.

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
    """A submission under this claim holds an unfinished cleanup claim:
    its object is being deleted right now. Typed 409 — the caller
    retries after the seconds-level claim window closes.

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


async def ensure_no_active_cleanup_claim(db: AsyncSession, claim_id: UUID) -> None:
    """Raise ``CleanupClaimConflictError`` when ANY submission under the
    claim holds an unfinished cleanup claim.

    Scope is the WHOLE claim, matching the cleanup predicate's scope:
    the candidate filter excludes submissions whose claim is inside the
    review pipeline, so entering those states must protect every version
    under the claim, not just the submission being processed. A finished
    deletion (``deleted_at`` set) is not a conflict — the object is
    already gone and protection is moot; a released claim (column NULL)
    is not either.

    Call this INSIDE the caller's transaction while holding the
    assignment-claim row lock (both existing write points do), before
    writing the protected status; raising rolls the transition back.
    """
    from sqlalchemy import select

    from app.modules.submissions.models import Submission

    claimed = (
        (
            await db.execute(
                select(Submission.id).where(
                    Submission.claim_id == claim_id,
                    Submission.cleanup_claimed_at.is_not(None),
                    Submission.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if claimed:
        raise CleanupClaimConflictError(claim_id, list(claimed))
