# backend/app/modules/notifications/deadline_scheduler.py
"""Claim deadline reminder planning (spec §25.2; plan 07 T3).

Two pure decision points for the ordinary DDL reminders, with no
database, no clock, and no I/O — every instant is an input (the caller
owns "now"; backend-engineering §11):

- `schedule_claim_deadline_notifications` is the claim-time (and
  re-arm-time) PLANNER. Spec §25.2, measured from `now` to the claim's
  snapshotted `deadline_at`:

    >24h left        -> plan both the 24h and the 4h reminder
    exactly 24h left -> the 24h reminder is still plannable (its
                        scheduled_at == now; "once" below)
    4h–24h left      -> plan only the 4h reminder
    exactly 4h left  -> the 4h reminder is still plannable for now
    <4h left         -> plan neither; the claim page shows remaining
                        time instead (not this module's concern)
    <=0              -> unreachable: claims cannot exist past the
                        deadline, and both windows are past anyway.

  "Exactly once" is NOT enforced here: the planner only returns plans,
  and the delivery table's UNIQUE(event_key, user_id, channel) is the
  idempotency boundary that collapses a re-plan, a Celery retry, or
  two racing workers onto one row (spec §25.3). The deterministic
  event keys `claim:{claim_id}:deadline_24h` / `deadline_4h`
  (interfaces.md `<aggregate>:<id>:<suffix>`) are what make that
  possible, which is also the re-arm rule: after a machine validation
  failure rolls the claim back to an actionable status, T5 simply
  re-runs this planner at that instant — reminders whose window has
  passed (scheduled_at < now) drop out (spec §25.2 "尚未错过的未来提
  醒"), and the still-future ones come back under the SAME event key
  so schedule-time dedupe keeps the existing delivery row.

  Channels come from `eligible_channels` (plan 07 T2) with the task's
  `TaskNotificationPolicy`: a disabled notify_24h/notify_4h flag or a
  channel missing from the task's list removes that channel, and
  account-level facts (unverified email, no phone, BANNED /
  PENDING_PHONE) remove it regardless of the task. An event with no
  eligible channel yields no plan — nothing to persist. The planner is
  deliberately status-blind: claim-time callers just created the
  claim, re-arm callers just made it actionable again, and dispatch
  remains the single cancellation point.

- `should_send_reminder` is the dispatch-time suppression predicate
  (spec §25.2 "取消/跳过"): CLAIMED and REVISION_REQUIRED may receive
  an applicable reminder; VALIDATING / UNDER_REVIEW / COMPLETED /
  ABANDONED / EXPIRED suppress it (the student has submitted or the
  claim is gone — an ordinary reminder would be noise). It also
  refuses to send early: `now` must have reached `scheduled`.

Claim statuses live in app/modules/tasks, but this module keeps the
notifications import direction (notifications -> identity only, see
service.py) by carrying the frozen status sets as strings — pinned to
`ClaimStatus` by a unit test so the two cannot drift — and importing
the tasks types for annotations only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from app.modules.identity.models import User
from app.modules.notifications.enums import NotificationChannel, NotificationEventType
from app.modules.notifications.service import (
    ChannelStatus,
    TaskNotificationPolicy,
    eligible_channels,
)

if TYPE_CHECKING:
    # Type-only: keeps the runtime import direction notifications ->
    # identity (see module docstring and service.py).
    from app.modules.tasks.enums import ClaimStatus
    from app.modules.tasks.models import AssignmentClaim

#: Claim statuses that may still receive an ordinary DDL reminder at
#: dispatch time (spec §25.2). REVISION_REQUIRED is included: a claim
#: sent back for revision is actionable again and its clock is still
#: running. Strings, not ClaimStatus, to avoid a runtime tasks import;
#: pinned to the enum by test_deadline_scheduler.
ACTIONABLE_CLAIM_STATUSES = frozenset({"CLAIMED", "REVISION_REQUIRED"})

#: Claim statuses that suppress ordinary DDL reminders at dispatch
#: time (spec §25.2): the student has submitted (VALIDATING /
#: UNDER_REVIEW), finished (COMPLETED), or the claim is gone
#: (ABANDONED / EXPIRED).
SUPPRESSED_CLAIM_STATUSES = frozenset(
    {"VALIDATING", "UNDER_REVIEW", "COMPLETED", "ABANDONED", "EXPIRED"}
)

_ALL_CLAIM_STATUSES = ACTIONABLE_CLAIM_STATUSES | SUPPRESSED_CLAIM_STATUSES


@dataclass(frozen=True)
class _ReminderSpec:
    """One reminder product: which event, which event-key suffix, and
    how far ahead of the deadline it fires."""

    event_type: NotificationEventType
    key_suffix: str
    lead_time: timedelta


# Planning order: 24h first, then 4h, so plan lists are deterministic.
_REMINDER_SPECS: tuple[_ReminderSpec, ...] = (
    _ReminderSpec(
        event_type=NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        key_suffix="deadline_24h",
        lead_time=timedelta(hours=24),
    ),
    _ReminderSpec(
        event_type=NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        key_suffix="deadline_4h",
        lead_time=timedelta(hours=4),
    ),
)


@dataclass(frozen=True)
class DeliveryPlan:
    """What the claim event handler (plan 07 T5) persists for one
    reminder: the dedupe-carrying event key, the event type, the
    recipient, the due instant, and the channels that survived
    routing. Frozen because nothing downstream may nudge a plan after
    the fact — same philosophy as the claim-time snapshots."""

    event_key: str
    event_type: NotificationEventType
    user_id: UUID
    scheduled_at: datetime
    channels: tuple[NotificationChannel, ...]


def _require_aware(value: datetime, name: str) -> None:
    """Enforce the aware-only API; naive datetimes have no UTC instant
    (mirrors tasks.deadlines._require_aware — not imported to keep the
    module dependency direction)."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must be a timezone-aware datetime (UTC instant), "
            f"got naive {value!r}"
        )


