"""Unit tests for the §9.3 tier oracle (plan Task 6, check (e))."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loadtest.check_s1 import tier_bucket

BASE = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
DEADLINE = BASE
GRACE = DEADLINE + timedelta(hours=24)


def test_boundaries_are_closed_on_the_early_side() -> None:
    # §9.3: exactly at deadline -> 100; exactly +4h -> 50; +12h -> 20;
    # exactly +24h (grace) -> rejected
    assert tier_bucket(DEADLINE, DEADLINE, GRACE) == 100
    assert tier_bucket(DEADLINE + timedelta(hours=4), DEADLINE, GRACE) == 50
    assert tier_bucket(DEADLINE + timedelta(hours=12), DEADLINE, GRACE) == 20
    assert tier_bucket(GRACE, DEADLINE, GRACE) is None


def test_interior_points_hit_each_band() -> None:
    assert tier_bucket(DEADLINE - timedelta(hours=1), DEADLINE, GRACE) == 100
    assert tier_bucket(DEADLINE + timedelta(hours=1), DEADLINE, GRACE) == 80
    assert tier_bucket(DEADLINE + timedelta(hours=5), DEADLINE, GRACE) == 50
    assert tier_bucket(DEADLINE + timedelta(hours=13), DEADLINE, GRACE) == 20


def test_naive_datetimes_are_treated_as_utc() -> None:
    naive = DEADLINE.replace(tzinfo=None) + timedelta(minutes=30)
    assert tier_bucket(naive, DEADLINE, GRACE) == 80
