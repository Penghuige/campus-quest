# backend/app/modules/tasks/deadlines.py
"""Deadline and reward-tier calculator (spec §9, §9.3, §11.5, §31.1,
§31.14; backend-engineering §11; plan 03 T5).

Pure functions only: no database, no clock, no I/O. Every instant is an
input — `datetime.now` never appears (backend-engineering §11: business
code receives a Clock; the caller owns "now" and passes `claimed_at` /
`submitted_at` in). All comparisons happen on timezone-aware datetimes,
which Python compares as UTC instants — exactly the §9.3 rule "所有比较
使用 UTC instant". Naive datetimes are rejected with `ValueError`
(aware-only API) because a naive value has no instant to compare.

Deadline computation (spec §9.1/§9.2), snapshotted at claim time:

- FIXED:     ``deadline_at = task.fixed_deadline_at`` — shared by all
  claims of the task; `claimed_at` does not participate.
- RELATIVE:  ``deadline_at = claimed_at + task.duration_minutes`` — a
  later Teacher edit of `duration` cannot affect existing claims
  (§9.2); this function's return value is what the claim service (T6)
  persists into the §6.2 MUST-snapshot columns.

Grace width decision: spec §9 writes ``grace_deadline_at = deadline_at +
24h`` and §6 fixes V1 grace at 1440 minutes with no product entry point.
The implementation therefore reads ``task.grace_period_minutes`` — the
database column, NOT NULL with server_default 1440 — rather than
hardcoding 1440, so a future spec change is a data/config change rather
than a code change. For rows not yet flushed through PostgreSQL (unit
tests, in-memory construction) the column reads None; that is treated
as the column default, 1440. Positivity mirrors the database CHECK.

Reward tiers (spec §9.3), the exact comparison table — note which edges
are inclusive:

    submitted_at <= deadline_at                    -> 100%  (1)
    deadline_at < submitted_at < deadline_at + 4h  ->  80%  (0.8)
    deadline_at + 4h <= submitted_at < +12h        ->  50%  (0.5)
    deadline_at + 12h <= submitted_at < grace      ->  20%  (0.2)
    submitted_at >= grace_deadline_at              -> closed

So exactly-at-deadline is 100%, exactly +4h is 50%, exactly +12h is
20%, and exactly-at-grace is closed. The window check runs FIRST: at or
after `grace_deadline_at` a submission cannot become newly valid
(§11.5: the DDL constrains student submission behavior — it never
constrains teacher review, which this module does not judge).

Points are integers (§31.14: no float money/points). Tier fractions are
exact `Decimal` literals and `reward_points` floors
``base_points * fraction`` with Decimal arithmetic only (§31.1:
101*80%=80, 101*50%=50, 101*20%=20); passing a binary-float fraction is
a `TypeError`, enforcing the no-float rule at this module's boundary
instead of relying on callers' discipline. ROUND_FLOOR implements
"mathematical floor" — identical to truncation for the non-negative
domain the database CHECKs guarantee (`base_reward_points > 0`, tier
fractions >= 0), but correct by construction rather than by accident.

`WindowClosedError` carries the registry code `SUBMISSION_WINDOW_CLOSED`
(§29) as a `BusinessError`, so the API envelope renders it without
translation; 400 is the provisional transport status pending an
interfaces.md assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.tasks.enums import DeadlineMode
from app.modules.tasks.models import Task

# --- frozen tier constants (spec §9.3; never binary floats) --------------------

FRACTION_FULL = Decimal("1")
FRACTION_EARLY = Decimal("0.8")
FRACTION_MID = Decimal("0.5")
FRACTION_LATE = Decimal("0.2")

# Tier widths measured from the deadline (spec §9.3).
_EARLY_TIER_WIDTH = timedelta(hours=4)
_MID_TIER_WIDTH = timedelta(hours=12)

# Spec §6/§9: V1 grace is exactly 24h. This is the database column's
# server_default, used only when the in-memory row has None (never
# flushed); persisted rows always carry the column value.
_GRACE_PERIOD_DEFAULT_MINUTES = 1440

_WINDOW_CLOSED_MESSAGE = "提交窗口已关闭"


class WindowClosedError(BusinessError):
    """`submitted_at` is at/after `grace_deadline_at` (spec §9.3: 禁止作为
    新有效提交). Maps to the registry code SUBMISSION_WINDOW_CLOSED (§29)."""

    def __init__(self, submitted_at: datetime, grace_deadline_at: datetime) -> None:
        super().__init__(
            ErrorCode.SUBMISSION_WINDOW_CLOSED,
            _WINDOW_CLOSED_MESSAGE,
            status_code=400,
            details={
                "submitted_at": submitted_at.isoformat(),
                "grace_deadline_at": grace_deadline_at.isoformat(),
            },
        )


@dataclass(frozen=True)
class ClaimDeadlines:
    """The two instants the claim service snapshots at claim time (§6.2).

    Frozen on purpose: these are the values later tier checks read, and
    nothing downstream may nudge them after the fact.
    """

    deadline_at: datetime
    grace_deadline_at: datetime


def _require_aware(value: datetime, name: str) -> None:
    """Enforce the aware-only API; naive datetimes have no UTC instant."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must be a timezone-aware datetime (UTC instant), "
            f"got naive {value!r}"
        )


