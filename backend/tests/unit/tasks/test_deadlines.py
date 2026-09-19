# backend/tests/unit/tasks/test_deadlines.py
"""Unit tests for the deadline and reward-tier calculator (spec §9, §9.3,
§11.5, §31.1; plan 03 T5).

Spec §9.3 makes the boundary table itself a MUST:

    这些边界 MUST 有单元测试，避免前后端各自使用不同的 < / <=。

So the core of this module is a 12-instant ladder around one deadline:
every tier edge (deadline, +4h, +12h, grace) is probed at -1ms / exactly
/ +1ms. Expected values straight from §9.3:

    submitted_at <= deadline_at                    -> Decimal("1")
    deadline_at < submitted_at < deadline_at + 4h   -> Decimal("0.8")
    deadline_at + 4h <= submitted_at < +12h         -> Decimal("0.5")
    deadline_at + 12h <= submitted_at < grace       -> Decimal("0.2")
    submitted_at >= grace_deadline_at               -> WindowClosedError

Exactly ON each edge: deadline = 100%, +4h = 50%, +12h = 20%, grace =
closed (spec §9.3 "因此" list).

Floor semantics come from §31.1 (101*80%=80, 101*50%=50, 101*20%=20)
with Decimal-only arithmetic — the large-value case below
(base_points > 2**53) is exact under Decimal and wrong under any binary
float path, and the 1*80% case pins floor (not round-half or truncate-
toward-zero on the .8 product).

Pure-function territory: no DB, no clock. The calculator receives every
instant it needs (backend-engineering §11), so `datetime.now` never
appears and no FrozenClock injection is required — inputs are planted
constants.

The §9.2 snapshot property ("之后 Teacher 修改 duration 不影响已存在
Claim") is tested in its pure-function analog: compute, mutate the task
row, assert the earlier result is unchanged. The claim service (T6)
persists exactly these values into the §6.2 MUST-snapshot columns, which
is what makes later Task edits unable to reach them.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.tasks.deadlines import (
    FRACTION_EARLY,
    FRACTION_FULL,
    FRACTION_LATE,
    FRACTION_MID,
    ClaimDeadlines,
    WindowClosedError,
    compute_claim_deadlines,
    reward_fraction,
    reward_points,
)
from app.modules.tasks.enums import DeadlineMode
from app.modules.tasks.models import Task

# --- planted instants ---------------------------------------------------------

ONE_MS = timedelta(milliseconds=1)
DEADLINE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
GRACE = DEADLINE + timedelta(hours=24)  # V1: grace = deadline + exactly 24h

CLAIMED_AT = datetime(2026, 9, 30, 8, 30, tzinfo=UTC)
FIXED_AT = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)

# Sentinel for "the window is closed at this instant".
CLOSED = object()


def deadline_task(mode: DeadlineMode, **overrides: Any) -> Task:
    """A hand-built Task row carrying only the calculator's inputs.

    Only the four columns `compute_claim_deadlines` reads are set (plus
    identity), mirroring what a DB-loaded row would carry; `grace_period_minutes`
    stays None unless overridden to model an un-flushed row whose
    server_default has not applied yet.
    """
    values: dict[str, Any] = {
        "id": uuid4(),
        "owner_teacher_id": uuid4(),
        "deadline_mode": mode,
        "fixed_deadline_at": FIXED_AT if mode is DeadlineMode.FIXED else None,
        "duration_minutes": 180 if mode is DeadlineMode.RELATIVE else None,
    }
    values.update(overrides)
    return Task(**values)


# --- §9.3: the 12-point boundary ladder (test -> fraction) --------------------
#
# offset from deadline | expected
# ---------------------+--------------------
# -1ms / +0            | Decimal("1")   (100%)
# +1ms .. +4h-1ms      | Decimal("0.8") (80%)
# +4h / +4h+1ms ..     | Decimal("0.5") (50%)
#   .. +12h-1ms        |
# +12h / +12h+1ms ..   | Decimal("0.2") (20%)
#   .. grace-1ms       |
# grace / grace+1ms    | WindowClosedError

BOUNDARY_CASES = [
    pytest.param(-ONE_MS, Decimal("1"), id="deadline-1ms"),
    pytest.param(timedelta(0), Decimal("1"), id="deadline-exact"),
    pytest.param(ONE_MS, Decimal("0.8"), id="deadline+1ms"),
    pytest.param(timedelta(hours=4) - ONE_MS, Decimal("0.8"), id="4h-1ms"),
    pytest.param(timedelta(hours=4), Decimal("0.5"), id="4h-exact"),
    pytest.param(timedelta(hours=4) + ONE_MS, Decimal("0.5"), id="4h+1ms"),
    pytest.param(timedelta(hours=12) - ONE_MS, Decimal("0.5"), id="12h-1ms"),
    pytest.param(timedelta(hours=12), Decimal("0.2"), id="12h-exact"),
    pytest.param(timedelta(hours=12) + ONE_MS, Decimal("0.2"), id="12h+1ms"),
    pytest.param(timedelta(hours=24) - ONE_MS, Decimal("0.2"), id="grace-1ms"),
    pytest.param(timedelta(hours=24), CLOSED, id="grace-exact"),
    pytest.param(timedelta(hours=24) + ONE_MS, CLOSED, id="grace+1ms"),
]


@pytest.mark.parametrize(("offset", "expected"), BOUNDARY_CASES)
def test_reward_fraction_boundary_ladder(offset: timedelta, expected: Any) -> None:
    submitted_at = DEADLINE + offset
    if expected is CLOSED:
        with pytest.raises(WindowClosedError):
            reward_fraction(submitted_at, DEADLINE, GRACE)
    else:
        assert reward_fraction(submitted_at, DEADLINE, GRACE) == expected


def test_reward_fraction_far_before_deadline_is_full() -> None:
    result = reward_fraction(DEADLINE - timedelta(days=3), DEADLINE, GRACE)
    assert result == Decimal("1")
    assert isinstance(result, Decimal)  # Decimal out, never float


def test_tier_constants_are_exact_decimals() -> None:
    """The four tiers are exact Decimal literals (§31: no binary float).

    Decimal("0.8") == 0.8 is False in Python (exact comparison), so the
    equality below also fails for any float-constructed constant.
    """
    assert Decimal("1") == FRACTION_FULL
    assert Decimal("0.8") == FRACTION_EARLY
    assert Decimal("0.5") == FRACTION_MID
    assert Decimal("0.2") == FRACTION_LATE
    for constant in (FRACTION_FULL, FRACTION_EARLY, FRACTION_MID, FRACTION_LATE):
        assert isinstance(constant, Decimal)


def test_window_closed_error_envelope_contract() -> None:
    with pytest.raises(WindowClosedError) as excinfo:
        reward_fraction(GRACE, DEADLINE, GRACE)
    error = excinfo.value
    assert isinstance(error, BusinessError)
    assert error.code == ErrorCode.SUBMISSION_WINDOW_CLOSED
    assert error.status_code == 400
    assert error.details == {
        "submitted_at": GRACE.isoformat(),
        "grace_deadline_at": GRACE.isoformat(),
    }


# --- §31.1: integer floor with Decimal-only arithmetic ------------------------


def test_reward_points_floor_for_101() -> None:
    assert reward_points(101, FRACTION_EARLY) == 80
    assert reward_points(101, FRACTION_MID) == 50
    assert reward_points(101, FRACTION_LATE) == 20


def test_reward_points_full_tier_is_identity() -> None:
    result = reward_points(101, FRACTION_FULL)
    assert result == 101
    assert type(result) is int


def test_reward_points_floors_below_one_to_zero() -> None:
    assert reward_points(1, FRACTION_EARLY) == 0  # floor(0.8) = 0


def test_reward_points_large_value_stays_exact() -> None:
    # 10**16 + 3 > 2**53: not representable as a binary float. Decimal is
    # exact: (10**16 + 3) * 0.5 = 5000000000000001.5 -> floor ...001.
    assert reward_points(10**16 + 3, FRACTION_MID) == 5000000000000001
    # 123456789 * 0.8 = 98765431.2 -> floor 98765431.
    assert reward_points(123456789, FRACTION_EARLY) == 98765431


def test_reward_points_rejects_binary_float_inputs() -> None:
    with pytest.raises(TypeError):
        reward_points(101, 0.8)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        reward_points(100.0, FRACTION_FULL)  # type: ignore[arg-type]


# --- §9.1/§9.2: deadline computation -------------------------------------------


def test_fixed_mode_uses_task_instant_grace_plus_24h() -> None:
    task = deadline_task(DeadlineMode.FIXED, grace_period_minutes=1440)
    result = compute_claim_deadlines(task, CLAIMED_AT)
    assert result == ClaimDeadlines(
        deadline_at=FIXED_AT, grace_deadline_at=FIXED_AT + timedelta(hours=24)
    )


def test_fixed_mode_ignores_claimed_at() -> None:
    task = deadline_task(DeadlineMode.FIXED)
    early = compute_claim_deadlines(task, CLAIMED_AT)
    late = compute_claim_deadlines(task, FIXED_AT - timedelta(minutes=1))
    assert early == late


def test_relative_mode_uses_claimed_at_plus_duration() -> None:
    task = deadline_task(DeadlineMode.RELATIVE, duration_minutes=180)
    result = compute_claim_deadlines(task, CLAIMED_AT)
    assert result.deadline_at == CLAIMED_AT + timedelta(hours=3)
    assert result.grace_deadline_at == result.deadline_at + timedelta(hours=24)


def test_relative_grace_falls_back_to_column_default() -> None:
    # Un-flushed rows have no server_default applied yet; the calculator
    # treats None as the column default (1440 = exactly 24h, spec §6/§9).
    task = deadline_task(DeadlineMode.RELATIVE, duration_minutes=60)
    assert task.grace_period_minutes is None
    result = compute_claim_deadlines(task, CLAIMED_AT)
    assert result.grace_deadline_at == result.deadline_at + timedelta(hours=24)


@pytest.mark.parametrize("mode", [DeadlineMode.FIXED, DeadlineMode.RELATIVE])
def test_grace_is_exactly_24h_in_both_modes(mode: DeadlineMode) -> None:
    task = deadline_task(mode, grace_period_minutes=1440)
    result = compute_claim_deadlines(task, CLAIMED_AT)
    assert result.grace_deadline_at - result.deadline_at == timedelta(hours=24)


def test_result_is_immutable() -> None:
    result = compute_claim_deadlines(deadline_task(DeadlineMode.RELATIVE), CLAIMED_AT)
    with pytest.raises(FrozenInstanceError):
        result.deadline_at = DEADLINE  # type: ignore[misc]


# --- §9.2/§6.2: snapshot survives task edits (pure-function analog) ------------


def test_relative_snapshot_survives_task_edit() -> None:
    task = deadline_task(DeadlineMode.RELATIVE, duration_minutes=180)
    result = compute_claim_deadlines(task, CLAIMED_AT)
    expected = ClaimDeadlines(
        deadline_at=CLAIMED_AT + timedelta(hours=3),
        grace_deadline_at=CLAIMED_AT + timedelta(hours=27),
    )
    assert result == expected

    # Teacher edits the task afterwards (spec §9.2): the values already
    # computed — and persisted by the claim service — must not move.
    task.duration_minutes = 9999
    task.fixed_deadline_at = FIXED_AT
    task.grace_period_minutes = 7
    assert result == expected

    # The task row itself did change: recomputing now differs, so the
    # snapshot (not luck) is what protects the persisted claim columns.
    assert compute_claim_deadlines(task, CLAIMED_AT) != expected


def test_fixed_snapshot_survives_task_edit() -> None:
    task = deadline_task(DeadlineMode.FIXED)
    result = compute_claim_deadlines(task, CLAIMED_AT)
    expected = ClaimDeadlines(
        deadline_at=FIXED_AT, grace_deadline_at=FIXED_AT + timedelta(hours=24)
    )
    assert result == expected

    task.fixed_deadline_at = FIXED_AT + timedelta(days=30)
    task.duration_minutes = 9999
    assert result == expected


# --- aware-only API ------------------------------------------------------------


@pytest.mark.parametrize(
    ("naive_slot", "submitted", "deadline", "grace"),
    [
        ("submitted_at", DEADLINE.replace(tzinfo=None), DEADLINE, GRACE),
        ("deadline_at", DEADLINE, DEADLINE.replace(tzinfo=None), GRACE),
        ("grace_deadline_at", DEADLINE, DEADLINE, GRACE.replace(tzinfo=None)),
    ],
    ids=["submitted", "deadline", "grace"],
)
def test_reward_fraction_rejects_naive_datetimes(
    naive_slot: str,
    submitted: datetime,
    deadline: datetime,
    grace: datetime,
) -> None:
    with pytest.raises(ValueError, match=naive_slot):
        reward_fraction(submitted, deadline, grace)


def test_compute_rejects_naive_claimed_at() -> None:
    task = deadline_task(DeadlineMode.RELATIVE)
    with pytest.raises(ValueError, match="claimed_at"):
        compute_claim_deadlines(task, CLAIMED_AT.replace(tzinfo=None))


def test_compute_rejects_naive_fixed_deadline() -> None:
    task = deadline_task(
        DeadlineMode.FIXED, fixed_deadline_at=FIXED_AT.replace(tzinfo=None)
    )
    with pytest.raises(ValueError, match="fixed_deadline_at"):
        compute_claim_deadlines(task, CLAIMED_AT)


# --- misconfigured task rows ---------------------------------------------------


def test_relative_without_duration_rejected() -> None:
    task = deadline_task(DeadlineMode.RELATIVE, duration_minutes=None)
    with pytest.raises(ValueError, match="duration_minutes"):
        compute_claim_deadlines(task, CLAIMED_AT)


def test_fixed_without_fixed_deadline_rejected() -> None:
    task = deadline_task(DeadlineMode.FIXED, fixed_deadline_at=None)
    with pytest.raises(ValueError, match="fixed_deadline_at"):
        compute_claim_deadlines(task, CLAIMED_AT)


def test_nonpositive_duration_rejected() -> None:
    # Mirrors the database CHECK `duration_minutes IS NULL OR > 0` for
    # hand-built rows that never passed through PostgreSQL.
    task = deadline_task(DeadlineMode.RELATIVE, duration_minutes=0)
    with pytest.raises(ValueError, match="duration_minutes"):
        compute_claim_deadlines(task, CLAIMED_AT)


def test_unknown_deadline_mode_rejected() -> None:
    task = deadline_task("WEEKLY")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="deadline_mode"):
        compute_claim_deadlines(task, CLAIMED_AT)
