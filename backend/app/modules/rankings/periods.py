# backend/app/modules/rankings/periods.py
"""Business-period math for rankings (spec §17; backend-engineering §11).

Documented choice (interfaces.md, "Ranking projection"): period
boundaries are computed PYTHON-SIDE with ``zoneinfo`` from
``settings.business_timezone``; the SQL aggregation only filters UTC
instant ranges (``>= start AND < end``). There is deliberately no
``func.timezone`` / ``date_trunc`` SQL and no hardcoded hour offset —
DST and every IANA zone stay correct, which the §38.9 DST test pins
(America/New_York fall-back shifts a business day's start by an hour;
a fixed offset would mis-bucket the straddling entries).

Half-open UTC ranges: a business day is ``[start, end)`` so the last
instant of one period and the first of the next never double-count.

Local midnights that do not exist (zones whose DST transition skips
past 00:00) are resolved by ``zoneinfo`` per PEP 495 — the bound moves
inside the gap but stays monotone; CampusQuest's zones (Asia/Shanghai
and peers) have no such transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

__all__ = [
    "ALL_TIME_KEY",
    "Granularity",
    "RankingPeriod",
    "business_day",
    "business_month",
    "daily_key",
    "day_bounds",
    "month_bounds",
    "monthly_key",
]

Granularity = Literal["daily", "monthly", "all"]

#: The all-time board's single global key (spec §17.3).
ALL_TIME_KEY = "ranking:all"


def _require_aware(effective_at: datetime) -> datetime:
    if effective_at.tzinfo is None:
        raise ValueError(
            "ranking_effective_at must be timezone-aware (persisted UTC instants)"
        )
    return effective_at


def business_day(effective_at: datetime, tz: ZoneInfo) -> date:
    """The BUSINESS_TIMEZONE natural day an instant belongs to."""
    return _require_aware(effective_at).astimezone(tz).date()


def business_month(effective_at: datetime, tz: ZoneInfo) -> date:
    """The first day of the BUSINESS_TIMEZONE month an instant belongs to."""
    local = _require_aware(effective_at).astimezone(tz)
    return date(local.year, local.month, 1)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The half-open ``[start, end)`` UTC instant range of a business day."""
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _next_month(month: date) -> date:
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


def month_bounds(month: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The half-open ``[start, end)`` UTC instant range of a business month.

    ``month`` is any day of the month (normalized to its first day).
    """
    first = date(month.year, month.month, 1)
    start_local = datetime.combine(first, time.min, tzinfo=tz)
    end_local = datetime.combine(_next_month(first), time.min, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def daily_key(day: date) -> str:
    """``ranking:daily:<YYYY-MM-DD>`` (spec §17.3 key contract)."""
    return f"ranking:daily:{day.isoformat()}"


def monthly_key(month: date) -> str:
    """``ranking:monthly:<YYYY-MM>`` (spec §17.3 key contract)."""
    return f"ranking:monthly:{month:%Y-%m}"


@dataclass(frozen=True, slots=True)
class RankingPeriod:
    """Which board a read addresses: one business day, one business
    month, or all-time.

    Constructed through the named constructors so an impossible state
    (a "daily" period without a day) cannot exist; ``redis_key`` is the
    single place the §17.3 key spelling lives for reads.
    """

    granularity: Granularity
    day: date | None = None
    month: date | None = None

    @classmethod
    def daily(cls, day: date) -> RankingPeriod:
        return cls(granularity="daily", day=day)

    @classmethod
    def monthly(cls, month: date) -> RankingPeriod:
        # Any day of the month names the month; normalize to its first
        # day so equality is well-defined.
        return cls(granularity="monthly", month=date(month.year, month.month, 1))

    @classmethod
    def all_time(cls) -> RankingPeriod:
        return cls(granularity="all")

    def redis_key(self) -> str:
        if self.granularity == "daily":
            if self.day is None:  # pragma: no cover - constructors prevent it
                raise ValueError("daily period requires a day")
            return daily_key(self.day)
        if self.granularity == "monthly":
            if self.month is None:  # pragma: no cover - constructors prevent it
                raise ValueError("monthly period requires a month")
            return monthly_key(self.month)
        return ALL_TIME_KEY
