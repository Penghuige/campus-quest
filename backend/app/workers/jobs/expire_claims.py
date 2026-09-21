# backend/app/workers/jobs/expire_claims.py
"""Celery shells for claim expiry (plan 07 T6; spec §8.2, §11.4, §11.5/§26;
backend-engineering §12).

Two tasks, one ownership rule — the worker DISCOVERS ids, the service
DECIDES:

- ``workers.expire_claims_scan`` — discovery only: samples one SystemClock
  instant, reads the due-actionable candidate ids in grace order
  (``collect_due_claim_ids``), and enqueues one ``workers.expire_claim``
  per id. No domain decision ever happens here.
- ``workers.expire_claim`` — the per-id unit of work: opens its OWN
  session via the shared per-job source
  (``app.workers.session_source`` — one fresh engine per invocation,
  created and disposed inside the same ``asyncio.run``), samples the
  SystemClock instant for this attempt, calls
  ``ClaimService.expire_claim_if_due(session, claim_id, now)``, and
  returns the JSON payload the result reduces to. Every outcome —
  EXPIRED, NOT_DUE, PROTECTED, VALID_SUBMISSION, ALREADY_TERMINAL,
  MISSING — is a normal return, never an exception: they are the scan's
  expected answers under at-least-once delivery, not failures.

At-least-once contract (plan-04 amendment 1): celery_app pins
``task_acks_late=True`` + ``task_reject_on_worker_lost=True``, so a worker
dying mid-task redelivers instead of silently dropping the job. Both
tasks absorb redelivery idempotently — a re-run expiry against a
terminal claim answers ALREADY_TERMINAL and writes nothing, and a re-scan
re-discovers only what is still due (a claim another delivery already
expired left the actionable candidate set). Domain outcomes never
retry: NOT_DUE/PROTECTED answers are facts about the clock and the row,
and the next beat scan supersedes them. The SCAN alone additionally
autoretries transient database failures (``OperationalError`` /
``DBAPIError`` — connection blips, pool timeouts, brief unreachability)
with bounded backoff: a scan instance lost to a DB transient would
otherwise wait a full beat for its successor, and the retry is safe
because the scan itself only reads and enqueues. The per-id
``expire_claim`` keeps no autoretry — its DB transient self-heals at
the next scan (the row is still due) — and under ``task_acks_late`` an
autoretried exception is re-delivered rather than failure-acked, which
is exactly the semantics the audit's gap #3 asked to verify.

The candidate query rides the partial index ``ix_assignment_claims_
expiry_due`` (migration 0013, MERGE_CARRIES item 4 / final-review N5):
``(grace_deadline_at, id) WHERE status IN (actionable)`` — deliberately
NOT the originally deferred predicate (``... AND revision <= grace``),
which would have excluded exactly the rows a review extended past grace
(``revision > grace AND revision <= now``); the composite covers both
row classes and serves the ``ORDER BY grace_deadline_at, id``.

Correlation (§15): ``request_id`` arrives as an explicit task argument,
is threaded unchanged into every per-id enqueue, and is logged at both
task boundaries — never re-derived or regenerated. Celery retries replay
the original arguments, so every attempt of one logical scan runs under
the same request_id.

Imports of sqlalchemy / app.db / app.modules stay INSIDE the functions
(the celery_app lazy-construction contract: importing this module never
requires a configured environment). The one module-level sqlalchemy
import is ``sqlalchemy.exc`` — the exception CLASSES the scan's
autoretry tuple names, an env-free import exactly like
project_ranking_update's ``redis.exceptions`` precedent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy.exc import (
    DBAPIError,
    OperationalError,
)  # env-free: see module docstring

from app.workers.session_source import run_with_session

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.tasks.claim_service import ClaimService, ExpireResult

logger = logging.getLogger(__name__)

# One scan batch's ceiling: bounds the per-scan enqueue burst; the next
# beat scan picks up whatever a huge backlog leaves behind.
SCAN_BATCH_LIMIT = 500

#: Transient database failures the SCAN's bounded autoretry covers
#: (hardening wave: the autoretry audit's shared gap — no task had DB
#: transients in its autoretry list, so a connection blip silently
#: dropped the instance until the next beat). Env-free module-level
#: import, the project_ranking_update ``redis.exceptions`` precedent.
_RETRYABLE_DB_TRANSIENTS = (OperationalError, DBAPIError)

_MAX_RETRIES = 5


async def collect_due_claim_ids(
    session: AsyncSession, now: datetime, *, limit: int = SCAN_BATCH_LIMIT
) -> list[UUID]:
    """Due-actionable claim ids in grace order — the scan's whole read.

    Mirrors the service's due predicate: status in the actionable set,
    ``grace_deadline_at <= now``, and ``revision_deadline_at`` either
    absent or also due. Discovery only — the per-id transaction re-judges
    everything under the claim-row lock, so this read may be stale by the
    time its ids are processed (a raced claim answers PROTECTED /
    ALREADY_TERMINAL / NOT_DUE there).
    """
    from sqlalchemy import or_, select

    from app.modules.tasks.claim_service import EXPIRY_ACTIONABLE_STATUSES
    from app.modules.tasks.models import AssignmentClaim

    rows = (
        await session.scalars(
            select(AssignmentClaim.id)
            .where(
                AssignmentClaim.status.in_(EXPIRY_ACTIONABLE_STATUSES),
                AssignmentClaim.grace_deadline_at <= now,
                or_(
                    AssignmentClaim.revision_deadline_at.is_(None),
                    AssignmentClaim.revision_deadline_at <= now,
                ),
            )
            .order_by(AssignmentClaim.grace_deadline_at, AssignmentClaim.id)
            .limit(limit)
        )
    ).all()
    return list(rows)


class ValidatedSubmissionInspector:
    """The real §11.5/§26 protection reader (MERGE_CARRIES item 1,
    wired into ``build_expire_service``).

    Answers True — the due claim must NOT expire — exactly when the
    claim's CURRENT submission (``latest_submission_id``) is:

    1. machine-VALIDATED (the amendment-2 strict reading's necessary
       condition: UPLOADED / VALIDATING / VALIDATION_FAILED never
       protect);
    2. still awaiting the review pipeline — its ``review_status`` is
       PENDING_REVIEW (no teacher decision has landed; UNDER_REVIEW is
       accepted with it as the queue pool does). This refinement is
       load-bearing: a VALIDATED submission a teacher already returned
       (``review_status = REVISION_REQUIRED``, the require-revision and
       lock-invalidation outcomes) must NOT protect, or §11.4 revision
       deadlines could never expire anything — protecting any VALIDATED
       row would silently change that product rule (G13). The shape this
       clause catches is the final-review blast radius: submission
       VALIDATED committed while the chained ``on_validation_passed``
       (claim -> UNDER_REVIEW) crashed before its commit, leaving an
       actionable claim carrying a passed submission;
    3. submitted inside the protection window — ``submitted_at`` before
       the claim's effective expiry deadline (§11.5 允许提交窗口内).
       The finalize gate already enforced the then-open window, and the
       effective deadline only ever extends (grace is a claim-time
       snapshot; revision extensions grow it), so this re-check is
       defense in depth against out-of-window rows.

    The worker layer may read across modules (the composition seam —
    the notifications precedent in send_notification.py); the
    tasks-side ``ValidSubmissionInspector`` Protocol in claim_service
    stays free of submissions imports.
    """

    async def has_valid_submission(self, db: AsyncSession, claim_id: UUID) -> bool:
        from sqlalchemy import select

        from app.modules.submissions.enums import ReviewStatus, ValidationStatus
        from app.modules.submissions.models import Submission
        from app.modules.tasks.claim_service import effective_expiry_deadline
        from app.modules.tasks.models import AssignmentClaim

        # Same transaction as the caller's locked expiry judgement: the
        # claim row is already locked FOR UPDATE there, so this read is
        # consistent with the ladder's own view of the row.
        claim = await db.scalar(
            select(AssignmentClaim).where(AssignmentClaim.id == claim_id)
        )
        if claim is None or claim.latest_submission_id is None:
            return False
        current = (
            await db.execute(
                select(
                    Submission.validation_status,
                    Submission.review_status,
                    Submission.submitted_at,
                ).where(Submission.id == claim.latest_submission_id)
            )
        ).one_or_none()
        if current is None:
            # A pointer without a row (direct DB surgery): no protection.
            return False
        validation_status, review_status, submitted_at = current
        if validation_status != ValidationStatus.VALIDATED.value:
            return False
        if review_status not in (
            ReviewStatus.PENDING_REVIEW.value,
            ReviewStatus.UNDER_REVIEW.value,
        ):
            return False
        # The Row's cells arrive as Any; the comparison is the decision.
        return bool(submitted_at < effective_expiry_deadline(claim))


def build_expire_service() -> ClaimService:
    """Production composition (§12 step 2): dependencies in, service out.

    The audit seam is the interim ``LoggingEventPublisher`` until the
    audit/outbox module attaches at the composition root. The
    valid-submission seam carries the REAL VALIDATED-reading inspector
    (``ValidatedSubmissionInspector`` above, MERGE_CARRIES item 1): the
    default ``NoValidSubmissionsInspector`` now applies only to direct
    ClaimService callers that inject no inspector (service-level tests
    pinning the strict §11.5/§26 reading among them). Tests substitute
    the session maker and the service by patching around this wiring.
    """
    from app.core.clock import SystemClock
    from app.modules.identity.events import LoggingEventPublisher
    from app.modules.tasks.claim_service import ClaimService

    return ClaimService(
        clock=SystemClock(),
        event_publisher=LoggingEventPublisher(),
        valid_submission_inspector=ValidatedSubmissionInspector(),
    )


def _expire_payload(result: ExpireResult) -> dict[str, Any]:
    """The result as a JSON wire dict (§12: jobs return serializable
    summaries; every datetime reduces to its isoformat)."""
    return {
        "claim_id": str(result.claim_id),
        "outcome": result.outcome.value,
        "status": result.status.value if result.status is not None else None,
        "assignment_id": (
            str(result.assignment_id) if result.assignment_id is not None else None
        ),
        "task_id": str(result.task_id) if result.task_id is not None else None,
        "user_id": str(result.user_id) if result.user_id is not None else None,
        "terminal_at": (
            result.terminal_at.isoformat() if result.terminal_at is not None else None
        ),
    }


async def _expire_one(claim_id: UUID, now: datetime) -> dict[str, Any]:
    service = build_expire_service()

    async def _expire(session: AsyncSession) -> dict[str, Any]:
        result = await service.expire_claim_if_due(session, claim_id, now)
        return _expire_payload(result)

    return await run_with_session(_expire)


async def _discover_due(now: datetime) -> list[UUID]:
    async def _discover(session: AsyncSession) -> list[UUID]:
        return await collect_due_claim_ids(session, now)

    return await run_with_session(_discover)


@shared_task(  # type: ignore[untyped-decorator]
    bind=True, name="workers.expire_claim"
)
def expire_claim(self: Any, claim_id: str, request_id: str) -> dict[str, Any]:
    """Expire one claim idempotently (spec §8.2; §11.4 pays nothing).

    ``claim_id`` travels as its string form (JSON wire format); an
    unparseable id is a producer bug and fails loudly. The task opens its
    own session and samples its own SystemClock instant — the service,
    not the queue, owns every deadline judgement under the claim-row lock.
    """
    from app.core.clock import SystemClock

    job_id = self.request.id
    logger.info(
        "expire_claim.start",
        extra={"request_id": request_id, "job_id": job_id, "claim_id": claim_id},
    )
    payload = asyncio.run(_expire_one(UUID(claim_id), SystemClock().now()))
    logger.info(
        "expire_claim.end",
        extra={**payload, "request_id": request_id, "job_id": job_id},
    )
    return payload


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.expire_claims_scan",
    autoretry_for=_RETRYABLE_DB_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def expire_claims_scan(self: Any, request_id: str) -> dict[str, Any]:
    """Discovery only: enqueue one ``workers.expire_claim`` per due id.

    The scan holds no locks and writes nothing; staleness between this
    read and the per-id transactions is absorbed by the service's
    re-judgement (PROTECTED / ALREADY_TERMINAL / NOT_DUE are expected
    answers, not errors). Transient DB failures retry with bounded
    backoff (see the module docstring's at-least-once paragraph): the
    read-and-enqueue body is idempotent, so a re-run after a connection
    blip re-discovers only what is still due.
    """
    from app.core.clock import SystemClock

    job_id = self.request.id
    logger.info(
        "expire_claims_scan.start",
        extra={"request_id": request_id, "job_id": job_id},
    )
    claim_ids = asyncio.run(_discover_due(SystemClock().now()))
    for claim_id in claim_ids:
        expire_claim.delay(str(claim_id), request_id)
    payload: dict[str, Any] = {
        "request_id": request_id,
        "discovered": len(claim_ids),
        "claim_ids": [str(claim_id) for claim_id in claim_ids],
    }
    logger.info(
        "expire_claims_scan.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
