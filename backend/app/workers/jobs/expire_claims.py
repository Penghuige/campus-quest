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
  session via the process-wide session maker, samples the SystemClock
  instant for this attempt, calls
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
expired left the actionable candidate set). No task autoretries:
NOT_DUE/PROTECTED answers are facts about the clock and the row, and the
next beat scan supersedes them.

The candidate query is index-ready WITHOUT a migration: it selects the
actionable status set with both deadline columns due, ordered by
``grace_deadline_at`` — the shape a partial index on
``(grace_deadline_at) WHERE status IN (actionable) AND (revision IS NULL
OR revision <= grace)`` serves. It deliberately does NOT add that index
here (a new 0011 would collide with the plans branch's 0011); the index
lands with the beat schedule wiring at merge (MERGE_CARRIES.md items 4-5).

Correlation (§15): ``request_id`` arrives as an explicit task argument,
is threaded unchanged into every per-id enqueue, and is logged at both
task boundaries — never re-derived or regenerated. Celery retries replay
the original arguments, so every attempt of one logical scan runs under
the same request_id.

Imports of sqlalchemy / app.db / app.modules stay INSIDE the functions
(the celery_app lazy-construction contract: importing this module never
requires a configured environment).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.tasks.claim_service import ClaimService, ExpireResult

logger = logging.getLogger(__name__)

# One scan batch's ceiling: bounds the per-scan enqueue burst; the next
# beat scan picks up whatever a huge backlog leaves behind.
SCAN_BATCH_LIMIT = 500


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


def build_expire_service() -> ClaimService:
    """Production composition (§12 step 2): dependencies in, service out.

    The audit seam is the interim ``LoggingEventPublisher`` until the
    audit/outbox module attaches at the composition root. The
    valid-submission seam keeps its default (``NoValidSubmissionsInspector``,
    strict §11.5/§26 reading: only a machine-VALIDATED submission
    protects) — the stream that owns submission validation swaps the real
    VALIDATED-reading inspector in at this exact call site
    (MERGE_CARRIES.md item 1). Tests
    substitute the session maker and the service by patching around this
    wiring.
    """
    from app.core.clock import SystemClock
    from app.modules.identity.events import LoggingEventPublisher
    from app.modules.tasks.claim_service import ClaimService

    return ClaimService(
        clock=SystemClock(),
        event_publisher=LoggingEventPublisher(),
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
    from app.db.session import get_async_session_maker

    service = build_expire_service()
    async with get_async_session_maker()() as session:
        result = await service.expire_claim_if_due(session, claim_id, now)
    return _expire_payload(result)


async def _discover_due(now: datetime) -> list[UUID]:
    from app.db.session import get_async_session_maker

    async with get_async_session_maker()() as session:
        return await collect_due_claim_ids(session, now)


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
    bind=True, name="workers.expire_claims_scan"
)
def expire_claims_scan(self: Any, request_id: str) -> dict[str, Any]:
    """Discovery only: enqueue one ``workers.expire_claim`` per due id.

    The scan holds no locks and writes nothing; staleness between this
    read and the per-id transactions is absorbed by the service's
    re-judgement (PROTECTED / ALREADY_TERMINAL / NOT_DUE are expected
    answers, not errors).
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