def compute_claim_deadlines(task: Task, claimed_at: datetime) -> ClaimDeadlines:
    """Derive `deadline_at` / `grace_deadline_at` for a new claim.

    FIXED reads the task's shared instant; RELATIVE anchors on
    `claimed_at` (spec §9.1/§9.2). Both modes add the grace width to the
    resulting deadline. Misconfigured rows (mode without its required
    column, non-positive duration/grace, unknown mode) raise `ValueError`
    — they mirror the database CHECKs for rows that never passed through
    PostgreSQL, and reaching them from a persisted PUBLISHED task would
    mean publish validation was bypassed.
    """

    _require_aware(claimed_at, "claimed_at")

    mode = task.deadline_mode
    if mode == DeadlineMode.FIXED:
        fixed_deadline_at = task.fixed_deadline_at
        if fixed_deadline_at is None:
            raise ValueError(
                "FIXED task is missing fixed_deadline_at; publish validation "
                "should have rejected this row"
            )
        _require_aware(fixed_deadline_at, "fixed_deadline_at")
        deadline_at = fixed_deadline_at
    elif mode == DeadlineMode.RELATIVE:
        duration_minutes = task.duration_minutes
        if duration_minutes is None or duration_minutes <= 0:
            raise ValueError(
                f"RELATIVE task has invalid duration_minutes "
                f"{duration_minutes!r}; must be a positive integer"
            )
        deadline_at = claimed_at + timedelta(minutes=duration_minutes)
    else:
        raise ValueError(f"unknown deadline_mode {mode!r}; expected FIXED or RELATIVE")

    grace_minutes = task.grace_period_minutes
    if grace_minutes is None:
        # Column server_default not applied yet (never-flushed row).
        grace_minutes = _GRACE_PERIOD_DEFAULT_MINUTES
    if grace_minutes <= 0:
        raise ValueError(
            f"task has invalid grace_period_minutes {grace_minutes!r}; "
            "must be a positive integer"
        )

    return ClaimDeadlines(
        deadline_at=deadline_at,
        grace_deadline_at=deadline_at + timedelta(minutes=grace_minutes),
    )


def reward_fraction(
    submitted_at: datetime,
    deadline_at: datetime,
    grace_deadline_at: datetime,
) -> Decimal:
    """Tier fraction for a submission instant (spec §9.3 table verbatim).

    Raises `WindowClosedError` (SUBMISSION_WINDOW_CLOSED) at or after
    `grace_deadline_at`. Tier edges are inclusive at the deadline (100%),
    at +4h (50%), and at +12h (20%).
    """

    _require_aware(submitted_at, "submitted_at")
    _require_aware(deadline_at, "deadline_at")
    _require_aware(grace_deadline_at, "grace_deadline_at")

    if submitted_at >= grace_deadline_at:
        raise WindowClosedError(submitted_at, grace_deadline_at)
    if submitted_at <= deadline_at:
        return FRACTION_FULL
    if submitted_at < deadline_at + _EARLY_TIER_WIDTH:
        return FRACTION_EARLY
    if submitted_at < deadline_at + _MID_TIER_WIDTH:
        return FRACTION_MID
    return FRACTION_LATE


def reward_points(base_points: int, fraction: Decimal) -> int:
    """Floor of `base_points * fraction` in pure Decimal arithmetic.

    Spec §31.1: 101*80% -> 80, 101*50% -> 50, 101*20% -> 20. Binary
    floats are rejected (`TypeError`) so no float artifact can reach an
    integer points column (§31.14).
    """

    if isinstance(base_points, bool) or not isinstance(base_points, int):
        raise TypeError(f"base_points must be int, got {type(base_points).__name__}")
    if not isinstance(fraction, Decimal):
        raise TypeError(
            "fraction must be decimal.Decimal, got "
            f"{type(fraction).__name__}; binary floats are forbidden "
            "(spec §31.1/§31.14)"
        )
    return int((base_points * fraction).to_integral_value(rounding=ROUND_FLOOR))
