# backend/app/workers/jobs/dispatch_due_notifications.py
"""Celery shell for the due-delivery dispatcher (plan 07 T8; spec
§25.3/§25.4; docs/quality/backend-engineering.md §12).

Discovery only, same ownership rule as the expiry scan: this job
DISCOVERS due delivery ids and enqueues ``workers.send_notification_
delivery`` per id; every sending/idempotency decision — claim-before-
send, the retry ladder, terminal gates — lives in the send service
(``app.modules.notifications.delivery_service``), never here. The scan
holds no locks and writes nothing, so running it twice is always safe:
rows that a send already resolved left the candidate set, and rows it
re-discovers collapse onto the send job's claim gate (ALREADY_SENT /
ALREADY_FAILED / IN_FLIGHT are expected answers, not errors).

The candidate read rides the ``(status, scheduled_at)`` composite index
(models.py) in ONE bounded batch:

- due: ``status IN (PENDING, RETRYABLE) AND scheduled_at <= now`` — the
  initial dispatches plus the RETRYABLE rungs the send service
  scheduled (§25.4 ladder). This scan is the single retry home's
  re-entry point: the send job never autoretries (see its module
  docstring), so a RETRYABLE row only ever fires again through here.
- stuck SENDING: ``status = SENDING AND updated_at <= now - threshold``
  (the T4 carry, spec §25.4 "bounded retries observable"). A sender
  that crashed between its claim commit and its finalize leaves the
  row in SENDING forever; V1 lease semantics are a TIMESTAMP HEURISTIC
  over ``updated_at`` (the claim write refreshes it) — there is no
  lease column and no owner identity, so "older than the threshold"
  (default 15 minutes, ``NOTIFICATION_DISPATCH_STALE_SENDING_SECONDS``)
  is the stand-in for "the claim died". The re-enqueued send's claim
  gate re-claims exactly those rows (a FRESH SENDING claim still
  answers IN_FLIGHT); the genuine double-send a slow original sender
  can then cause is collapsed by the deterministic provider
  idempotency key — the documented V1 trade-off of choosing the
  heuristic over a schema change.

Batch size is configurable (``NOTIFICATION_DISPATCH_BATCH_LIMIT``) and
bounds the per-beat enqueue burst; whatever a huge backlog leaves
behind is the next beat's work.

Correlation (§15): ``request_id`` arrives as an explicit task argument,
is threaded unchanged into every per-id enqueue, and is logged at both
task boundaries — never re-derived. Imports of sqlalchemy / app.db /
app.modules stay INSIDE the functions (the celery_app
lazy-construction contract: importing this module never requires a
configured environment).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]

# The per-id unit of work this scan feeds. Imported at module scope:
# importing it stays environment-free (it binds a shared_task proxy and
# imports nothing configured), and naming the producer->consumer edge
# here keeps the dispatch flow readable in one place.
from app.workers.jobs.send_notification import send_notification_delivery

logger = logging.getLogger(__name__)

#: Scan reasons, in the JSON summary and the logs.
REASON_DUE = "due"
REASON_STUCK_SENDING = "stuck_sending"


class DueDelivery(NamedTuple):
    """One scan discovery: the delivery id plus why it was due."""

    delivery_id: UUID
    reason: str


async def collect_due_deliveries(
    session: Any, now: datetime, *, limit: int, stale_after: timedelta
) -> list[DueDelivery]:
    """Due and stuck-SENDING delivery ids — the scan's whole read.

    Ordered by ``scheduled_at`` (oldest due first) so a bounded batch
    always drains the most overdue rows before the merely due.
    Discovery only — the per-id send re-judges everything under the
    row lock, so this read may be stale by the time its ids are
    processed (a raced send answers ALREADY_SENT / IN_FLIGHT there).
    """

    from sqlalchemy import and_, or_, select

    from app.modules.notifications.enums import DeliveryStatus
    from app.modules.notifications.models import NotificationDelivery

    rows = (
        await session.execute(
            select(NotificationDelivery.id, NotificationDelivery.status)
            .where(
                or_(
                    and_(
                        NotificationDelivery.status.in_(
                            (
                                DeliveryStatus.PENDING.value,
                                DeliveryStatus.RETRYABLE.value,
                            )
                        ),
                        NotificationDelivery.scheduled_at <= now,
                    ),
                    and_(
                        NotificationDelivery.status == DeliveryStatus.SENDING.value,
                        NotificationDelivery.updated_at <= now - stale_after,
                    ),
                )
            )
            .order_by(NotificationDelivery.scheduled_at, NotificationDelivery.id)
            .limit(limit)
        )
    ).all()
    return [
        DueDelivery(
            delivery_id=delivery_id,
            reason=(
                REASON_STUCK_SENDING
                if status == DeliveryStatus.SENDING.value
                else REASON_DUE
            ),
        )
        for delivery_id, status in rows
    ]


@shared_task(  # type: ignore[untyped-decorator]
    bind=True, name="workers.dispatch_due_notifications"
)
def dispatch_due_notifications(self: Any, request_id: str) -> dict[str, Any]:
    """Discovery only: enqueue one ``workers.send_notification_delivery``
    per due or stuck-SENDING delivery id.

    The scan writes nothing, so at-least-once redelivery of THIS task is
    idempotent by the same argument as the expiry scan: a re-scan
    re-discovers only rows the sends have not yet resolved, and the
    per-id job absorbs the duplicate enqueue through its claim gate.
    """

    from app.core.clock import SystemClock
    from app.core.config import get_settings
    from app.db.session import get_async_session_maker

    job_id = self.request.id
    settings = get_settings()
    stale_after = timedelta(
        seconds=settings.notification_dispatch_stale_sending_seconds
    )
    logger.info(
        "dispatch_due_notifications.start",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "batch_limit": settings.notification_dispatch_batch_limit,
            "stale_after_seconds": (
                settings.notification_dispatch_stale_sending_seconds
            ),
        },
    )

    async def _discover() -> list[DueDelivery]:
        async with get_async_session_maker()() as session:
            return await collect_due_deliveries(
                session,
                SystemClock().now(),
                limit=settings.notification_dispatch_batch_limit,
                stale_after=stale_after,
            )

    discovered = asyncio.run(_discover())
    for item in discovered:
        send_notification_delivery.delay(str(item.delivery_id), request_id)
    reasons = [item.reason for item in discovered]
    payload: dict[str, Any] = {
        "request_id": request_id,
        "enqueued": len(discovered),
        "due": reasons.count(REASON_DUE),
        "stuck_sending": reasons.count(REASON_STUCK_SENDING),
        "delivery_ids": [str(item.delivery_id) for item in discovered],
    }
    logger.info(
        "dispatch_due_notifications.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
