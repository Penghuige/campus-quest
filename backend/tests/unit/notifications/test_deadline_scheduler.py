# backend/tests/unit/notifications/test_deadline_scheduler.py
"""Unit tests for claim deadline reminder planning (spec §25.2; plan 07
T3).

The decision table this module pins:

Claim-time planning (§25.2, measured from `now` to the claim's
snapshotted `deadline_at`):

- >24h left: plan BOTH the 24h and the 4h reminder.
- exactly 24h left: the 24h reminder is still plannable — its
  scheduled_at equals `now` ("may schedule for now once" — the "once"
  is the UNIQUE(event_key, user_id, channel) dedupe at persistence
  time, not a planner concern).
- 4h–24h left: plan only the 4h reminder; the 24h window has passed
  and is never back-filled.
- exactly 4h left: the 4h reminder is plannable for now.
- <4h left: plan neither past reminder (the claim page shows the
  remaining time instead — not this module's concern).

Planning is pure: hand-built claim/user rows, a FrozenClock, and a
TaskNotificationPolicy. No database, no persistence — the returned
DeliveryPlan list is what the claim event handler (plan 07 T5)
persists, and the deterministic event keys
(`claim:{claim_id}:deadline_24h` / `...:deadline_4h`) are what make
re-planning idempotent under the delivery table's UNIQUE constraint.

Dispatch-time suppression (§25.2 "取消/跳过"): CLAIMED and
REVISION_REQUIRED claims may still receive an outstanding reminder;
VALIDATING / UNDER_REVIEW / COMPLETED / ABANDONED / EXPIRED suppress
it. Not-yet-due reminders never send early.

Re-arm (§25.2 "机器校验失败…重新判断尚未错过的未来提醒"): after a
validation failure rolls the claim back to an actionable status,
re-running the planner at that instant returns only reminders whose
scheduled_at is still in the future — the missed 24h reminder is not
resurrected, and the still-future 4h reminder comes back under the
SAME event key, so schedule-time dedupe collapses it onto the delivery
row that already exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.core.clock import FrozenClock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.notifications.deadline_scheduler import (
    ACTIONABLE_CLAIM_STATUSES,
    SUPPRESSED_CLAIM_STATUSES,
    deadline_event_key,
    schedule_claim_deadline_notifications,
    should_send_reminder,
)
from app.modules.notifications.enums import NotificationChannel, NotificationEventType
from app.modules.notifications.service import TaskNotificationPolicy
from app.modules.tasks.enums import ClaimStatus
from app.modules.tasks.models import AssignmentClaim

_VERIFIED_AT = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
# One fixed deadline; every case moves the FrozenClock around it.
_DEADLINE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

_CLAIM_ID = UUID("00000000-0000-4000-8000-000000000001")
_USER_ID = UUID("00000000-0000-4000-8000-000000000002")
_TASK_ID = UUID("00000000-0000-4000-8000-000000000003")
_ASSIGNMENT_ID = UUID("00000000-0000-4000-8000-000000000004")

_KEY_24H = f"claim:{_CLAIM_ID}:deadline_24h"
_KEY_4H = f"claim:{_CLAIM_ID}:deadline_4h"

_AT_DUE = FrozenClock(_DEADLINE - timedelta(hours=2))  # after both sends


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


def _claim(**overrides: Any) -> AssignmentClaim:
    """A freshly CLAIMED assignment 48h before the shared deadline."""

    fields: dict[str, Any] = {
        "id": _CLAIM_ID,
        "assignment_id": _ASSIGNMENT_ID,
        "task_id": _TASK_ID,
        "user_id": _USER_ID,
        "status": ClaimStatus.CLAIMED,
        "claimed_at": _DEADLINE - timedelta(hours=48),
        "deadline_at": _DEADLINE,
        "grace_deadline_at": _DEADLINE + timedelta(hours=24),
        "reward_policy_snapshot": {},
        "base_reward_points_snapshot": 10,
        "submission_schema_version": 1,
        "reward_lock_status": "NONE",
    }
    fields.update(overrides)
    return AssignmentClaim(**fields)


def _clock(hours_before_deadline: float) -> FrozenClock:
    return FrozenClock(_DEADLINE - timedelta(hours=hours_before_deadline))


def _plan(
    claim: AssignmentClaim | None = None,
    hours_before_deadline: float = 30,
    policy: TaskNotificationPolicy | None = None,
    user: User | None = None,
) -> list[Any]:
    return schedule_claim_deadline_notifications(
        claim if claim is not None else _claim(),
        user if user is not None else _user(),
        _clock(hours_before_deadline).now(),
        policy if policy is not None else TaskNotificationPolicy(),
    )


def _by_key(plans: list[Any]) -> dict[str, Any]:
    return {plan.event_key: plan for plan in plans}


# --- §25.2 claim-time planning: the five FrozenClock cases -------------------


def test_30h_left_plans_both_reminders() -> None:
    plans = _by_key(_plan(hours_before_deadline=30))

    assert sorted(plans) == sorted([_KEY_24H, _KEY_4H])
    assert plans[_KEY_24H].event_type is NotificationEventType.ASSIGNMENT_DEADLINE_24H
    assert plans[_KEY_4H].event_type is NotificationEventType.ASSIGNMENT_DEADLINE_4H
    # scheduled_at is anchored to the DEADLINE, not to claim time.
    assert plans[_KEY_24H].scheduled_at == _DEADLINE - timedelta(hours=24)
    assert plans[_KEY_4H].scheduled_at == _DEADLINE - timedelta(hours=4)
    # Fully provisioned account + default policy: every channel.
    assert plans[_KEY_24H].channels == (
        NotificationChannel.SMS,
        NotificationChannel.EMAIL,
        NotificationChannel.IN_APP,
    )
    assert plans[_KEY_4H].channels == (
        NotificationChannel.SMS,
        NotificationChannel.EMAIL,
        NotificationChannel.IN_APP,
    )
    assert plans[_KEY_24H].user_id == _USER_ID
    assert plans[_KEY_4H].user_id == _USER_ID


def test_30h_left_plans_are_ordered_24h_then_4h() -> None:
    plans = _plan(hours_before_deadline=30)

    assert [plan.event_key for plan in plans] == [_KEY_24H, _KEY_4H]


def test_10h_left_plans_only_4h_reminder() -> None:
    plans = _by_key(_plan(hours_before_deadline=10))

    assert list(plans) == [_KEY_4H]


def test_3h_left_plans_neither_past_reminder() -> None:
    assert _plan(hours_before_deadline=3) == []


def test_exactly_24h_left_schedules_24h_reminder_for_now() -> None:
    clock = _clock(hours_before_deadline=24)
    plans = _by_key(
        schedule_claim_deadline_notifications(
            _claim(), _user(), clock.now(), TaskNotificationPolicy()
        )
    )

    # The boundary is inclusive: deadline - 24h == now is still
    # plannable ("may schedule for now once" — once via the event-key
    # dedupe, not the planner).
    assert plans[_KEY_24H].scheduled_at == clock.now()
    assert _KEY_4H in plans


def test_exactly_4h_left_schedules_4h_reminder_for_now() -> None:
    clock = _clock(hours_before_deadline=4)
    plans = _by_key(
        schedule_claim_deadline_notifications(
            _claim(), _user(), clock.now(), TaskNotificationPolicy()
        )
    )

    assert list(plans) == [_KEY_4H]
    assert plans[_KEY_4H].scheduled_at == clock.now()


# --- event keys and channel routing ------------------------------------------


def test_event_keys_embed_the_claim_id() -> None:
    other = _claim(id=UUID("00000000-0000-4000-8000-000000000009"))
    plans = _by_key(_plan(claim=other, hours_before_deadline=30))

    assert set(plans) == {
        f"claim:{other.id}:deadline_24h",
        f"claim:{other.id}:deadline_4h",
    }
    assert (
        deadline_event_key(other.id, NotificationEventType.ASSIGNMENT_DEADLINE_4H)
        == f"claim:{other.id}:deadline_4h"
    )


def test_task_notify_24h_disabled_skips_the_24h_event_entirely() -> None:
    plans = _by_key(
        _plan(hours_before_deadline=30, policy=TaskNotificationPolicy(notify_24h=False))
    )

    assert list(plans) == [_KEY_4H]


def test_task_channel_list_restricts_planned_channels() -> None:
    plans = _by_key(
        _plan(
            hours_before_deadline=30,
            policy=TaskNotificationPolicy(
                channels=frozenset({NotificationChannel.IN_APP})
            ),
        )
    )

    assert plans[_KEY_24H].channels == (NotificationChannel.IN_APP,)
    assert plans[_KEY_4H].channels == (NotificationChannel.IN_APP,)


def test_unverified_email_is_excluded_from_planned_channels() -> None:
    plans = _by_key(_plan(hours_before_deadline=30, user=_user(email_verified_at=None)))

    assert plans[_KEY_4H].channels == (
        NotificationChannel.SMS,
        NotificationChannel.IN_APP,
    )


def test_every_channel_skipped_yields_no_plan() -> None:
    # Both reminder flags off: nothing to deliver on any channel.
    plans = _plan(
        hours_before_deadline=30,
        policy=TaskNotificationPolicy(notify_24h=False, notify_4h=False),
    )

    assert plans == []


# --- aware-only API -----------------------------------------------------------


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_claim_deadline_notifications(
            _claim(), _user(), datetime(2026, 9, 30, 6, 0), TaskNotificationPolicy()
        )


def test_naive_claim_deadline_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_claim_deadline_notifications(
            _claim(deadline_at=datetime(2026, 10, 1, 12, 0)),
            _user(),
            _clock(30).now(),
            TaskNotificationPolicy(),
        )


# --- §25.2 dispatch-time suppression ------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ClaimStatus.CLAIMED, True),
        (ClaimStatus.REVISION_REQUIRED, True),
        (ClaimStatus.VALIDATING, False),
        (ClaimStatus.UNDER_REVIEW, False),
        (ClaimStatus.COMPLETED, False),
        (ClaimStatus.ABANDONED, False),
        (ClaimStatus.EXPIRED, False),
    ],
)
def test_should_send_reminder_status_matrix(
    status: ClaimStatus, expected: bool
) -> None:
    scheduled = _DEADLINE - timedelta(hours=4)

    assert should_send_reminder(status, _AT_DUE.now(), scheduled) is expected


def test_should_send_reminder_accepts_raw_status_strings() -> None:
    # AssignmentClaim.status is a VARCHAR column; the predicate must
    # decide on the stored string exactly like on the enum member.
    scheduled = _DEADLINE - timedelta(hours=4)

    assert should_send_reminder("CLAIMED", _AT_DUE.now(), scheduled) is True
    assert should_send_reminder("VALIDATING", _AT_DUE.now(), scheduled) is False


def test_should_send_reminder_suppresses_not_yet_due() -> None:
    scheduled = _DEADLINE - timedelta(hours=4)
    early = FrozenClock(_DEADLINE - timedelta(hours=6))

    assert should_send_reminder(ClaimStatus.CLAIMED, early.now(), scheduled) is False


def test_should_send_reminder_rejects_unknown_status() -> None:
    scheduled = _DEADLINE - timedelta(hours=4)

    with pytest.raises(ValueError, match="unknown claim status"):
        should_send_reminder("PAUSED", _AT_DUE.now(), scheduled)


def test_should_send_reminder_rejects_naive_datetimes() -> None:
    scheduled = _DEADLINE - timedelta(hours=4)

    with pytest.raises(ValueError, match="timezone-aware"):
        should_send_reminder(
            ClaimStatus.CLAIMED, datetime(2026, 10, 1, 10, 0), scheduled
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        should_send_reminder(
            ClaimStatus.CLAIMED, _AT_DUE.now(), datetime(2026, 10, 1, 8, 0)
        )


def test_status_sets_partition_the_claim_status_enum() -> None:
    # The module owns string sets (not the tasks enum) to keep the
    # notifications import direction; this pins them to ClaimStatus so
    # the two cannot drift apart.
    all_statuses = frozenset(status.value for status in ClaimStatus)

    assert all_statuses == ACTIONABLE_CLAIM_STATUSES | SUPPRESSED_CLAIM_STATUSES
    assert not (ACTIONABLE_CLAIM_STATUSES & SUPPRESSED_CLAIM_STATUSES)


# --- §25.2 re-arm after machine validation failure -----------------------------


def test_validation_failure_rearm_replans_only_the_future_reminder() -> None:
    # Claim 48h before the deadline; 30h left at planning time.
    initial = _by_key(_plan(hours_before_deadline=30))
    assert set(initial) == {_KEY_24H, _KEY_4H}

    # Machine validation fails 10h before the deadline; the claim rolls
    # back to an actionable status and T5 re-runs the planner.
    rearm = _by_key(
        _plan(
            claim=_claim(status=ClaimStatus.REVISION_REQUIRED),
            hours_before_deadline=10,
        )
    )

    # The missed 24h reminder is NOT resurrected; the still-future 4h
    # reminder is re-planned under the SAME event key, so the delivery
    # table's UNIQUE(event_key, user_id, channel) collapses it onto the
    # row that already exists instead of duplicating it.
    assert list(rearm) == [_KEY_4H]
    assert rearm[_KEY_4H].scheduled_at == initial[_KEY_4H].scheduled_at
    assert rearm[_KEY_4H].channels == initial[_KEY_4H].channels

    # At its due instant the re-armed reminder passes dispatch-time
    # suppression (REVISION_REQUIRED is actionable).
    due = FrozenClock(rearm[_KEY_4H].scheduled_at)
    assert (
        should_send_reminder(
            ClaimStatus.REVISION_REQUIRED, due.now(), rearm[_KEY_4H].scheduled_at
        )
        is True
    )


def test_rearm_after_both_windows_missed_plans_nothing() -> None:
    # Validation fails inside the last 4h: both reminders are past and
    # neither is back-filled (the claim page shows remaining time).
    plans = _plan(
        claim=_claim(status=ClaimStatus.REVISION_REQUIRED),
        hours_before_deadline=3,
    )

    assert plans == []


def test_suppressed_claim_does_not_receive_due_reminder_at_dispatch() -> None:
    # The claim entered VALIDATING exactly when the 4h reminder fell
    # due: dispatch-time suppression must skip it (§25.2 取消/跳过).
    scheduled = _DEADLINE - timedelta(hours=4)

    assert (
        should_send_reminder(ClaimStatus.VALIDATING, _AT_DUE.now(), scheduled) is False
    )