def deadline_event_key(claim_id: UUID, event_type: NotificationEventType) -> str:
    """The deterministic dedupe key for one claim's reminder event,
    e.g. `claim:<uuid>:deadline_4h` (interfaces.md event-key
    convention). Exposed so the worker/handler layers construct the
    exact same string this planner put on the plan."""

    for spec in _REMINDER_SPECS:
        if event_type == spec.event_type:
            return f"claim:{claim_id}:{spec.key_suffix}"
    raise ValueError(
        f"{event_type!r} is not a deadline reminder event type; expected one of "
        f"{[spec.event_type.value for spec in _REMINDER_SPECS]}"
    )


def schedule_claim_deadline_notifications(
    claim: AssignmentClaim,
    user: User,
    now: datetime,
    policy: TaskNotificationPolicy,
) -> list[DeliveryPlan]:
    """Plan the ordinary DDL reminders for one claim at `now` (spec
    §25.2; see module docstring for the full decision table).

    Pure: returns the plans for the caller (plan 07 T5) to persist;
    writes nothing. A reminder enters the result only if its window is
    still open (`scheduled_at >= now`, inclusive so an exactly-due
    reminder may schedule "for now once" — once via the event-key
    dedupe) and at least one channel survived `eligible_channels`.
    Plans come back 24h-then-4h, deterministically.
    """

    _require_aware(now, "now")
    _require_aware(claim.deadline_at, "claim.deadline_at")

    plans: list[DeliveryPlan] = []
    for spec in _REMINDER_SPECS:
        scheduled_at = claim.deadline_at - spec.lead_time
        if scheduled_at < now:
            # The reminder's window has passed (claim-time §25.2 band
            # or a re-arm after a missed window): never back-fill.
            continue
        decisions = eligible_channels(user, policy, spec.event_type)
        channels = tuple(
            decision.channel
            for decision in decisions
            if decision.status is ChannelStatus.ELIGIBLE
        )
        if not channels:
            continue
        plans.append(
            DeliveryPlan(
                event_key=f"claim:{claim.id}:{spec.key_suffix}",
                event_type=spec.event_type,
                user_id=claim.user_id,
                scheduled_at=scheduled_at,
                channels=channels,
            )
        )
    return plans


def should_send_reminder(
    claim_status: ClaimStatus | str,
    now: datetime,
    scheduled: datetime,
) -> bool:
    """Dispatch-time suppression check for an ordinary DDL reminder
    (spec §25.2): True only when the claim is still actionable
    (CLAIMED / REVISION_REQUIRED) AND the reminder is due
    (`now >= scheduled`).

    Accepts the raw column string or the enum member — the worker
    reads `AssignmentClaim.status` as VARCHAR. Statuses outside the
    frozen seven raise `ValueError` (they cannot persist; reaching one
    means a caller invented it) rather than silently suppressing,
    which would hide the bug.
    """

    _require_aware(now, "now")
    _require_aware(scheduled, "scheduled")
    if claim_status not in _ALL_CLAIM_STATUSES:
        raise ValueError(
            f"unknown claim status {claim_status!r}; expected one of "
            f"{sorted(_ALL_CLAIM_STATUSES)}"
        )
    if claim_status not in ACTIONABLE_CLAIM_STATUSES:
        return False
    return now >= scheduled
