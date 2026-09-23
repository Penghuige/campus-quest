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
- **PER-CHANNEL SNAPSHOT, ONE NAMESPACE (PR #5 gfix D, Option A):**
  the title/body snapshot is rendered EXACTLY ONCE per channel, HERE,
  while the raw event payload is still in hand, through
  `templates.render_template` — the SAME event-variable namespace the
  W4 write gate validates (`EVENT_VARIABLES`). "The write gate accepts
  it" therefore implies "the production path renders it"
  structurally. The IN_APP render freezes into the canonical
  Notification row (`title`/`body` — the row the inbox lists). Each
  SMS/EMAIL channel that has an ENABLED managed row gets its finished
  text frozen into a per-channel SNAPSHOT Notification row keyed
  ``event_key + ":" + channel`` (models.py documents the reserved
  suffix namespace), and that channel's delivery row points at it;
  the dispatch worker then sends whatever its delivery points at and
  interprets nothing. A channel with no row, a disabled row, or a row
  that fails to render keeps the historic contract: its delivery
  points at the canonical row and the provider call carries the
  snapshot pair plus the event-type slug, the provider rendering
  channel-appropriate copy from its own registry. Send decisions are
  NEVER made here — `eligible_channels` and the Task channel policy
  own them, with or without template rows.
- **Managed rows are consumed HERE, and enabled=false means "no
  override" (Option A, overturning the gfix-C channel-off reading):**
  the record point queries the (event_type, channel) rows inside the
  caller's transaction; an enabled row renders through the
  ``template=`` seam (its text replaces the seed), a DISABLED or
  missing row falls back to `DEFAULT_TEMPLATES` (G7 seed semantics).
  Spec §25.5 names the `enabled` column but spells no disabled
  semantics; the spec's send switches are the Task channel policy
  (§25.1: notify_24h / notify_4h / per-channel toggles) and IN_APP is
  the V1 fallback channel (§25) that critical events always fan out
  to (service.py) — so a template row must never silently suppress a
  delivery: disabled template copy degrades to the seed text, it does
  not turn a channel off.
- **Row render failures degrade; payload failures raise.** A managed
  row that fails to render (a pre-gate legacy row, or a whitelisted
  variable this payload does not carry) falls back — IN_APP to the
  seed render for the snapshot, SMS/EMAIL to the historic no-override
  contract — with an ERROR log; the record itself never fails on
  admin copy. A SEED render that fails is a caller/payload bug and
  still raises inside the domain transaction, exactly as before:
  programming errors surface, admin copy degrades loudly.
- **Seed render failures raise.** `InvalidTemplateError` /
  `MissingTemplateVariableError` / non-str coercion `TypeError` on a
  SEED render are programming errors (a malformed payload), not
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

import logging
from collections.abc import Mapping
from dataclasses import replace
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
from app.modules.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationTemplate,
)
from app.modules.notifications.service import TaskNotificationPolicy
from app.modules.notifications.templates import (
    EVENT_VARIABLES,
    RenderedMessage,
    TemplateRenderError,
    TemplateText,
    render_template,
)

logger = logging.getLogger(__name__)

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


def _snapshot_event_key(event_key: str, channel: NotificationChannel) -> str:
    """The event key of a per-channel snapshot row: the caller's key
    plus the channel code (``...:SMS`` / ``...:EMAIL``). The suffixed
    namespace is reserved for these rows (models.py); it keeps them
    inside the same idempotent (event_key, user_id) uniqueness domain
    the canonical row uses, so a re-record collapses onto the existing
    snapshot instead of forking the frozen text."""

    return f"{event_key}:{channel.value}"


def _row_text(row: NotificationTemplate) -> TemplateText:
    """A managed row as a render source (the ``template=`` seam)."""

    return TemplateText(title=row.title, body=row.template_body)


async def _channel_template(
    session: AsyncSession,
    event_type: NotificationEventType,
    channel: NotificationChannel,
) -> NotificationTemplate | None:
    """The (event_type, channel) NotificationTemplate row, or None —
    read through the CALLER's session so every managed-template
    consult joins the registration transaction (one PG, one commit
    decision; UNIQUE(event_type, channel) bounds it to one row)."""

    template: NotificationTemplate | None = await session.scalar(
        select(NotificationTemplate).where(
            NotificationTemplate.event_type == event_type.value,
            NotificationTemplate.channel == channel.value,
        )
    )
    return template


