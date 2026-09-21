# backend/app/modules/notifications/delivery_service.py
"""Idempotent notification delivery worker service (spec §25.3/§25.4;
plan 07 T4).

`DeliveryService.send(delivery_id, request_id)` delivers ONE
NotificationDelivery row exactly once, no matter how many times the
Celery job re-fires. The design decisions, in the order a send
experiences them:

**Claim-before-send (two transactions, one row).** tx1 locks the
delivery FOR UPDATE, returns idempotently on a terminal row (SENT /
FAILED: a duplicate queue message is a no-op, not an error), refuses a
not-yet-due row (PENDING with scheduled_at > now stays untouched —
the T8 due-scan owns re-enqueueing), refuses a row another worker
already claimed (SENDING -> IN_FLIGHT result), and otherwise moves the
row to SENDING with attempts += 1 BEFORE any external call, then
COMMITS.

**Stuck-SENDING recovery (V1 lease semantics: timestamp heuristic, no
lease column).** A sender that dies between its claim commit and its
finalize leaves the row in SENDING forever. The T8 due scan re-enqueues
SENDING rows whose updated_at (refreshed by the claim write) is older
than the configurable threshold (default 15 minutes), and THIS claim
gate re-claims exactly those rows: a SENDING claim older than
`stale_claim_threshold` is presumed dead and falls through to a normal
re-claim (attempts advances again), while a fresh one still answers
IN_FLIGHT. There is no lease column and no owner identity, so a slow
original sender can race its finalize against the re-claim's outcome
write — the pre-existing finalize-lost-race guard arbitrates, and the
deterministic provider idempotency key collapses a genuine
double-send. That trade-off is the documented cost of the heuristic
over a schema change. The lease's time domain is the SERVICE clock:
the claim (and every finalize) stamps `updated_at` with the
caller-side `now`, and the column deliberately carries NO ORM onupdate
(models.py) so a DB-clock write can never sneak in — including the
subtle case where tx2 finalize re-assigns the same instant tx1 claimed
with (SQLAlchemy prunes the net-unchanged column, and with no onupdate
the row simply keeps the claim's service value). "now - updated_at"
here and the T8 scan's threshold compare instants from one domain;
host clock skew skews the heuristic with it.

Committing the claim before the provider call is what makes a crashed
provider call observable (the row is stuck in SENDING, not silently
re-firable) and what serializes two racing workers onto one provider
call: the loser blocks on the row lock and then observes the winner's
claim. tx2 (after the provider call) re-locks the row and applies the
outcome, but only if the row is still SENDING — a concurrently
forced terminal state always wins over a stale finalize.

**Division of retry ownership (single retry home).** The SERVICE owns
the retry schedule; the Celery job does NOT autoretry on provider
errors. A Temporary/UnknownOutcome failure records RETRYABLE with the
next ladder scheduled_at, and the plan 07 T8 due-delivery scan
re-enqueues RETRYABLE rows at their due instant. Giving Celery its own
autoretry would create a second, racing retry home (Celery countdown
vs. scheduled_at scan); one home converges, two can double-send.

**Retry ladder (spec §25.4).** RETRY_DELAYS is ~1m / ~5m / ~20m after
the 1st / 2nd / 3rd failure; DEFAULT_MAX_ATTEMPTS = 3, so with the
default ceiling the third failure is terminal FAILED and the 20m rung
serves deployments that configure max_attempts deeper ("最多 3 次或配置
值"). Attempts advance on every claim, so attempts == max_attempts
after a failing attempt means terminal.

**Failure taxonomy (backend-engineering §13).**
TemporaryProviderError -> RETRYABLE (next rung) or FAILED at the
ceiling; UnknownOutcomeError (timeout, side effect may have happened)
-> the SAME treatment, retried under the SAME provider idempotency key
so a real provider collapses the uncertain first attempt and the retry
onto one message — the delivery row is only ever UPDATEd, never
re-INSERTed (UNIQUE(event_key, user_id, channel) is the boundary, spec
§25.3); PermanentProviderError -> FAILED immediately, reason recorded.
last_error tokens are "temporary:", "unknown_outcome:", "permanent:".
Notification failures never roll back or touch business state (spec
§25.4): the service writes only notification_deliveries /
notifications rows.

**Policy skips are not failures (spec §25.1/§25.2).** DeliveryStatus is
frozen to five members — there is no SKIPPED status — so an eligibility
skip resolves the row as SENT with provider_message_id NULL and
last_error carrying a "skipped:<token>" marker: the deliverable is
complete BY POLICY (nothing further will ever be attempted), while
`last_error LIKE 'skipped:%'` keeps admin queries able to distinguish
policy skips from provider sends. Skip sources: the T2
`eligible_channels` re-check at dispatch (user state may have drifted
since routing — e.g. the email was unverified or the account became
BANNED; the re-check applies account-level rules, and for deadline
events a default TaskNotificationPolicy because task-level toggles
belong to routing time, not dispatch time), and the T3 dispatch-time
suppression predicate `should_send_reminder` (a claim that has moved
to VALIDATING/UNDER_REVIEW/COMPLETED/ABANDONED/EXPIRED, or whose
assignment_claim row is gone, cancels the ordinary reminder — spec
§25.2 "未发送的普通 DDL 提醒必须取消/跳过"). The claim-status read is
injected as `claim_status_resolver` because this module must not
import tasks at runtime (notifications -> identity only); the worker
job wires the real reader, tests wire fakes.

**Channels.** IN_APP: the Notification row IS the inbox message (its
title/body are the record-time render snapshot, models.py), so
dispatch calls no provider at all — it marks the delivery SENT and
clears notification.read_at (completing delivery is the moment the
message officially enters the inbox, unread). SMS/EMAIL: through the
SmsSender/EmailSender ports. The provider call passes `to`, a stable
template id (the event type slug, e.g. "assignment_deadline_4h"), the
notification snapshot as variables ({"title", "body"}), and the
idempotency key

    {event_key}:{CHANNEL}:{user_id}

e.g. "claim:<uuid>:deadline_4h:SMS:<uuid>". Per-channel text is NOT
re-rendered at dispatch: the T1 schema persists no event payload, so
the per-event render variables exist only at record time (T5 renders
the snapshot); the ports' frozen contract renders the final message
provider-side from the template id + variables (app/integrations/
sms.py, email.py). provider_message_id stays NULL on success because
the V1 ports return None; the column is the seam for adapters that
later surface provider receipts.

Provider sends happen with NO row lock or session held (§7: the lock
spans only the tiny state transitions, never external I/O).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.integrations.email import EmailSender
from app.integrations.errors import (
    PermanentProviderError,
    TemporaryProviderError,
    UnknownOutcomeError,
)
from app.integrations.sms import SmsSender
from app.modules.identity.models import User
from app.modules.notifications.deadline_scheduler import should_send_reminder
from app.modules.notifications.enums import (
    SKIPPED_LAST_ERROR_PREFIX,
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.notifications.service import (
    DEADLINE_REMINDER_EVENTS,
    ChannelDecision,
    ChannelStatus,
    TaskNotificationPolicy,
    eligible_channels,
)

logger = logging.getLogger(__name__)

#: Spec §25.4 retry ladder: delay after the 1st / 2nd / 3rd failure
#: (~1m / ~5m / ~20m). Index is (attempts already made - 1); a
#: configured max_attempts deeper than the ladder re-uses the last rung.
RETRY_DELAYS: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=20),
)

#: Spec §25.4 "最多 3 次或配置值": attempts at or beyond this ceiling
#: after a failing attempt resolve FAILED (terminal).
DEFAULT_MAX_ATTEMPTS = 3

#: V1 lease stand-in: a SENDING claim older than this is presumed dead
#: (crashed sender) and may be re-claimed by the next arrival — the
#: same threshold the T8 due scan uses to decide which SENDING rows to
#: re-enqueue (production wiring feeds both from
#: `NOTIFICATION_DISPATCH_STALE_SENDING_SECONDS` so scanner and gate
#: can never disagree).
DEFAULT_STALE_CLAIM_THRESHOLD = timedelta(minutes=15)

# Deadline reminder event keys are "claim:<uuid>:deadline_24h" /
# "deadline_4h" (deadline_scheduler.deadline_event_key); the claim id
# is what the suppression resolver reads.
_CLAIM_EVENT_KEY_RE = re.compile(
    r"^claim:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):"
)

#: Type of the injected deadline-claim status reader (event_key's claim
#: id -> AssignmentClaim.status as the raw column string, or None when
#: the claim row no longer exists).
ClaimStatusResolver = Callable[[UUID], Awaitable[str | None]]


def provider_idempotency_key(
    event_key: str, channel: NotificationChannel, user_id: UUID
) -> str:
    """The deterministic provider-side idempotency key for one delivery.

    Derived purely from the row identity, so every attempt of one
    delivery — including an UnknownOutcome retry whose first attempt
    may have succeeded at the provider — lands on the same key.
    """
    return f"{event_key}:{channel.value}:{user_id}"


class SendOutcome(StrEnum):
    """What THIS send call did (the row status may differ; see
    SendResult.status)."""

    SENT = "SENT"
    #: Terminal SENT achieved by policy skip, not a provider send.
    SKIPPED = "SKIPPED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"
    #: Row was already SENT when this call arrived (idempotent no-op).
    ALREADY_SENT = "ALREADY_SENT"
    #: Row was already FAILED when this call arrived.
    ALREADY_FAILED = "ALREADY_FAILED"
    #: scheduled_at is in the future; the T8 scan re-enqueues later.
    NOT_DUE = "NOT_DUE"
    #: Another worker holds the SENDING claim.
    IN_FLIGHT = "IN_FLIGHT"
    #: No such delivery row (bad queue payload).
    MISSING = "MISSING"


@dataclass(frozen=True)
class SendResult:
    """Observable outcome of one send call, JSON-friendly via the job."""

    delivery_id: UUID
    outcome: SendOutcome
    #: The delivery row's status after this call (None only for
    #: MISSING, where there is no row to report).
    status: DeliveryStatus | None
    attempts: int
    scheduled_at: datetime | None = None
    #: The "skipped:<token>" marker for SKIPPED outcomes.
    skip_reason: str | None = None


@dataclass(frozen=True)
class _Claim:
    """Snapshot of the claimed row + dispatch context, taken inside tx1.

    Plain values only: nothing downstream may depend on ORM instances
    surviving the claim transaction (the session is closed before any
    provider call).
    """

    delivery_id: UUID
    notification_id: UUID
    user_id: UUID
    event_key: str
    event_type: NotificationEventType
    channel: NotificationChannel
    attempts: int
    scheduled_at: datetime
    title: str
    body: str
    phone_e164: str | None
    email_normalized: str | None
    #: T2 re-check skip reason for this channel, None when eligible.
    channel_skip_reason: str | None


class DeliveryService:
    """Deliver one NotificationDelivery idempotently (see module
    docstring for the full contract)."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        sms_sender: SmsSender,
        email_sender: EmailSender,
        clock: Clock,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        stale_claim_threshold: timedelta = DEFAULT_STALE_CLAIM_THRESHOLD,
        claim_status_resolver: ClaimStatusResolver | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        if stale_claim_threshold.total_seconds() < 1:
            raise ValueError(
                "stale_claim_threshold must be at least 1 second, got "
                f"{stale_claim_threshold!r}"
            )
        self._session_maker = session_maker
        self._sms_sender = sms_sender
        self._email_sender = email_sender
        self._clock = clock
        self._max_attempts = max_attempts
        self._stale_claim_threshold = stale_claim_threshold
        self._claim_status_resolver = claim_status_resolver

    async def send(self, delivery_id: UUID, request_id: str) -> SendResult:
        """Deliver `delivery_id` once; duplicate calls are no-ops.

        `request_id` is the caller's correlation id (§15): logged, never
        re-derived, and threaded unchanged through retries.
        """
        now = self._clock.now()
        claimed = await self._claim(delivery_id, now)
        if not isinstance(claimed, _Claim):
            return claimed  # an early-out SendResult

        skip_token = await self._dispatch_skip_token(claimed, now)
        if skip_token is not None:
            return await self._finalize(
                claimed.delivery_id,
                now,
                outcome=SendOutcome.SKIPPED,
                status=DeliveryStatus.SENT,
                last_error=skip_token,
            )

        if claimed.channel is NotificationChannel.IN_APP:
            # The Notification row IS the in-app message: no provider
            # port, snapshot content already persisted at record time.
            return await self._finalize(
                claimed.delivery_id,
                now,
                outcome=SendOutcome.SENT,
                status=DeliveryStatus.SENT,
                last_error=None,
                clear_notification_read_at=True,
            )

        return await self._send_via_provider(claimed, now)

    # --- tx1: claim ----------------------------------------------------------------

    async def _claim(self, delivery_id: UUID, now: datetime) -> _Claim | SendResult:
        """Lock, decide, and claim the row (tx1; commits the claim).

        Returns a `_Claim` snapshot when this call owns the send, or a
        `SendResult` explaining why nothing will be sent.
        """
        async with self._session_maker() as session, session.begin():
            delivery = (
                await session.execute(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if delivery is None:
                return SendResult(
                    delivery_id=delivery_id,
                    outcome=SendOutcome.MISSING,
                    status=None,
                    attempts=0,
                )
            if delivery.status == DeliveryStatus.SENT.value:
                return self._observed(delivery, SendOutcome.ALREADY_SENT)
            if delivery.status == DeliveryStatus.FAILED.value:
                return self._observed(delivery, SendOutcome.ALREADY_FAILED)
            if delivery.status == DeliveryStatus.SENDING.value:
                # Another worker claimed first and may be mid-call.
                # V1 lease semantics (see module docstring): the claim
                # write refreshed updated_at, so a claim younger than
                # the threshold is live -> IN_FLIGHT; an older one is
                # presumed dead (crashed sender; the T8 scan
                # re-enqueued exactly these) and falls through to a
                # normal re-claim below.
                if now - delivery.updated_at < self._stale_claim_threshold:
                    return self._observed(delivery, SendOutcome.IN_FLIGHT)
                logger.warning(
                    "notification_delivery.stale_claim_reclaimed",
                    extra={
                        "delivery_id": str(delivery_id),
                        "claim_updated_at": (
                            delivery.updated_at.isoformat()
                            if delivery.updated_at is not None
                            else None
                        ),
                        "stale_threshold_seconds": (
                            self._stale_claim_threshold.total_seconds()
                        ),
                    },
                )
            if delivery.scheduled_at > now:
                return self._observed(delivery, SendOutcome.NOT_DUE)

            # PENDING or RETRYABLE and due: claim before sending. The
            # claim stamps updated_at from the SERVICE clock (the same
            # domain the staleness check below judges in); the column
            # has no ORM onupdate (models.py), so every write is the
            # caller's clock and the lease never mixes two time
            # domains.
            delivery.status = DeliveryStatus.SENDING.value
            delivery.attempts += 1
            delivery.updated_at = now

            notification = await session.get(Notification, delivery.notification_id)
            user = await session.get(User, delivery.user_id)
            if notification is None or user is None:
                # FK-backed: only reachable with a corrupted store.
                raise LookupError(
                    f"delivery {delivery_id} references missing "
                    f"notification {delivery.notification_id} or "
                    f"user {delivery.user_id}"
                )
            event_type = NotificationEventType(notification.event_type)
            channel = NotificationChannel(delivery.channel)
            decision = _channel_decision(user, event_type, channel)
            return _Claim(
                delivery_id=delivery.id,
                notification_id=notification.id,
                user_id=delivery.user_id,
                event_key=delivery.event_key,
                event_type=event_type,
                channel=channel,
                attempts=delivery.attempts,
                scheduled_at=delivery.scheduled_at,
                title=notification.title,
                body=notification.body,
                phone_e164=user.phone_e164,
                email_normalized=user.email_normalized,
                channel_skip_reason=(
                    decision.reason
                    if decision.status is ChannelStatus.SKIPPED
                    else None
                ),
            )

    @staticmethod
    def _observed(delivery: NotificationDelivery, outcome: SendOutcome) -> SendResult:
        """A no-op SendResult describing an observed row state."""
        return SendResult(
            delivery_id=delivery.id,
            outcome=outcome,
            status=DeliveryStatus(delivery.status),
            attempts=delivery.attempts,
            scheduled_at=delivery.scheduled_at,
        )

    # --- dispatch-time policy --------------------------------------------------------

    async def _dispatch_skip_token(self, claimed: _Claim, now: datetime) -> str | None:
        """The "skipped:<token>" marker when dispatch policy cancels the
        send, or None when the delivery should proceed."""
        if claimed.channel_skip_reason is not None:
            return f"{SKIPPED_LAST_ERROR_PREFIX}{claimed.channel_skip_reason}"

        if (
            claimed.event_type in DEADLINE_REMINDER_EVENTS
            and self._claim_status_resolver is not None
        ):
            claim_id = _claim_id_from_event_key(claimed.event_key)
            if claim_id is not None:
                claim_status = await self._claim_status_resolver(claim_id)
                if claim_status is None or not should_send_reminder(
                    claim_status, now, claimed.scheduled_at
                ):
                    status_token = (
                        claim_status if claim_status is not None else "missing"
                    )
                    return (
                        f"{SKIPPED_LAST_ERROR_PREFIX}"
                        f"deadline_claim_status:{status_token}"
                    )
        return None

    # --- provider dispatch ------------------------------------------------------------

    async def _send_via_provider(self, claimed: _Claim, now: datetime) -> SendResult:
        idempotency_key = provider_idempotency_key(
            claimed.event_key, claimed.channel, claimed.user_id
        )
        template_id = claimed.event_type.value.lower()
        variables: dict[str, str] = {"title": claimed.title, "body": claimed.body}
        try:
            if claimed.channel is NotificationChannel.SMS:
                if claimed.phone_e164 is None:
                    # The T2 re-check guarantees a bound phone for an
                    # eligible SMS delivery; reaching here means the
                    # re-check was bypassed (programming error).
                    raise LookupError(
                        f"SMS delivery {claimed.delivery_id} has no bound phone"
                    )
                self._sms_sender.send(
                    to=claimed.phone_e164,
                    template=template_id,
                    variables=variables,
                    idempotency_key=idempotency_key,
                )
            else:
                if claimed.email_normalized is None:
                    raise LookupError(
                        f"EMAIL delivery {claimed.delivery_id} has no address"
                    )
                self._email_sender.send(
                    to=claimed.email_normalized,
                    template=template_id,
                    variables=variables,
                    idempotency_key=idempotency_key,
                )
        except PermanentProviderError as exc:
            return await self._finalize(
                claimed.delivery_id,
                now,
                outcome=SendOutcome.FAILED,
                status=DeliveryStatus.FAILED,
                last_error=f"permanent:{exc}",
            )
        except UnknownOutcomeError as exc:
            return await self._retry_or_fail(claimed, now, f"unknown_outcome:{exc}")
        except TemporaryProviderError as exc:
            return await self._retry_or_fail(claimed, now, f"temporary:{exc}")

        # Ports return None in V1, so there is no provider receipt id
        # yet; provider_message_id is the seam for adapters that add
        # one (set explicitly to keep that contract visible here).
        return await self._finalize(
            claimed.delivery_id,
            now,
            outcome=SendOutcome.SENT,
            status=DeliveryStatus.SENT,
            last_error=None,
        )

    async def _retry_or_fail(
        self, claimed: _Claim, now: datetime, error_token: str
    ) -> SendResult:
        """Bounded retry bookkeeping for temporary/unknown failures."""
        if claimed.attempts >= self._max_attempts:
            return await self._finalize(
                claimed.delivery_id,
                now,
                outcome=SendOutcome.FAILED,
                status=DeliveryStatus.FAILED,
                last_error=error_token,
            )
        rung = min(claimed.attempts, len(RETRY_DELAYS)) - 1
        return await self._finalize(
            claimed.delivery_id,
            now,
            outcome=SendOutcome.RETRY_SCHEDULED,
            status=DeliveryStatus.RETRYABLE,
            last_error=error_token,
            retry_due_at=now + RETRY_DELAYS[rung],
        )

    # --- tx2: finalize --------------------------------------------------------------

    async def _finalize(
        self,
        delivery_id: UUID,
        now: datetime,
        *,
        outcome: SendOutcome,
        status: DeliveryStatus,
        last_error: str | None,
        retry_due_at: datetime | None = None,
        clear_notification_read_at: bool = False,
    ) -> SendResult:
        """Apply the dispatch outcome to the row (tx2, row re-locked).

        Writes only when the row still holds this call's SENDING claim:
        a concurrently forced terminal state wins and is reported as
        the observed outcome instead.
        """
        async with self._session_maker() as session, session.begin():
            delivery = (
                await session.execute(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if delivery is None:
                return SendResult(
                    delivery_id=delivery_id,
                    outcome=SendOutcome.MISSING,
                    status=None,
                    attempts=0,
                )
            if delivery.status != DeliveryStatus.SENDING.value:
                logger.warning(
                    "notification_delivery.finalize_lost_race",
                    extra={
                        "delivery_id": str(delivery_id),
                        "current_status": delivery.status,
                    },
                )
                if delivery.status == DeliveryStatus.SENT.value:
                    return self._observed(delivery, SendOutcome.ALREADY_SENT)
                if delivery.status == DeliveryStatus.FAILED.value:
                    return self._observed(delivery, SendOutcome.ALREADY_FAILED)
                return self._observed(delivery, SendOutcome.IN_FLIGHT)

            delivery.status = status.value
            delivery.updated_at = now
            delivery.last_error = last_error
            delivery.provider_message_id = None
            if status is DeliveryStatus.SENT:
                delivery.sent_at = now
            if retry_due_at is not None:
                delivery.scheduled_at = retry_due_at
            if clear_notification_read_at:
                notification = await session.get(Notification, delivery.notification_id)
                if notification is not None:
                    notification.read_at = None
            return SendResult(
                delivery_id=delivery.id,
                outcome=outcome,
                status=status,
                attempts=delivery.attempts,
                scheduled_at=retry_due_at or delivery.scheduled_at,
                skip_reason=(last_error if outcome is SendOutcome.SKIPPED else None),
            )


def _channel_decision(
    user: User,
    event_type: NotificationEventType,
    channel: NotificationChannel,
) -> ChannelDecision:
    """The T2 `eligible_channels` decision for one channel.

    Account-level drift is the dispatch-time concern, so deadline
    reminder events pass a default TaskNotificationPolicy: task-level
    toggles were applied when the delivery was routed and are not
    re-derived here (routing-time policy, dispatch-time user state).
    """
    policy = (
        TaskNotificationPolicy() if event_type in DEADLINE_REMINDER_EVENTS else None
    )
    decisions = eligible_channels(user, policy, event_type)
    index = list(NotificationChannel).index(channel)
    return decisions[index]


def _claim_id_from_event_key(event_key: str) -> UUID | None:
    """The claim id of a "claim:<uuid>:..." event key, else None."""
    match = _CLAIM_EVENT_KEY_RE.match(event_key)
    if match is None:
        return None
    return UUID(match.group(1))
