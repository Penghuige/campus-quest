# backend/tests/unit/rankings/test_honors.py
"""Fixed-rule honor evaluation, pure part (spec §18; plan 05 task 7).

The five first-version auto honor types are FROZEN rules, not data: the
catalog (which definitions exist) and the matcher (which definitions a
fact state qualifies for) are pure functions, so every threshold and
rank cutoff is pinned here without a database. The DB half — one Honor
row per period, idempotent grants, display selection — lives in
tests/integration/rankings/test_honor_grants.py.

Threshold provenance: TOTAL_COMPLETED 1/10/50 and ON_TIME_STREAK 10 are
spec §18 examples; TOTAL_EARNED_POINTS 500/2000/10000 and the monthly
Top-3 cutoff are the documented first-version ruling (honor_service
module docstring); DAILY_RANK/MONTHLY_RANK "rank == 1 -> 卷王" is the
spec's 今日卷王/本月卷王 example.
"""

from __future__ import annotations

import pytest

from app.modules.rankings.honor_models import HonorType
from app.modules.rankings.honor_service import (
    FIXED_HONOR_DEFINITIONS,
    HonorDefinition,
    HonorEvaluationEvent,
    HonorFacts,
    HonorTrigger,
    matching_honor_definitions,
)


def _names(
    definitions: tuple[HonorDefinition, ...],
) -> set[str]:
    return {definition.name for definition in definitions}


def _matched(
    trigger: HonorTrigger = HonorTrigger.CLAIM_COMPLETED,
    *,
    completed: int = 0,
    streak: int = 0,
    earned: int = 0,
) -> tuple[HonorDefinition, ...]:
    event = HonorEvaluationEvent(trigger=trigger)
    return matching_honor_definitions(
        event,
        HonorFacts(
            completed_count=completed, on_time_streak=streak, earned_points=earned
        ),
    )


# --- the frozen catalog (spec §18) ----------------------------------------------------


def test_fixed_catalog_is_exactly_the_spec_example_set() -> None:
    """Five types, ten definitions: 1/10/50 completed, streak 10, daily
    top-1, monthly top-1 + top-3, 500/2000/10000 earned points."""
    by_type: dict[HonorType, list[int]] = {}
    for definition in FIXED_HONOR_DEFINITIONS:
        cutoff = (
            definition.threshold
            if definition.threshold is not None
            else definition.best_rank
        )
        assert cutoff is not None, "every fixed definition carries a cutoff"
        by_type.setdefault(definition.honor_type, []).append(cutoff)
    assert sorted(by_type[HonorType.TOTAL_COMPLETED]) == [1, 10, 50]
    assert by_type[HonorType.ON_TIME_STREAK] == [10]
    assert by_type[HonorType.DAILY_RANK] == [1]
    assert sorted(by_type[HonorType.MONTHLY_RANK]) == [1, 3]
    assert sorted(by_type[HonorType.TOTAL_EARNED_POINTS]) == [500, 2000, 10_000]
    # Names stay unique so an (honor_type, name, period) row is a stable
    # definition identity.
    assert len(_names(FIXED_HONOR_DEFINITIONS)) == len(FIXED_HONOR_DEFINITIONS)
    # Admin commemorative honors are NOT part of the fixed auto catalog.
    assert HonorType.COMMEMORATIVE not in by_type


def test_rank_definitions_are_periodic_and_lifetime_are_not() -> None:
    """Only DAILY_RANK/MONTHLY_RANK definitions are periodic (spec §18:
    a periodic honor must carry a period)."""
    periodic = {HonorType.DAILY_RANK, HonorType.MONTHLY_RANK}
    for definition in FIXED_HONOR_DEFINITIONS:
        assert definition.periodic is (definition.honor_type in periodic)


# --- lifetime thresholds (claim_completed / points_changed) ---------------------------


def test_total_completed_thresholds_grant_inclusively_and_self_heal() -> None:
    """A fact state qualifies for EVERY definition at or below it: a user
    who quietly reaches 50 completed claims earns 1/10/50 in ONE
    evaluation even if the earlier trigger events were lost."""
    assert _names(_matched(completed=0)) == set()
    assert _names(_matched(completed=1)) == {"首次完成任务"}
    assert _names(_matched(completed=9)) == {"首次完成任务"}
    assert _names(_matched(completed=10)) == {"首次完成任务", "累计完成 10 个任务"}
    assert _names(_matched(completed=49)) == {"首次完成任务", "累计完成 10 个任务"}
    assert _names(_matched(completed=50)) == {
        "首次完成任务",
        "累计完成 10 个任务",
        "累计完成 50 个任务",
    }