def _render_in_app_snapshot(
    intent: RecordIntent,
    row: NotificationTemplate | None,
    variables: Mapping[str, str],
) -> RenderedMessage:
    """The canonical snapshot: an enabled (event_type, IN_APP) row
    renders through the seam; a disabled row, no row, or a row that
    fails to render falls back to the seed (the enabled-A semantics —
    disabled is "no override", never "no notification"). Seed render
    failures propagate: a payload missing a whitelisted variable is a
    caller bug, surfaced here as always."""

    if row is not None and row.enabled:
        try:
            return render_template(
                intent.event_type,
                NotificationChannel.IN_APP,
                variables,
                template=_row_text(row),
            )
        except TemplateRenderError as exc:
            logger.error(
                "notification_registration.template_render_failed",
                extra={
                    "event_key": intent.event_key,
                    "event_type": intent.event_type.value,
                    "channel": NotificationChannel.IN_APP.value,
                    "error": str(exc),
                },
            )
    return render_template(intent.event_type, NotificationChannel.IN_APP, variables)


async def _render_channel_product(
    session: AsyncSession,
    intent: RecordIntent,
    channel: NotificationChannel,
    variables: Mapping[str, str],
) -> RenderedMessage | None:
    """The frozen per-channel product for an SMS/EMAIL channel, or None
    when the channel keeps the historic contract (no row, a disabled
    row, or a row that fails to render — logged, never fatal: the
    record must not fail on admin copy, and the send decision belongs
    to channel policy, not to template rows)."""

    row = await _channel_template(session, intent.event_type, channel)
    if row is None or not row.enabled:
        return None
    try:
        return render_template(
            intent.event_type, channel, variables, template=_row_text(row)
        )
    except TemplateRenderError as exc:
        logger.error(
            "notification_registration.template_render_failed",
            extra={
                "event_key": intent.event_key,
                "event_type": intent.event_type.value,
                "channel": channel.value,
                "error": str(exc),
            },
        )
        return None


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
        user, and the seed-render errors for a malformed payload —
        programming errors, surfaced not swallowed. A managed row that
        fails to render degrades (seed / historic contract) with an
        ERROR log instead of failing the record.
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
        """Persist one intent: render every channel's message once
        (event-variable namespace), freeze the renders as snapshot
        rows, insert the logical row, insert the per-channel deliveries
        — each insert-on-conflict."""

        channels = intent.channels
        if not channels:
            # An event with no eligible channel persists nothing (the
            # planner's rule, applied uniformly): no inbox row, no
            # delivery rows.
            return
        variables = {
            name: _render_value(payload[name])
            for name in EVENT_VARIABLES[intent.event_type]
            if name in payload
        }
        in_app_row = await _channel_template(
            session, intent.event_type, NotificationChannel.IN_APP
        )
        rendered = _render_in_app_snapshot(intent, in_app_row, variables)
        notification_id = await self._insert_notification(session, intent, rendered)
        # Per-channel frozen products: an SMS/EMAIL channel with an
        # enabled managed row gets its finished text frozen into a
        # snapshot row of its own, and its delivery points at that row
        # — dispatch then sends exactly this text. Channels without a
        # product keep the canonical row (the historic provider
        # contract: snapshot pair + event-type slug).
        snapshot_ids: dict[NotificationChannel, UUID] = {}
        for channel in channels:
            if channel is NotificationChannel.IN_APP:
                continue
            product = await _render_channel_product(session, intent, channel, variables)
            if product is not None:
                snapshot_ids[channel] = await self._insert_notification(
                    session,
                    replace(
                        intent, event_key=_snapshot_event_key(intent.event_key, channel)
                    ),
                    product,
                )
        for channel in channels:
            await session.execute(
                pg_insert(NotificationDelivery)
                .values(
                    notification_id=snapshot_ids.get(channel, notification_id),
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
