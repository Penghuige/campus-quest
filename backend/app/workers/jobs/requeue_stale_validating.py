# backend/app/workers/jobs/requeue_stale_validating.py
"""Stale-VALIDATING recovery scan (PR #2 hardening, final-review sub-F2;
spec §10 step 8, §32 idempotency; plan-04 task 7's retry-exhaustion gap).

The gap: ``workers.validate_submission`` retries only transient storage
failures, and after ``max_retries`` (or an instance lost to worker death
under ``task_acks_late`` — redelivery covers process loss, not exhausted
retries) the job fails with the submission parked in VALIDATING — the
validation service's tx1 committed (status flip + run row) but tx2 never
landed. The claim rides along in VALIDATING, which the expiry ladder
answers PROTECTED: nothing expires the pair and nothing finishes it, so
without this scan both rows are wedged forever (the final-review
finding: "校验 job 重试耗尽后 submission/claim 永久卡 VALIDATING").

This scan is the minimal closed loop: DISCOVERY + re-dispatch only —
submissions still VALIDATING whose NEWEST run row started longer ago
than ``stale_validating_requeue_seconds`` (default 1800s, sized above
the job's worst legitimate window: 6 attempts x download + sandbox
wall-time + backoff) are re-enqueued to ``workers.validate_submission``.

Idempotency needs no extra gate — the SERVICE is the gate:

- a submission that reached a terminal state replays the persisted
  report (``already_terminal``) and, on the VALIDATED replay, re-runs
  the chained ``on_validation_passed`` — which also heals the sibling
  stuck shape (VALIDATED submission + claim still VALIDATING from a
  lost chain commit);
- a genuinely stale VALIDATING run re-enters through the service's
  stale-rerun gate (fresh run row, the interrupted row stays as honest
  history);
- a re-dispatch racing a live retry simply produces the twin-run shape
  the service already arbitrates (last-writer-wins, both reports
  describe the same frozen object).

The scan never mutates submission or claim state itself, so §26 strict
reading and reward semantics are untouched (G13/G14); it changes only
WHEN the existing pipeline gets another attempt.

The staleness signal is the newest ``submission_validations.started_at``
(not a column on submissions — the table has no ``updated_at``, and the
run row IS the fingerprint of an interrupted run: a VALIDATING
submission's newest run is by definition unfinished). A re-dispatched
run writes a fresh run row in tx1, which re-arms the clock — repeated
scans cannot hot-loop a broken row.

Retry policy: transient DB failures retry with bounded backoff (the
scan only reads and enqueues); the re-dispatched validation jobs carry
their own policy.

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
    scan's whole read.

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
    every stale-VALIDATING submission id.

    The scan writes nothing; the re-dispatched job is absorbed
    idempotently by the validation service's terminal / stale-rerun /
    twin-run gates (see the module docstring), so at-least-once
    redelivery of THIS task is equally safe.
    """
    from app.core.clock import SystemClock
    from app.core.config import get_settings

    job_id = self.request.id
    settings = get_settings()
    stale_after = timedelta(seconds=settings.stale_validating_requeue_seconds)
    logger.info(
        "requeue_stale_validating.start",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "stale_after_seconds": settings.stale_validating_requeue_seconds,
        },
    )

    async def _discover(session: AsyncSession) -> list[UUID]:
        return await collect_stale_validating_ids(
            session,
            SystemClock().now(),
            stale_after=stale_after,
            limit=SCAN_BATCH_LIMIT,
        )

    submission_ids = asyncio.run(run_with_session(_discover))
    for submission_id in submission_ids:
        validate_submission_job.delay(str(submission_id), request_id)
    payload: dict[str, Any] = {
        "request_id": request_id,
        "requeued": len(submission_ids),
        "submission_ids": [str(submission_id) for submission_id in submission_ids],
    }
    logger.info(
        "requeue_stale_validating.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