def test_on_time_streak_grants_at_ten_or_more() -> None:
    assert _names(_matched(streak=9)) == set()
    assert _names(_matched(streak=10)) == {"连续 10 个任务按时"}
    assert _names(_matched(streak=31)) == {"连续 10 个任务按时"}


def test_total_earned_points_thresholds() -> None:
    assert _names(_matched(earned=499)) == set()
    assert _names(_matched(earned=500)) == {"累计获得 500 积分"}
    assert _names(_matched(earned=1999)) == {"累计获得 500 积分"}
    assert _names(_matched(earned=2000)) == {"累计获得 500 积分", "累计获得 2000 积分"}
    assert _names(_matched(earned=10_000)) == {
        "累计获得 500 积分",
        "累计获得 2000 积分",
        "累计获得 10000 积分",
    }


def test_lifetime_triggers_combine_all_lifetime_types() -> None:
    """claim_completed and points_changed both evaluate the whole
    lifetime catalog from current facts (the trigger says which facts
    PLAUSIBLY changed, not which honors may be awarded)."""
    combined = {"首次完成任务", "连续 10 个任务按时", "累计获得 500 积分"}
    assert _names(_matched(completed=1, streak=10, earned=500)) == combined
    assert (
        _names(
            _matched(HonorTrigger.POINTS_CHANGED, completed=1, streak=10, earned=500)
        )
        == combined
    )


# --- periodic rank honors -------------------------------------------------------------


def test_daily_rank_grants_top_one_only() -> None:
    event = HonorEvaluationEvent(
        trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=1, period="2026-09-21"
    )
    assert _names(matching_honor_definitions(event, HonorFacts())) == {"今日卷王"}
    second = HonorEvaluationEvent(
        trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=2, period="2026-09-21"
    )
    assert matching_honor_definitions(second, HonorFacts()) == ()


def test_monthly_rank_grants_top_one_and_top_three() -> None:
    top = HonorEvaluationEvent(
        trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-09"
    )
    assert _names(matching_honor_definitions(top, HonorFacts())) == {
        "本月卷王",
        "月度 Top 3",
    }
    third = HonorEvaluationEvent(
        trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=3, period="2026-09"
    )
    assert _names(matching_honor_definitions(third, HonorFacts())) == {"月度 Top 3"}
    fourth = HonorEvaluationEvent(
        trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=4, period="2026-09"
    )
    assert matching_honor_definitions(fourth, HonorFacts()) == ()


def test_rank_matching_ignores_lifetime_facts() -> None:
    """A rank event never re-evaluates lifetime thresholds: the two rule
    families are disjoint by trigger."""
    rich = HonorFacts(completed_count=50, on_time_streak=10, earned_points=10_000)
    event = HonorEvaluationEvent(
        trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=1, period="2026-09-21"
    )
    assert _names(matching_honor_definitions(event, rich)) == {"今日卷王"}


# --- event shape validation -----------------------------------------------------------


def test_rank_events_require_rank_and_wellformed_period() -> None:
    with pytest.raises(ValueError, match="rank"):
        HonorEvaluationEvent(trigger=HonorTrigger.DAILY_RANK_KNOWN, period="2026-09-21")
    with pytest.raises(ValueError, match="period"):
        HonorEvaluationEvent(trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1)
    # Daily periods are YYYY-MM-DD; a monthly key is rejected there and a
    # daily key is rejected on the monthly trigger.
    with pytest.raises(ValueError, match="period"):
        HonorEvaluationEvent(
            trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=1, period="2026-09"
        )
    with pytest.raises(ValueError, match="period"):
        HonorEvaluationEvent(
            trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-09-21"
        )
    with pytest.raises(ValueError, match="rank"):
        HonorEvaluationEvent(
            trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=0, period="2026-09-21"
        )


def test_lifetime_events_reject_rank_facts() -> None:
    with pytest.raises(ValueError, match="rank"):
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED, rank=1)
    with pytest.raises(ValueError, match="period"):
        HonorEvaluationEvent(trigger=HonorTrigger.POINTS_CHANGED, period="2026-09")
