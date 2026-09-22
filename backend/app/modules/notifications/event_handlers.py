# backend/app/modules/notifications/event_handlers.py
"""Event-type routing for recorded business events (spec §25; plan 07
task 5).

This module is the pure decision layer between the persistence port
(`app.modules.notifications.port`) and the T2/T3 primitives: it maps an
incoming (event_type, payload) pair onto `RecordIntent`s — "persist one
logical notification for this key, type, recipient, due instant, and
channel set". No database, no clock, no I/O; the port owns the rows.

The mapping (spec §25 event list + delivery model):

- ``CLAIM_CREATED`` (trigger, not a NotificationEventType member — it
  never persists as a notification of its own) -> the T3 planner
  `schedule_claim_deadline_notifications` turns the claim facts into
  per-reminder intents: ``claim:{id}:deadline_24h`` /
  ``claim:{id}:deadline_4h``, each due at its deadline-minus-lead-time
  instant, channels already filtered by T2 eligibility. The same path
  serves the spec §25.2 RE-ARM: when a machine validation failure
  rolls a claim back to an actionable status, the caller re-records
  with `now` = the rollback instant — the planner drops windows that
  have passed and returns only still-future reminders under the SAME
  event keys, so the delivery-table dedupe keeps existing rows.
- The six critical events (`REVISION_REQUIRED`, `SUBMISSION_APPROVED`,
  `SUBMISSION_VALIDATION_FAILED`, both redemption results,
  `ACCOUNT_SECURITY`) -> one fan-out intent due NOW: IN_APP always
  (when the account can receive), SMS/EMAIL account-gated (T2
  `eligible_channels`; task policy is ignored for critical events).
- The two deadline reminder types may also be recorded DIRECTLY (an
  already-due reminder, e.g. an admin re-fire): the same fan-out path
  with the T2 deadline-policy contract (task_policy required).
- `SUBMISSION_VALIDATION_FAILED` additionally re-arms deadline
  planning when its payload carries the claim facts (below): the
  submissions module emits the validation-failure notification AND
  restores future reminders in one transaction.

Payload conventions (the port passes the caller's mapping through):

- Render variables: payload keys matching the event type's frozen
  whitelist (`templates.EVENT_VARIABLES`) — values may be str, datetime
  (ISO-8601 at the port), or int/Decimal (exact text; §31.1 — floats
  have no defined points text).
- Deadline (re)planning: the pair of keys ``claim_id`` (UUID or its
  string form) and ``deadline_at`` (timezone-aware datetime) selects
  the planner path. Either key absent -> no planning, fan-out only.
  ``task_policy`` (the Task row or a `TaskNotificationPolicy`) is
  REQUIRED whenever planning runs.

The producers are wired (MERGE_CARRIES item 2 closed at the PR #2
hardening merge): the claim path (``tasks.claim_service`` → CLAIM_CREATED),
the submissions validation service (SUBMISSION_VALIDATION_FAILED with
the re-arm pair), the review flow (REVISION_REQUIRED /
SUBMISSION_APPROVED), points redemption (both result events), and the
identity security producer (ACCOUNT_SECURITY on TOTP enable) — each
through the constructor-injected port at its composition root.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from app.modules.identity.models import User
from app.modules.notifications.deadline_scheduler import (
    schedule_claim_deadline_notifications,
)
from app.modules.notifications.enums import NotificationChannel, NotificationEventType
from app.modules.notifications.service import (
    ChannelStatus,
    TaskNotificationPolicy,
    eligible_channels,
)

if TYPE_CHECKING:
    # Annotation-only: keeps the runtime import direction notifications
    # -> identity (deadline_scheduler does the same for its claim
    # parameter, which is duck-typed the same way).
    from app.modules.tasks.models import AssignmentClaim

#: The claim-creation planning trigger. NOT a NotificationEventType
#: member: interfaces.md freezes the persisted event set, and this
#: trigger never becomes a Notification row — only the planner's
#: deadline_* intents do. Spelled here as the canonical constant;
#: tasks.claim_service passes the same string (it cannot import this
#: module).
CLAIM_CREATED = "CLAIM_CREATED"

#: Payload key carrying the claim id (UUID or string form).
PAYLOAD_CLAIM_ID = "claim_id"

#: Payload key carrying the claim's snapshotted deadline (aware
#: datetime). Doubles as the deadline events' render variable.
PAYLOAD_DEADLINE_AT = "deadline_at"


@dataclass(frozen=True)
class RecordIntent:
    """What the port persists for one planned notification: the
    dedupe-carrying event key, the persisted event type, the recipient,
    the due instant, and the registration-time channel set (dispatch
    re-checks eligibility; models.py UNIQUE(event_key, user_id,
    channel) is the boundary that collapses duplicates)."""

    event_key: str
    event_type: NotificationEventType
    user_id: UUID
    scheduled_at: datetime
    channels: tuple[NotificationChannel, ...]


@dataclass(frozen=True)
class ClaimDeadlineSnapshot:
    """The three claim facts the T3 planner reads (id, user_id,
    deadline_at), reconstructed from the event payload.

    A structural stand-in for `tasks.models.AssignmentClaim` so this
    module keeps the notifications import direction (notifications ->
    identity only; the tasks types stay TYPE_CHECKING-only there).
    """

    id: UUID
    user_id: UUID
    deadline_at: datetime


def normalize_event_type(
    event_type: NotificationEventType | str,
) -> NotificationEventType | str:
    """Accept an enum member, its canonical string value, or the
    CLAIM_CREATED trigger; raise ValueError for anything else.

    Strings keep the port callable from modules that must not import
    the notifications enums (tasks.claim_service passes "CLAIM_CREATED"
    as a plain string).
    """

    if isinstance(event_type, NotificationEventType):
        return event_type
    if event_type == CLAIM_CREATED:
        return CLAIM_CREATED
    if event_type in frozenset(member.value for member in NotificationEventType):
        return NotificationEventType(event_type)
    raise ValueError(
        f"unknown event type {event_type!r}; expected one of "
        f"{sorted(member.value for member in NotificationEventType)} "
        f"or the {CLAIM_CREATED} trigger"
    )


def build_record_intents(
    *,
    user: User,
    event_key: str,
    event_type: NotificationEventType | str,
    payload: Mapping[str, Any],
    now: datetime,
    task_policy: TaskNotificationPolicy | None,
) -> list[RecordIntent]:
    """Map one recorded event onto the intents the port persists.

    `now` is the registration instant (the port's clock): critical
    events are due immediately; deadline (re)planning measures window
    membership against it (spec §25.2). `task_policy` participates
    only in deadline routing (T2 contract: required for the reminder
    events, ignored for critical ones).
    """

    normalized = normalize_event_type(event_type)
    if normalized == CLAIM_CREATED:
        # The trigger is pure planning input: no fan-out of its own.
        return _deadline_planning_intents(
            user=user, payload=payload, now=now, task_policy=task_policy
        )
    assert isinstance(normalized, NotificationEventType)  # normalize guarantees it
    intents = [
        _fanout_intent(
            user=user,
            event_key=event_key,
            event_type=normalized,
            now=now,
            task_policy=task_policy,
        )
    ]
    if normalized is NotificationEventType.SUBMISSION_VALIDATION_FAILED:
        # Spec §25.2: a validation failure that returned the claim to
        # an actionable status re-judges the not-yet-missed reminders.
        intents.extend(
            _deadline_planning_intents(
                user=user, payload=payload, now=now, task_policy=task_policy
            )
        )
    return intents


def _fanout_intent(
    *,
    user: User,
    event_key: str,
    event_type: NotificationEventType,
    now: datetime,
    task_policy: TaskNotificationPolicy | None,
) -> RecordIntent:
    """One due-now intent whose channel set is the T2 registration-time
    decision (critical events: IN_APP whenever the account can receive,
    SMS/EMAIL account-gated; reminder events: task policy applied)."""

    decisions = eligible_channels(user, task_policy, event_type)
    channels = tuple(
        decision.channel
        for decision in decisions
        if decision.status is ChannelStatus.ELIGIBLE
    )
    return RecordIntent(
        event_key=event_key,
        event_type=event_type,
        user_id=user.id,
        scheduled_at=now,
        channels=channels,
    )


def _deadline_planning_intents(
    *,
    user: User,
    payload: Mapping[str, Any],
    now: datetime,
    task_policy: TaskNotificationPolicy | None,
) -> list[RecordIntent]:
    """Run the T3 planner over the payload's claim facts (spec §25.2
    claim-time table and re-arm rule) and translate its DeliveryPlans
    into port intents. No claim facts in the payload -> no planning."""

    snapshot = _claim_deadline_snapshot(user_id=user.id, payload=payload)
    if snapshot is None:
        return []
    if task_policy is None:
        raise ValueError(
            f"deadline (re)planning for claim {snapshot.id} requires a "
            "task policy; pass the Task row or "
            "TaskNotificationPolicy.from_task(task)"
        )
    # The cast is honest: the planner's runtime contract is exactly the
    # three attributes ClaimDeadlineSnapshot carries (deadline_scheduler
    # documents the same duck-typing from its side of the seam).
    plans = schedule_claim_deadline_notifications(
        cast("AssignmentClaim", snapshot), user, now, task_policy
    )
    return [
        RecordIntent(
            event_key=plan.event_key,
            event_type=plan.event_type,
            user_id=plan.user_id,
            scheduled_at=plan.scheduled_at,
            channels=plan.channels,
        )
        for plan in plans
    ]


def _claim_deadline_snapshot(
    *, user_id: UUID, payload: Mapping[str, Any]
) -> ClaimDeadlineSnapshot | None:
    """The re-plan payload convention (see module docstring): both
    ``claim_id`` and ``deadline_at`` present -> snapshot; otherwise
    None (nothing to plan). The recipient is always the event's
    user_id — deadline reminders target the claimant."""

    raw_claim_id = payload.get(PAYLOAD_CLAIM_ID)
    deadline_at = payload.get(PAYLOAD_DEADLINE_AT)
    if raw_claim_id is None or deadline_at is None:
        return None
    claim_id = (
        raw_claim_id if isinstance(raw_claim_id, UUID) else UUID(str(raw_claim_id))
    )
    if not isinstance(deadline_at, datetime):
        raise ValueError(
            f"payload {PAYLOAD_DEADLINE_AT!r} must be a timezone-aware datetime "
            f"for deadline (re)planning, got {type(deadline_at).__name__}"
        )
    return ClaimDeadlineSnapshot(id=claim_id, user_id=user_id, deadline_at=deadline_at)
