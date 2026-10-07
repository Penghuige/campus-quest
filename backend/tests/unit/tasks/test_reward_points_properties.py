# backend/tests/unit/tasks/test_reward_points_properties.py
"""Property-based domain rules of the tiered reward arithmetic (spec §31.1/§31.14).

The example suite pins the documented numbers (101×80%→80 family);
these properties assert the RULE over arbitrary inputs:

- **Spec oracle**: the result equals ``floor(base × fraction)`` computed
  independently in Decimal — the spec formula is the oracle, so any
  implementation arithmetic (float-free or otherwise) that agrees with
  the spec passes, and any drift fails;
- **Integer discipline**: the return type is a true ``int`` — no float
  artifact can reach an integer points column, and floats are rejected
  at the boundary (``TypeError``) rather than silently coerced;
- **Boundedness & monotonicity**: for fractions in [0, 1] the result
  stays within ``[0, base]`` and never decreases as the fraction grows.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.modules.tasks.deadlines import (
    FRACTION_EARLY,
    FRACTION_FULL,
    FRACTION_LATE,
    FRACTION_MID,
    reward_points,
)

_BASES = st.integers(min_value=0, max_value=10**9)
_FRACTIONS_IN_UNIT = st.decimals(
    min_value=0, max_value=1, places=4, allow_nan=False, allow_infinity=False
)


def _spec_floor(base: int, fraction: Decimal) -> int:
    """The spec formula, computed independently of the implementation."""
    product = Decimal(base) * fraction
    return int(product.to_integral_value(rounding=ROUND_FLOOR))


@settings(max_examples=200)
@given(base=_BASES, fraction=_FRACTIONS_IN_UNIT)
@example(0, FRACTION_FULL)
@example(1, FRACTION_EARLY)  # floor(0.8) = 0
@example(101, FRACTION_EARLY)  # the spec's own worked example
@example(101, FRACTION_MID)
@example(101, FRACTION_LATE)
def test_reward_points_matches_the_spec_floor_formula(
    base: int, fraction: Decimal
) -> None:
    result = reward_points(base, fraction)
    assert result == _spec_floor(base, fraction)


@settings(max_examples=100)
@given(base=_BASES, fraction=_FRACTIONS_IN_UNIT)
def test_result_is_a_true_integer(base: int, fraction: Decimal) -> None:
    result = reward_points(base, fraction)
    assert isinstance(result, int) and not isinstance(result, bool)


@settings(max_examples=100)
@given(base=_BASES, fraction=_FRACTIONS_IN_UNIT)
def test_float_fractions_are_rejected_not_coerced(base: int, fraction: Decimal) -> None:
    with pytest.raises(TypeError):
        reward_points(base, float(fraction))


@settings(max_examples=100)
@given(base=_BASES, small=_FRACTIONS_IN_UNIT, large=_FRACTIONS_IN_UNIT)
def test_bounded_and_monotone_in_the_fraction(
    base: int, small: Decimal, large: Decimal
) -> None:
    if small > large:
        small, large = large, small
    low = reward_points(base, small)
    high = reward_points(base, large)
    assert 0 <= low <= high <= base
