# backend/app/workers/jobs/requeue_stale_validating.py
"""Stale-VALIDATING and stale-UPLOADED recovery scan (PR #2 hardening,
final-review sub-F2 + final pass B P1; spec §10 step 8, §32 idempotency;
plan-04 task 7's retry-exhaustion gap).

Two wedged families, one discovery job (G8/G9 durable handoff: a
finalized row must always have a scheduled path back into the pipeline
— row existence implies recovery):

1. **stale VALIDATING**: ``workers.validate_submission`` retries only
   transient storage failures, and after ``max_retries`` (or an
   instance lost to worker death under ``task_acks_late`` — redelivery
   covers process loss, not exhausted retries) the job fails with the
   submission parked in VALIDATING — the validation service's tx1
   committed (status flip + run row) but tx2 never landed. The claim
   rides along in VALIDATING, which the expiry ladder answers
   PROTECTED: nothing expires the pair and nothing finishes it, so
   without this scan both rows are wedged forever (the final-review
   finding: "校验 job 重试耗尽后 submission/claim 永久卡 VALIDATING").
   Discovery signal: submissions still VALIDATING whose NEWEST
   ``submission_validations`` run row started longer ago than
   ``stale_validating_requeue_seconds`` (default 1800s, sized above the
   job's worst legitimate window: 6 attempts x download + sandbox
   wall-time + backoff). A re-dispatched run writes a fresh run row in
   tx1, which re-arms the clock — repeated scans cannot hot-loop a
   broken row.

2. **stale UPLOADED** (final pass B P1): two paths leave a finalized
   row UPLOADED with NO run row at all, invisible to family 1's
   newest-run signal — (a) the finalize commit succeeded but the
   dispatch after it failed (a broker outage at the enqueue; a client
   that never retries upload-complete leaves it parked forever), and
   (b) every validation attempt died BEFORE tx1 committed:
   ``CleanupClaimConflictError`` (a live cleanup deletion lease
   refusing the claim's VALIDATING entry) is autoretried at most 5
   times with ~1s backoff ≈ 31s — far inside the 300s lease — so the
   ladder exhausts, the job fails loudly, and the row stays UPLOADED
   with the whole tx1 rolled back. Discovery signal: submissions still
   UPLOADED whose ``submitted_at`` is older than
   ``uploaded_dispatch_grace_seconds`` (default 120s, above the
   healthy finalize-commit -> enqueue round trip, so a row whose
   dispatch simply has not landed yet is never scooped up — no hot
   loop over fresh finalizes). The conflict in (b) is inherently
   time-bounded (the lease expires or the deletion completes), so the
   next scan after it lifts heals the row; until then each beat
   re-dispatches a bounded retry ladder and the job fails loudly again
   — churn, never a wedge.

The scan is the minimal closed loop for both families: DISCOVERY +
re-dispatch only.

Repeat-enqueue idempotency needs no extra gate — the SERVICE's tx1 is
the gate. ``ValidationService.validate_submission`` takes the
submission row FOR UPDATE and judges status under the lock:

- TERMINAL (VALIDATED / VALIDATION_FAILED) -> the persisted report is
  replayed, nothing is written;
- UPLOADED -> the one-way UPLOADED -> VALIDATING flip, the claim's
  CLAIMED/REVISION_REQUIRED -> VALIDATING move, and the run-row INSERT
  commit as ONE transaction — whichever dispatched job takes the lock
  first performs the flip; every duplicate arriving after it sees the
  flipped state, so no amount of duplicate enqueue can double-flip,
  resurrect, or create a second submission;
- VALIDATING (a duplicate racing the flip, or the stale shape) -> the
  stale-rerun gate: a fresh run row while the interrupted row stays as
  honest history, and tx2's ``populate_existing`` last-writer-wins
  arbitration collapses the twin-run shape — both reports describe the
  same write-once frozen object, never two verdicts.

So the scan never mutates submission or claim state itself, and §26
strict reading and reward semantics are untouched (G13/G14); it changes
only WHEN the existing pipeline gets another attempt. At-least-once
redelivery of THIS task is equally safe: discovery is a pure read over
committed state.

Retry policy: transient DB failures retry with bounded backoff (the
scan only reads and enqueues); the re-dispatched validation jobs carry
their own policy. A dispatch-time broker failure surfaces as this
task's failure — the next beat re-runs the whole scan, which is the
at-least-once envelope the idempotent service gates absorb.

Importing this module stays environment-free (the celery_app
lazy-construction contract); see the module docstring in
expire_claims.py for the ``sqlalchemy.exc`` exception-import precedent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy.exc import (  # env-free: see module docstring
    DBAPIError,
    OperationalError,
)

# The per-id unit of work this scan feeds. Imported at module scope
# (the dispatch_due_notifications -> send_notification precedent):
# importing it stays environment-free, and naming the producer ->
# consumer edge here keeps the recovery flow readable in one place.
from app.workers.jobs.validate_submission import validate_submission_job
from app.workers.session_source import run_with_session

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# One scan batch's ceiling: bounds the per-scan re-dispatch burst; the
# next beat scan picks up whatever a huge backlog leaves behind.
SCAN_BATCH_LIMIT = 500

#: Transient database failures the scan's bounded autoretry covers.
_RETRYABLE_DB_TRANSIENTS = (OperationalError, DBAPIError)

_MAX_RETRIES = 5


async def collect_stale_validating_ids(
    session: AsyncSession, now: datetime, *, stale_after: timedelta, limit: int
) -> list[UUID]:
    """Submissions stuck in the UPLOADED -> VALIDATING transition — the
    scan's whole read for family 1 (a tx1 committed, a tx2 that never
    landed).

    A row qualifies when its projection is still VALIDATING and its
    NEWEST run row started before ``now - stale_after``: tx1 of that run
    committed (the flip and the run row are one transaction) and tx2
    never landed, so the newest run's age is exactly how long the row
    has been wedged. Ordering by that age drains the most-stuck rows
    first. Discovery only — the re-dispatched job re-judges everything
    through the service's own gates.
    """
    from sqlalchemy import func, select

    from app.modules.submissions.enums import ValidationStatus
    from app.modules.submissions.models import Submission, SubmissionValidation

    newest_run = (
        select(
            SubmissionValidation.submission_id.label("submission_id"),
            func.max(SubmissionValidation.started_at).label("newest_started_at"),
        )
        .group_by(SubmissionValidation.submission_id)
        .subquery()
    )
    rows = (
        await session.scalars(
            select(Submission.id)
            .join(newest_run, newest_run.c.submission_id == Submission.id)
            .where(
                Submission.validation_status == ValidationStatus.VALIDATING.value,
                newest_run.c.newest_started_at <= now - stale_after,
            )
            .order_by(newest_run.c.newest_started_at, Submission.id)
            .limit(limit)
        )
    ).all()
    return list(rows)


async def collect_stale_uploaded_ids(
    session: AsyncSession, now: datetime, *, grace: timedelta, limit: int
) -> list[UUID]:
    """Finalized submissions still UPLOADED past the dispatch grace —
    family 2 (a dispatch that never landed, or a validation job whose
    every attempt died before tx1 committed).

    A row qualifies when its projection is still UPLOADED and its
    ``submitted_at`` (the finalize-time clock sample — the row was born
    verified, the object was there) predates ``now - grace``. UPLOADED
    rows carry NO run rows yet (tx1 writes the first one), so
    ``submitted_at`` is the only age signal this family has; the grace
    keeps freshly finalized rows — whose enqueue is merely in flight —
    out of the scan. Disjoint from family 1 by the status predicate, so
    the two id lists never overlap. Discovery only.
    """
    from sqlalchemy import select

    from app.modules.submissions.enums import ValidationStatus
    from app.modules.submissions.models import Submission

    rows = (
        await session.scalars(
            select(Submission.id)
            .where(
                Submission.validation_status == ValidationStatus.UPLOADED.value,
                Submission.submitted_at <= now - grace,
            )
            .order_by(Submission.submitted_at, Submission.id)
            .limit(limit)
        )
    ).all()
    return list(rows)


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.requeue_stale_validating",
    autoretry_for=_RETRYABLE_DB_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def requeue_stale_validating(self: Any, request_id: str) -> dict[str, Any]:
    """Discovery only: re-enqueue ``workers.validate_submission`` for
    every stale-VALIDATING and stale-UPLOADED submission id (the two
    families share one beat entry — both are the same remedy, another
    attempt at a pipeline the row already belongs to).

    The scan writes nothing; the re-dispatched jobs are absorbed
    idempotently by the validation service's terminal / stale-rerun /
    tx1-serialization gates (see the module docstring), so at-least-once
    redelivery of THIS task is equally safe.
    """
    from app.core.clock import SystemClock
    from app.core.config import get_settings

    job_id = self.request.id
    settings = get_settings()
    stale_after = timedelta(seconds=settings.stale_validating_requeue_seconds)
    uploaded_grace = timedelta(seconds=settings.uploaded_dispatch_grace_seconds)
    logger.info(
        "requeue_stale_validating.start",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "stale_after_seconds": settings.stale_validating_requeue_seconds,
            "uploaded_grace_seconds": settings.uploaded_dispatch_grace_seconds,
        },
    )

    async def _discover(
        session: AsyncSession,
    ) -> tuple[list[UUID], list[UUID]]:
        now = SystemClock().now()
        validating = await collect_stale_validating_ids(
            session, now, stale_after=stale_after, limit=SCAN_BATCH_LIMIT
        )
        uploaded = await collect_stale_uploaded_ids(
            session, now, grace=uploaded_grace, limit=SCAN_BATCH_LIMIT
        )
        return validating, uploaded

    stale_validating_ids, stale_uploaded_ids = asyncio.run(run_with_session(_discover))
    submission_ids = [*stale_validating_ids, *stale_uploaded_ids]
    for submission_id in submission_ids:
        validate_submission_job.delay(str(submission_id), request_id)
    payload: dict[str, Any] = {
        "request_id": request_id,
        "requeued": len(submission_ids),
        "stale_validating": len(stale_validating_ids),
        "stale_uploaded": len(stale_uploaded_ids),
        "submission_ids": [str(submission_id) for submission_id in submission_ids],
    }
    logger.info(
        "requeue_stale_validating.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
