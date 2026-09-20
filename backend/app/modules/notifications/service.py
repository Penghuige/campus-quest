# backend/app/modules/notifications/service.py
"""Channel eligibility for notification events (spec §25, §25.1, §5.7;
plan 07 T2).

`eligible_channels` is the routing decision the dispatch service (plan
07 T4/T5) consults before creating per-channel NotificationDelivery
rows. It is a pure function over the user row, the task's notification
policy, and the event type — no database, no clock — so every rule
below is pin-testable:

- EMAIL is verified-only (spec §25.1 default "EMAIL 对 verified email
  on"): `email_normalized` missing, or present but `email_verified_at`
  NULL, means the channel decision is SKIPPED with a reason — the
  plan's review focus 3 explicitly wants skip, not failure.
- SMS requires a bound phone. A student's phone is verified by
  construction (the OTP flow is the only binding path, spec §5.4), so
  presence is the entire check; staff accounts carry no phone and are
  SKIPPED with `no_phone` — staff notifications ride EMAIL + IN_APP.
- IN_APP is the always-on fallback (spec §25: 轻量站内通知作为兜底).
  V1 ruling, recorded here because the spec does not spell it out:
  accounts in ACTIVE or SUSPENDED can receive (spec §5.7 keeps a
  suspended user's points and audit data; the inbox is that retained
  data, still readable), while BANNED accounts receive nothing on any
  channel and PENDING_PHONE accounts have not finished registering, so
  both skip everything. The status gate is applied to all three
  channels uniformly — one "can this account receive at all" answer,
  not a per-channel patch.
- Task policy (spec §25.1: notify_24h / notify_4h / per-channel
  toggles) gates ONLY the two deadline reminder events: a disabled
  reminder flag skips the whole event on every channel; a channel
  missing from `notification_channels` skips just that channel. The
  six critical events (REVISION_REQUIRED, SUBMISSION_APPROVED,
  SUBMISSION_VALIDATION_FAILED, both redemption results,
  ACCOUNT_SECURITY) ignore task policy entirely: they always fan out
  to IN_APP when the account can receive, with SMS/EMAIL still subject
  to the account-level checks. Redemption and account-security events
  have no Task row to ask, and plan 07 fixes "critical events always
  create IN_APP"; a Task's channel toggles exist to tune its reminder
  noise, not to hide approval or security messages.
- SKIPPED carries a stable machine-readable reason token (constants
  below) for logs and admin observability; ELIGIBLE carries None.
  Decisions come back in enum order (SMS, EMAIL, IN_APP) so dispatch
  iterates deterministically.

`TaskNotificationPolicy.from_task` snapshots a Task row's columns at
decision time; None columns on never-flushed rows fall back to the
schema defaults exactly like deadlines.py treats grace_period_minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from app.modules.identity.enums import UserStatus
from app.modules.identity.models import User
from app.modules.notifications.enums import NotificationChannel, NotificationEventType

if TYPE_CHECKING:
    # Type-only: the notifications module must not import tasks at
    # runtime (dependency direction stays tasks -> nothing, identity ->
    # nothing, notifications -> identity only).
    from app.modules.tasks.models import Task

# Stable reason tokens for SKIPPED decisions (logs/admin queries grep
# these; they are not §29 API envelope codes).
REASON_ACCOUNT_BANNED = "account_banned"
REASON_ACCOUNT_PENDING_PHONE = "account_pending_phone"
REASON_TASK_NOTIFY_24H_DISABLED = "task_notify_24h_disabled"
REASON_TASK_NOTIFY_4H_DISABLED = "task_notify_4h_disabled"
REASON_TASK_CHANNEL_DISABLED = "task_channel_disabled"
REASON_NO_PHONE = "no_phone"
REASON_NO_EMAIL = "no_email"
REASON_EMAIL_NOT_VERIFIED = "email_not_verified"

#: Statuses whose accounts can still receive notifications (V1 ruling,
#: see module docstring).
_IN_APP_ELIGIBLE_STATUSES = frozenset({UserStatus.ACTIVE, UserStatus.SUSPENDED})

#: The two event types a Task's notification policy may gate (spec
#: §25.1/§25.2). Every other event type is critical and ignores it.
DEADLINE_REMINDER_EVENTS = frozenset(
    {
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
    }
)

_KNOWN_EVENT_TYPES = frozenset(NotificationEventType)
_KNOWN_CHANNELS = frozenset(NotificationChannel)

# Schema defaults for Task notification columns (mirrors
# tasks.commands.DEFAULT_NOTIFICATION_CHANNELS and the migration's
# server_defaults), used when from_task reads a never-flushed row.
_DEFAULT_NOTIFY_24H = True
_DEFAULT_NOTIFY_4H = True
_DEFAULT_CHANNELS = frozenset(NotificationChannel)


class ChannelStatus(StrEnum):
    """Per-channel outcome of the routing decision."""

    ELIGIBLE = "ELIGIBLE"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ChannelDecision:
    """One channel's decision for one (user, event) pair. SKIPPED
    always carries a reason token; ELIGIBLE never does."""

    channel: NotificationChannel
    status: ChannelStatus
    reason: str | None = None


@dataclass(frozen=True)
class TaskNotificationPolicy:
    """The Task-side inputs of the routing decision (spec §25.1),
    snapshotted from the row via `from_task`."""

    notify_24h: bool = _DEFAULT_NOTIFY_24H
    notify_4h: bool = _DEFAULT_NOTIFY_4H
    channels: frozenset[str] = _DEFAULT_CHANNELS

    @classmethod
    def from_task(cls, task: Task) -> TaskNotificationPolicy:
        """Snapshot a Task row. None columns (never-flushed in-memory
        rows in unit tests) fall back to the schema defaults; persisted
        rows are NOT NULL so the fallback never fires in production.
        Channel codes are taken as stored — the database CHECK pins
        them to the closed channel set."""

        return cls(
            notify_24h=(
                _DEFAULT_NOTIFY_24H if task.notify_24h is None else task.notify_24h
            ),
            notify_4h=(
                _DEFAULT_NOTIFY_4H if task.notify_4h is None else task.notify_4h
            ),
            channels=(
                _DEFAULT_CHANNELS
                if task.notification_channels is None
                else frozenset(task.notification_channels)
            ),
        )


def eligible_channels(
    user: User,
    task_policy: TaskNotificationPolicy | None,
    event_type: NotificationEventType,
) -> list[ChannelDecision]:
    """Decide, per channel, whether `user` may receive `event_type`.

    `task_policy` participates only for the deadline reminder events
    (and is REQUIRED for them — a reminder without its Task's policy is
    a programming error); critical events accept None and ignore any
    policy passed. Returns one decision per channel in enum order.
    """

    if event_type not in _KNOWN_EVENT_TYPES:
        raise ValueError(
            f"unknown notification event type {event_type!r}; expected one of "
            f"{sorted(event.value for event in NotificationEventType)}"
        )
    is_deadline_reminder = event_type in DEADLINE_REMINDER_EVENTS
    if is_deadline_reminder and task_policy is None:
        raise ValueError(
            f"deadline reminder event {event_type} requires a task policy; "
            "pass TaskNotificationPolicy.from_task(task)"
        )

    decisions: list[ChannelDecision] = []
    for channel in NotificationChannel:
        reason = _skip_reason(user, task_policy, event_type, channel)
        decisions.append(
            ChannelDecision(
                channel=channel,
                status=(
                    ChannelStatus.ELIGIBLE if reason is None else ChannelStatus.SKIPPED
                ),
                reason=reason,
            )
        )
    return decisions


def _skip_reason(
    user: User,
    task_policy: TaskNotificationPolicy | None,
    event_type: NotificationEventType,
    channel: NotificationChannel,
) -> str | None:
    """Why this channel is skipped, or None when eligible. Check order
    is the decision chain: account-wide gate, then (reminders only) the
    task's event-level flags and channel toggles, then the channel's
    own account requirements."""

    status = user.status
    if status == UserStatus.BANNED:
        return REASON_ACCOUNT_BANNED
    if status == UserStatus.PENDING_PHONE:
        return REASON_ACCOUNT_PENDING_PHONE

    if event_type in DEADLINE_REMINDER_EVENTS:
        assert task_policy is not None  # guarded by eligible_channels
        if (
            event_type == NotificationEventType.ASSIGNMENT_DEADLINE_24H
            and not task_policy.notify_24h
        ):
            return REASON_TASK_NOTIFY_24H_DISABLED
        if (
            event_type == NotificationEventType.ASSIGNMENT_DEADLINE_4H
            and not task_policy.notify_4h
        ):
            return REASON_TASK_NOTIFY_4H_DISABLED
        if channel not in task_policy.channels:
            return REASON_TASK_CHANNEL_DISABLED

    if channel == NotificationChannel.SMS:
        if not user.phone_e164:
            return REASON_NO_PHONE
    elif channel == NotificationChannel.EMAIL:
        if not user.email_normalized:
            return REASON_NO_EMAIL
        if not user.email_verified_at:
            return REASON_EMAIL_NOT_VERIFIED
    # IN_APP has no per-channel account requirement beyond the status
    # gate above: it is the fallback channel (spec §25).
    return None
