# backend/app/workers/jobs/recover_stuck_sending.py
"""Aged-SENDING automatic recovery monitor (Plan 08 W5a carry; the S3
T4 ruling's aged-SENDING direction; spec §25.4; G8/G9).

The wedge: a delivery sender that dies between its claim commit (tx1,
status -> SENDING) and its finalize (tx2) leaves the row in SENDING
forever — the claim-before-send design's observable-crash property
(delivery_service). The existing dispatcher already re-enqueues aged
SENDING rows and the send service's claim gate re-claims them under
the SAME lease heuristic; this monitor is the complementary STATE-side
path the carry ruling asked for: a delivery that has sat in SENDING
longer than ``notification_sending_stuck_threshold_seconds`` (default
600s — sized above any healthy provider call, so a live sender is never
scooped) is flipped to RETRYABLE in one conditional UPDATE, and the
next dispatcher round re-dispatches it as ordinary due work (the row's
``scheduled_at`` is the claim-time due instant, already past). The
deterministic provider idempotency key stays the backstop against the
genuine double-send a slow original sender can still cause — the
documented V1 lease trade-off (models.py / delivery_service).

Division of labor with the manual command: this monitor is the
AUTOMATIC path and lands RETRYABLE (re-try the delivery);
``RepairService.force_fail_delivery`` is the OPERATOR path and lands
FAILED (kill it). They write different states through row locks, so
they never conflict — whichever takes a row's lock first commits its
state, and the other's predicate (status = SENDING) no longer matches.

**One conditional UPDATE, no cross-row-lock-long transaction** (the
brief's shape): a single statement

    UPDATE notification_deliveries
       SET status = 'RETRYABLE',
           last_error = 'stuck-SENDING recovery',
           updated_at = :now
     WHERE id IN (SELECT id FROM notification_deliveries
                   WHERE status = 'SENDING'
                     AND updated_at <= :now - :threshold
                   ORDER BY updated_at, id
                   LIMIT :batch)

The WHERE predicate is the mutual exclusion against the send
service's own SENDING writes: a live claim stamps ``updated_at`` from
the service clock at claim time, so ``updated_at > cutoff`` fails the
predicate and the monitor cannot touch an in-flight send that is
younger than the threshold; a row the monitor already flipped is
RETRYABLE, so at-least-once redelivery of THIS task (beat duplicate,
worker redelivery under ``task_acks_late``) re-matches nothing — the
recovery is idempotent (G8). The inner ``LIMIT`` bounds the batch
(the dispatcher's ``notification_dispatch_batch_limit`` knob), so one
beat never holds row locks across an unbounded backlog; whatever is
left drains on the next beat. A slow original sender racing the flip
is arbitrated by the send service's own finalize gate (``status !=
SENDING`` -> the observed state wins, reported as IN_FLIGHT), never by
a second write here.

Timestamp domain: the lease heuristic's clock is the SERVICE clock
(``updated_at`` carries no ORM onupdate precisely so every write is a
caller instant — models.py). The monitor therefore samples ONE
``SystemClock`` instant and uses it for both the cutoff comparison and
the ``updated_at`` stamp, exactly like the send service's claim does;
host clock skew skews the heuristic with it, never mixes two domains.

Retry policy: transient database failures (``OperationalError`` /
``DBAPIError``) autoretry with bounded backoff, the scan-task family
precedent — the UPDATE is idempotent, so a retry after a connection
blip re-recovers only what is still wedged.

Imports of sqlalchemy / app.modules stay INSIDE the functions (the
celery_app lazy-construction contract); the one module-level
sqlalchemy import is ``sqlalchemy.exc`` — the exception CLASSES the
autoretry tuple names, an env-free import exactly like
expire_claims.py's precedent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy.exc import (  # env-free: see module docstring
    DBAPIError,
    OperationalError,
)

from app.workers.session_source import run_with_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: The last_error annotation a recovered row carries, so an admin
#: failure query can tell the monitor's automatic recovery from every
#: provider-failure family ("temporary:"/"unknown_outcome:"/
#: "permanent:", delivery_service) and from the operator kill
#: ("forced:", repair_service) at a glance.
STUCK_SENDING_RECOVERY_MARKER = "stuck-SENDING recovery"

#: Transient database failures the monitor's bounded autoretry covers.
_RETRYABLE_DB_TRANSIENTS = (OperationalError, DBAPIError)

_MAX_RETRIES = 5


async def recover_stuck_sending_deliveries(
    session: AsyncSession,
    now: datetime,
    *,
    stuck_after: timedelta,
    limit: int,
) -> list[UUID]:
    """Flip deliveries that have sat SENDING past ``stuck_after`` to
    RETRYABLE — the monitor's whole write, one conditional UPDATE plus
    its commit.

    ``now`` is the caller's SERVICE-clock instant (the same domain the
    send service's claim stamps ``updated_at`` from); it serves both
    the cutoff comparison and the row's new ``updated_at``.
    ``scheduled_at`` and ``attempts`` are deliberately untouched: the
    row's due instant is already past, so the next dispatcher round
    re-dispatches it, and the next claim advances attempts under the
    send service's bounded ladder. Returns the recovered ids (in the
    database's RETURNING order).
    """
    from sqlalchemy import select, update

    from app.modules.notifications.enums import DeliveryStatus
    from app.modules.notifications.models import NotificationDelivery

    cutoff = now - stuck_after
    statement = (
        update(NotificationDelivery)
        .where(
            NotificationDelivery.id.in_(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.status == DeliveryStatus.SENDING.value,
                    NotificationDelivery.updated_at <= cutoff,
                )
                .order_by(NotificationDelivery.updated_at, NotificationDelivery.id)
                .limit(limit)
            )
        )
        .values(
            status=DeliveryStatus.RETRYABLE.value,
            last_error=STUCK_SENDING_RECOVERY_MARKER,
            updated_at=now,
        )
        .returning(NotificationDelivery.id)
    )
    recovered = list((await session.execute(statement)).scalars().all())
    await session.commit()
    return recovered


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    name="workers.recover_stuck_sending",
    autoretry_for=_RETRYABLE_DB_TRANSIENTS,
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_MAX_RETRIES,
)
def recover_stuck_sending(self: Any, request_id: str) -> dict[str, Any]:
    """Recover aged-SENDING deliveries (see the module docstring): one
    conditional UPDATE per beat, batch-bounded, idempotent under
    at-least-once delivery.

    Discovery and dispatch stay separated exactly as the dispatcher
    family rules: this task changes STATE only (SENDING -> RETRYABLE)
    and enqueues nothing — the next due-delivery scan owns the
    re-dispatch, which keeps one retry home (the service's ladder plus
    the dispatcher) instead of a racing second one.
    """
    from app.core.clock import SystemClock
    from app.core.config import get_settings

    job_id = self.request.id
    settings = get_settings()
    stuck_after = timedelta(
        seconds=settings.notification_sending_stuck_threshold_seconds
    )
    logger.info(
        "recover_stuck_sending.start",
        extra={
            "request_id": request_id,
            "job_id": job_id,
            "threshold_seconds": (
                settings.notification_sending_stuck_threshold_seconds
            ),
            "batch_limit": settings.notification_dispatch_batch_limit,
        },
    )

    async def _recover(session: AsyncSession) -> list[UUID]:
        return await recover_stuck_sending_deliveries(
            session,
            SystemClock().now(),
            stuck_after=stuck_after,
            limit=settings.notification_dispatch_batch_limit,
        )

    recovered = asyncio.run(run_with_session(_recover))
    payload: dict[str, Any] = {
        "request_id": request_id,
        "recovered": len(recovered),
        "delivery_ids": [str(delivery_id) for delivery_id in recovered],
    }
    logger.info(
        "recover_stuck_sending.end",
        extra={**payload, "job_id": job_id},
    )
    return payload
