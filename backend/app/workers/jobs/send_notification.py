# backend/app/workers/jobs/send_notification.py
"""Celery shell for idempotent notification delivery (§12 of
docs/quality/backend-engineering.md; spec §25.3/§25.4; plan 07 T4).

Orchestration only: the job receives ids and the correlation id,
constructs the dependencies, calls `DeliveryService.send`, and returns
a JSON-serializable summary. All delivery semantics — claim-before-
send, the retry ladder, the failure taxonomy, policy skips — live in
the service (`app.modules.notifications.delivery_service`), never
here.

Retry ownership (plan 07 controller decision, single retry home):
THIS JOB DOES NOT AUTORETRY on TemporaryProviderError. The service
records RETRYABLE with the next ladder scheduled_at (spec §25.4
~1m/~5m/~20m), and the plan 07 T8 due-delivery scan re-enqueues
RETRYABLE rows at their due instant. A Celery-side autoretry would be
a second retry home racing the scan (Celery's countdown vs. the row's
scheduled_at) and the two can double-send; one home converges. The
practical consequence: `send_notification_delivery` returns normally
for provider failures (the RETRY_SCHEDULED outcome is a success of the
BOOKKEEPING, not of the delivery) and Celery never re-runs it on its
own — the queue-level duplicate this job must tolerate is a re-delivered
message for the same delivery_id, which the service absorbs
idempotently.

Correlation (§15): `request_id` arrives as an explicit task argument
and is logged and passed through unchanged, never re-derived.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import select

from app.modules.notifications.delivery_service import DeliveryService

logger = logging.getLogger(__name__)


async def _claim_status(claim_id: UUID) -> str | None:
    """Deadline-suppression input: the claim's status column, or None.

    Imported lazily so that importing this job module stays environment-
    free (the celery_app lazy-construction contract); the workers layer
    MAY read across modules — the notifications service it feeds may
    not (notifications -> identity only).
    """
    from app.db.session import get_async_session_maker
    from app.modules.tasks.models import AssignmentClaim

    async with get_async_session_maker()() as session:
        # cast: session.scalar is untyped for a column select; the query
        # returns the status string or None, nothing wider.
        return cast(
            "str | None",
            await session.scalar(
                select(AssignmentClaim.status).where(AssignmentClaim.id == claim_id)
            ),
        )


def build_delivery_service() -> DeliveryService:
    """Production composition (§12 step 2): dependencies in, service out.

    The SMS/EMAIL senders are the interim Logging* adapters until this
    plan's later provider wiring replaces them at this exact call site
    (real adapters translate their SDK failures into the §13 taxonomy
    and honor `idempotency_key`; nothing else changes). Tests substitute
    the fakes by patching this factory.
    """
    from datetime import timedelta

    from app.core.clock import SystemClock
    from app.core.config import get_settings
    from app.db.session import get_async_session_maker
    from app.integrations.email import LoggingEmailSender
    from app.integrations.sms import LoggingSmsSender

    return DeliveryService(
        session_maker=get_async_session_maker(),
        sms_sender=LoggingSmsSender(),
        email_sender=LoggingEmailSender(),
        clock=SystemClock(),
        # The claim gate's lease threshold comes from the SAME setting
        # the T8 due scan reads, so the rows the scanner re-enqueues as
        # stuck are exactly the rows this gate will re-claim.
        stale_claim_threshold=timedelta(
            seconds=get_settings().notification_dispatch_stale_sending_seconds
        ),
        claim_status_resolver=_claim_status,
    )


@shared_task(  # type: ignore[untyped-decorator]
    bind=True, name="workers.send_notification_delivery"
)
def send_notification_delivery(
    self: Any, delivery_id: str, request_id: str
) -> dict[str, Any]:
    """Deliver one NotificationDelivery idempotently.

    `delivery_id` travels as its string form (JSON wire format);
    an unparseable id is a producer bug and fails loudly. Re-delivered
    duplicates of the same message are absorbed by the service
    (ALREADY_SENT / ALREADY_FAILED / IN_FLIGHT outcomes), so Celery may
    redeliver at-least-once.
    """
    job_id = self.request.id
    logger.info(
        "send_notification_delivery.start",
        extra={"request_id": request_id, "job_id": job_id},
    )
    service = build_delivery_service()
    result = asyncio.run(service.send(UUID(delivery_id), request_id))
    payload: dict[str, Any] = {
        "delivery_id": str(result.delivery_id),
        "outcome": result.outcome.value,
        "status": result.status.value if result.status is not None else None,
        "attempts": result.attempts,
    }
    logger.info(
        "send_notification_delivery.end",
        extra={**payload, "request_id": request_id, "job_id": job_id},
    )
    return payload
