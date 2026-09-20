# backend/app/modules/notifications/port.py
"""The cross-module notification port: same-transaction persistence of
notification intent (spec §25; plan 07 task 5; interfaces.md "Core
Primitives" outbox rule and "Cross-module ports").

`NotificationPort.record_event(session, event_key, event_type,
user_id, payload, task_policy=None)` is what domain services call
INSIDE their business transaction:

- **The caller owns the transaction.** Every row is written through the
  caller's session; record_event never commits, rolls back, or flushes
  beyond executing its own INSERTs. The intent therefore commits with
  the domain change it describes or not at all — the outbox rule that
  rules out "event sent but DB commit failed" dual-write bugs. Celery
  dispatch (T4/T8) only ever sees committed rows.
- **Idempotency is the database's.** The logical row inserts with ON
  CONFLICT DO NOTHING on (event_key, user_id) and each channel
  delivery on (event_key, user_id, channel) (spec §25.3): a duplicate
  event, a re-arm, or two racing transactions collapse onto the
  existing rows instead of inserting. First write wins the snapshot —
  a re-recorded event can never rewrite an already-created
  notification (the claim-time snapshot philosophy, models.py).
- **SINGLE-RENDER DECISION (resolving the T4 carry):** the title/body
  snapshot is rendered EXACTLY ONCE per logical event, at registration
  time, from the IN_APP template (`templates.render_template` — the
  T2 renderer). The Notification row IS the in-app message, and IN_APP
  eligibility is implied by any other channel's eligibility (SMS/EMAIL
  carry strictly harder account requirements), so the IN_APP render
  serves every channel's bookkeeping. Per-channel SMS/EMAIL text is
  NOT re-rendered here or at dispatch: the provider-side contract
  (delivery_service) sends the template slug
  ``event_type.value.lower()`` — e.g. ``revision_required``,
  ``assignment_deadline_4h``, ``reward_redemption_approved`` — plus
  the snapshot variables ``{"title", "body"}``, and the provider
  renders channel-appropriate copy from its own template registry.
  One render, one snapshot, three channels; a Plan 08 Admin template
  edit can therefore never rewrite or fork an in-flight message.
- **Render failures raise.** `InvalidTemplateError` /
  `MissingTemplateVariableError` / non-str coercion `TypeError` are
  programming errors (a bad template row or a malformed payload), not
  provider drift — they surface inside the domain transaction instead
  of silently dropping a notification. Provider failures, by
  contrast, live entirely in dispatch (spec §25.4: notification
  failures never roll back business state).

Routing — which rows to write, at which due instant, on which channels
— comes from `event_handlers.build_record_intents` (the event-type ->
intent mapping, including the CLAIM_CREATED planning trigger and the
spec §25.2 validation-failure re-arm). `task_policy` accepts the Task
row itself (duck-typed: `TaskNotificationPolicy.from_task` reads
exactly notify_24h / notify_4h / notification_channels) so tasks-side
callers pass what they already hold without importing notifications
types.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.event_handlers import RecordIntent, build_record_intents
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.notifications.service import TaskNotificationPolicy
from app.modules.notifications.templates import (
    EVENT_VARIABLES,
    RenderedMessage,
    render_template,
)

if TYPE_CHECKING:
    # Annotation-only: the runtime dependency direction stays
    # notifications -> identity (see service.py).
    from app.modules.tasks.models import Task


class TaskPolicySource(Protocol):
    """The task-side attributes a duck-typed `task_policy` provides
    (read-only view; the ORM Task satisfies this structurally)."""

    @property
    def notify_24h(self) -> bool | None: ...

    @property
    def notify_4h(self) -> bool | None: ...

    @property
    def notification_channels(self) -> list[str] | None: ...


def _normalize_task_policy(
    task_policy: TaskNotificationPolicy | TaskPolicySource | None,
) -> TaskNotificationPolicy | None:
    """A ready policy passes through; a task-like row is snapshotted;
    None stays None (critical events need no policy)."""

    if task_policy is None or isinstance(task_policy, TaskNotificationPolicy):
        return task_policy
    # The cast is honest: from_task's runtime contract is exactly the
    # three TaskPolicySource attributes; the annotation just cannot say
    # so across the module seam.
    return TaskNotificationPolicy.from_task(cast("Task", task_policy))


def _render_value(value: Any) -> str:
    """Coerce one payload value to the str the renderer requires.

    The port IS the formatting call site (templates.py leaves timezone
    and number presentation to the caller, backend-engineering §11):
    datetimes render as ISO-8601 instants, ints and Decimals as exact
    decimal text (§31.1/§31.14 — binary floats have no defined points
    text and raise, mirroring the renderer's own boundary). Strings
    pass through; every other type is a caller bug.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        raise TypeError(
            f"notification template variable has no text form: bool {value!r}"
        )
    if isinstance(value, (int, Decimal)):
        return str(value)
    raise TypeError(
        f"notification template variable of type {type(value).__name__} has no "
        "defined text form; format it at the call site"
    )


class NotificationPort:
    """Persists notification intent inside the caller's transaction
    (see module docstring for the full contract)."""

    def __init__(self, *, clock: Clock) -> None:
        """`clock` supplies the registration instant: critical events
        are due now; deadline planning measures §25.2 window membership
        against it. Production wiring shares the SystemClock with the
        calling service; tests share one FrozenClock so boundary cases
        (exactly-24h-left) stay exact."""

        self._clock = clock

    async def record_event(
        self,
        session: AsyncSession,
        event_key: str,
        event_type: NotificationEventType | str,
        user_id: UUID,
        payload: Mapping[str, Any],
        task_policy: TaskNotificationPolicy | TaskPolicySource | None = None,
    ) -> None:
        """Record one business event: render the snapshot, create the
        logical Notification and its per-channel PENDING deliveries —
        all via `session`, all idempotent on the §25.3 unique keys, and
        all uncommitted (the caller's transaction decides).

        Raises ValueError for an unknown event type or a deadline
        (re)plan without its task policy, LookupError for a missing
        user, and the templates render errors for a malformed template
        or payload — programming errors, surfaced not swallowed.
        """

        now = self._clock.now()
        user = await session.get(User, user_id)
        if user is None:
            raise LookupError(
                f"notification event {event_key!r} references missing user {user_id}"
            )
        intents = build_record_intents(
            user=user,
            event_key=event_key,
            event_type=event_type,
            payload=payload,
            now=now,
            task_policy=_normalize_task_policy(task_policy),
        )
        for intent in intents:
            await self._persist_intent(session, intent, payload)

    async def _persist_intent(
        self,
        session: AsyncSession,
        intent: RecordIntent,
        payload: Mapping[str, Any],
    ) -> None:
        """Persist one intent: render once, insert the logical row,
        insert the per-channel deliveries — each insert-on-conflict."""

        if not intent.channels:
            # An event with no eligible channel persists nothing (the
            # planner's rule, applied uniformly): no inbox row, no
            # delivery rows.
            return
        variables = {
            name: _render_value(payload[name])
            for name in EVENT_VARIABLES[intent.event_type]
            if name in payload
        }
        rendered = render_template(
            intent.event_type, NotificationChannel.IN_APP, variables
        )
        notification_id = await self._insert_notification(session, intent, rendered)
        for channel in intent.channels:
            await session.execute(
                pg_insert(NotificationDelivery)
                .values(
                    notification_id=notification_id,
                    user_id=intent.user_id,
                    event_key=intent.event_key,
                    channel=channel.value,
                    status=DeliveryStatus.PENDING.value,
                    scheduled_at=intent.scheduled_at,
                    attempts=0,
                )
                # Re-arm safe: a still-future reminder under a key that
                # already has this channel collapses onto the existing
                # row; a genuinely new channel (eligibility drifted)
                # inserts.
                .on_conflict_do_nothing(
                    index_elements=["event_key", "user_id", "channel"]
                )
            )

    async def _insert_notification(
        self,
        session: AsyncSession,
        intent: RecordIntent,
        rendered: RenderedMessage,
    ) -> UUID:
        """Insert the logical Notification idempotently and return its
        id — the existing row's id on conflict (first write wins the
        snapshot; ours is discarded)."""

        result = await session.execute(
            pg_insert(Notification)
            .values(
                user_id=intent.user_id,
                event_key=intent.event_key,
                event_type=intent.event_type.value,
                title=rendered.title,
                body=rendered.body,
            )
            .on_conflict_do_nothing(index_elements=["event_key", "user_id"])
            .returning(Notification.id)
        )
        inserted: UUID | None = result.scalar_one_or_none()
        if inserted is not None:
            return inserted
        existing: UUID | None = await session.scalar(
            select(Notification.id).where(
                Notification.event_key == intent.event_key,
                Notification.user_id == intent.user_id,
            )
        )
        if existing is None:
            # Unreachable: the insert conflicted, so the row exists.
            raise LookupError(
                f"notification {intent.event_key!r} for user {intent.user_id} "
                "conflicted but cannot be re-read"
            )
        return existing
