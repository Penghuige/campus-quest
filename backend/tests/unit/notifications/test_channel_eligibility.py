# backend/tests/unit/notifications/test_channel_eligibility.py
"""Unit tests for notification channel eligibility (spec §25, §25.1,
§5.7; plan 07 T2).

The decision table this module pins:

- EMAIL is verified-only (spec §25.1 default "EMAIL 对 verified email
  on"): a missing or unverified address is SKIPPED with a reason —
  never an error, never a FAILED delivery (plan 07 review focus 3).
- SMS needs a bound phone. Students' phones are verified by
  construction (the OTP flow is the only way a student binds one), so
  presence is the whole check; staff accounts carry no phone and are
  SKIPPED, which is why staff notifications ride EMAIL + IN_APP.
- IN_APP is the always-on fallback for accounts that can still receive
  (spec §25: 站内通知作为兜底). V1 ruling recorded here: ACTIVE and
  SUSPENDED accounts receive in-app (§5.7 keeps a suspended user's
  data readable, and the inbox is that data); BANNED accounts receive
  nothing on any channel, and PENDING_PHONE accounts have not finished
  binding, so both skip everything.
- Task policy (spec §25.1) gates only the two deadline reminder
  events: notify_24h/notify_4h switch the whole reminder off, and the
  per-task channel list switches a single channel off. The six
  critical events (revision required, approval, validation failure,
  redemption results, account security) ignore task policy entirely —
  they always fan out to IN_APP when the account can receive, with
  SMS/EMAIL still gated by the account-level checks above.

Pure function territory: hand-built ORM rows, no database, no clock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.enums import NotificationChannel, NotificationEventType
from app.modules.notifications.service import (
    REASON_ACCOUNT_BANNED,
    REASON_ACCOUNT_PENDING_PHONE,
    REASON_EMAIL_NOT_VERIFIED,
    REASON_NO_EMAIL,
    REASON_NO_PHONE,
    REASON_TASK_CHANNEL_DISABLED,
    REASON_TASK_NOTIFY_4H_DISABLED,
    REASON_TASK_NOTIFY_24H_DISABLED,
    ChannelDecision,
    ChannelStatus,
    TaskNotificationPolicy,
    eligible_channels,
)
from app.modules.tasks.models import Task

_VERIFIED_AT = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)

DEADLINE_EVENTS = (
    NotificationEventType.ASSIGNMENT_DEADLINE_24H,
    NotificationEventType.ASSIGNMENT_DEADLINE_4H,
)
CRITICAL_EVENTS = (
    NotificationEventType.REVISION_REQUIRED,
    NotificationEventType.SUBMISSION_APPROVED,
    NotificationEventType.SUBMISSION_VALIDATION_FAILED,
    NotificationEventType.REWARD_REDEMPTION_APPROVED,
    NotificationEventType.REWARD_REDEMPTION_REJECTED,
    NotificationEventType.ACCOUNT_SECURITY,
)


def _user(**overrides: Any) -> User:
    """An ACTIVE student with a bound phone and a verified email — the
    fully provisioned account every channel wants."""

    fields: dict[str, Any] = {
        "username": "20250010001",
        "password_hash": "not-a-real-hash",
        "nickname": "测试同学",
        "phone_e164": "+8613800138000",
        "email_normalized": "student@example.edu.cn",
        "email_verified_at": _VERIFIED_AT,
        "role": Role.STUDENT,
        "status": UserStatus.ACTIVE,
    }
    fields.update(overrides)
    return User(**fields)


def _decide(
    user: User,
    task_policy: TaskNotificationPolicy | None,
    event_type: NotificationEventType,
) -> dict[NotificationChannel, ChannelDecision]:
    return {
        decision.channel: decision
        for decision in eligible_channels(user, task_policy, event_type)
    }


def _decision(
    user: User,
    task_policy: TaskNotificationPolicy | None,
    event_type: NotificationEventType,
    channel: NotificationChannel,
) -> ChannelDecision:
    return _decide(user, task_policy, event_type)[channel]


def _all_eligible(
    user: User,
    task_policy: TaskNotificationPolicy | None,
    event_type: NotificationEventType,
) -> bool:
    return all(
        decision.status is ChannelStatus.ELIGIBLE
        for decision in _decide(user, task_policy, event_type).values()
    )


# --- baseline: fully provisioned account ---------------------------------------


def test_fully_provisioned_active_student_is_eligible_everywhere() -> None:
    decisions = eligible_channels(
        _user(), TaskNotificationPolicy(), NotificationEventType.ASSIGNMENT_DEADLINE_4H
    )

    # Fixed channel order (enum order) so dispatch iterates deterministically.
    assert [decision.channel for decision in decisions] == [
        NotificationChannel.SMS,
        NotificationChannel.EMAIL,
        NotificationChannel.IN_APP,
    ]
    assert all(decision.status is ChannelStatus.ELIGIBLE for decision in decisions)
    assert all(decision.reason is None for decision in decisions)


# --- email eligibility matrix (plan 07 review focus 3) --------------------------


def test_verified_email_is_eligible() -> None:
    decision = _decision(
        _user(email_normalized="s@example.edu.cn", email_verified_at=_VERIFIED_AT),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationChannel.EMAIL,
    )

    assert decision.status is ChannelStatus.ELIGIBLE
    assert decision.reason is None


def test_missing_email_is_skipped_with_reason_not_failed() -> None:
    decision = _decision(
        _user(email_normalized=None, email_verified_at=None),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationChannel.EMAIL,
    )

    assert decision.status is ChannelStatus.SKIPPED
    assert decision.reason == REASON_NO_EMAIL


def test_unverified_email_is_skipped_with_reason_not_failed() -> None:
    decision = _decision(
        _user(email_normalized="s@example.edu.cn", email_verified_at=None),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
        NotificationChannel.EMAIL,
    )

    assert decision.status is ChannelStatus.SKIPPED
    assert decision.reason == REASON_EMAIL_NOT_VERIFIED


# --- SMS eligibility ------------------------------------------------------------


def test_student_with_phone_is_sms_eligible() -> None:
    decision = _decision(
        _user(),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        NotificationChannel.SMS,
    )

    assert decision.status is ChannelStatus.ELIGIBLE


@pytest.mark.parametrize("event_type", CRITICAL_EVENTS)
def test_staff_without_phone_skips_sms_but_keeps_email_and_in_app(
    event_type: NotificationEventType,
) -> None:
    decisions = _decide(
        _user(
            username="teacher01",
            phone_e164=None,
            role=Role.TEACHER,
        ),
        None,
        event_type,
    )

    assert decisions[NotificationChannel.SMS].status is ChannelStatus.SKIPPED
    assert decisions[NotificationChannel.SMS].reason == REASON_NO_PHONE
    assert decisions[NotificationChannel.EMAIL].status is ChannelStatus.ELIGIBLE
    assert decisions[NotificationChannel.IN_APP].status is ChannelStatus.ELIGIBLE


def test_account_without_phone_skips_sms_even_when_active() -> None:
    """The SMS check is presence-based: an ACTIVE row that somehow lost
    its phone (column-level nothing forbids NULL) still skips rather
    than sending to nothing."""

    decision = _decision(
        _user(phone_e164=None),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
        NotificationChannel.SMS,
    )

    assert decision.status is ChannelStatus.SKIPPED
    assert decision.reason == REASON_NO_PHONE


# --- account status gate (spec §5.7; V1 ruling documented in service) -----------


@pytest.mark.parametrize("event_type", CRITICAL_EVENTS)
@pytest.mark.parametrize("status", [UserStatus.ACTIVE, UserStatus.SUSPENDED])
def test_critical_events_always_reach_in_app_when_account_can_receive(
    status: UserStatus, event_type: NotificationEventType
) -> None:
    """Plan 07: revision required / approval / validation failure /
    redemption results / account security fan out to IN_APP for every
    account that can still receive — no task policy involved (staff
    events like redemption review have no Task row to ask)."""

    decision = _decision(
        _user(status=status), None, event_type, NotificationChannel.IN_APP
    )

    assert decision.status is ChannelStatus.ELIGIBLE


@pytest.mark.parametrize(
    "event_type",
    CRITICAL_EVENTS + DEADLINE_EVENTS[:1],  # one deadline event too
)
def test_banned_account_receives_nothing_on_any_channel(
    event_type: NotificationEventType,
) -> None:
    # Deadline reminders demand a policy even for a banned account; the
    # account gate must fire before anything the policy could say.
    policy = TaskNotificationPolicy() if event_type in DEADLINE_EVENTS else None
    decisions = _decide(_user(status=UserStatus.BANNED), policy, event_type)

    assert decisions
    for channel, decision in decisions.items():
        assert decision.status is ChannelStatus.SKIPPED, channel
        assert decision.reason == REASON_ACCOUNT_BANNED


def test_pending_phone_account_receives_nothing() -> None:
    """A PENDING_PHONE student has not completed binding; nothing is
    dispatched to a half-registered account (V1 ruling)."""

    decisions = _decide(
        _user(
            status=UserStatus.PENDING_PHONE,
            phone_e164=None,
            email_normalized=None,
            email_verified_at=None,
        ),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_4H,
    )

    for channel, decision in decisions.items():
        assert decision.status is ChannelStatus.SKIPPED, channel
        assert decision.reason == REASON_ACCOUNT_PENDING_PHONE


def test_suspended_user_still_gets_all_channels_for_deadline_reminder() -> None:
    assert _all_eligible(
        _user(status=UserStatus.SUSPENDED),
        TaskNotificationPolicy(),
        NotificationEventType.ASSIGNMENT_DEADLINE_24H,
    )


# --- deadline-reminder task policy gating (spec §25.1) --------------------------


def test_notify_24h_off_skips_the_whole_24h_reminder() -> None:
    policy = TaskNotificationPolicy(notify_24h=False)

    decisions = _decide(_user(), policy, NotificationEventType.ASSIGNMENT_DEADLINE_24H)

    for channel, decision in decisions.items():
        assert decision.status is ChannelStatus.SKIPPED, channel
        assert decision.reason == REASON_TASK_NOTIFY_24H_DISABLED


def test_notify_4h_off_does_not_touch_the_24h_reminder() -> None:
    policy = TaskNotificationPolicy(notify_4h=False)

    assert _all_eligible(_user(), policy, NotificationEventType.ASSIGNMENT_DEADLINE_24H)


def test_notify_4h_off_skips_the_whole_4h_reminder() -> None:
    policy = TaskNotificationPolicy(notify_4h=False)

    decisions = _decide(_user(), policy, NotificationEventType.ASSIGNMENT_DEADLINE_4H)

    for channel, decision in decisions.items():
        assert decision.status is ChannelStatus.SKIPPED, channel
        assert decision.reason == REASON_TASK_NOTIFY_4H_DISABLED


def test_notify_24h_off_does_not_touch_the_4h_reminder() -> None:
    policy = TaskNotificationPolicy(notify_24h=False)

    assert _all_eligible(_user(), policy, NotificationEventType.ASSIGNMENT_DEADLINE_4H)


def test_task_channel_toggle_skips_only_that_channel_for_reminders() -> None:
    policy = TaskNotificationPolicy(channels=frozenset({NotificationChannel.IN_APP}))

    decisions = _decide(_user(), policy, NotificationEventType.ASSIGNMENT_DEADLINE_24H)

    assert decisions[NotificationChannel.SMS].status is ChannelStatus.SKIPPED
    assert decisions[NotificationChannel.SMS].reason == REASON_TASK_CHANNEL_DISABLED
    assert decisions[NotificationChannel.EMAIL].status is ChannelStatus.SKIPPED
    assert decisions[NotificationChannel.EMAIL].reason == REASON_TASK_CHANNEL_DISABLED
    assert decisions[NotificationChannel.IN_APP].status is ChannelStatus.ELIGIBLE


def test_task_with_all_channels_off_skips_every_reminder_channel() -> None:
    policy = TaskNotificationPolicy(channels=frozenset())

    decisions = _decide(_user(), policy, NotificationEventType.ASSIGNMENT_DEADLINE_4H)

    assert all(
        decision.status is ChannelStatus.SKIPPED for decision in decisions.values()
    )


def test_deadline_reminder_without_task_policy_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="task policy"):
        eligible_channels(_user(), None, NotificationEventType.ASSIGNMENT_DEADLINE_24H)


@pytest.mark.parametrize("event_type", CRITICAL_EVENTS)
def test_critical_events_ignore_task_policy_completely(
    event_type: NotificationEventType,
) -> None:
    """Even a fully muted policy (both reminder flags off, every channel
    toggled off) cannot silence a critical event: the flags exist to
    tune reminders, not to hide approval or security messages."""

    silenced = TaskNotificationPolicy(
        notify_24h=False, notify_4h=False, channels=frozenset()
    )

    assert _all_eligible(_user(), silenced, event_type)


def test_unknown_event_type_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="event type"):
        eligible_channels(_user(), TaskNotificationPolicy(), "NOT_AN_EVENT")


# --- TaskNotificationPolicy.from_task ------------------------------------------


def _task(**overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "notify_24h": True,
        "notify_4h": True,
        "notification_channels": ["SMS", "EMAIL", "IN_APP"],
    }
    fields.update(overrides)
    return Task(**fields)


def test_from_task_reads_the_snapshot_columns() -> None:
    policy = TaskNotificationPolicy.from_task(
        _task(notify_24h=False, notification_channels=["IN_APP"])
    )

    assert policy.notify_24h is False
    assert policy.notify_4h is True
    assert policy.channels == frozenset({"IN_APP"})


def test_from_task_applies_column_defaults_to_unflushed_rows() -> None:
    """A Task built in memory (unit tests, pre-flush) carries None in
    server-default columns; from_task must fall back to the schema
    defaults (both reminders on, all three channels), mirroring how
    deadlines.py treats grace_period_minutes."""

    policy = TaskNotificationPolicy.from_task(Task())

    assert policy.notify_24h is True
    assert policy.notify_4h is True
    assert policy.channels == frozenset(NotificationChannel)
