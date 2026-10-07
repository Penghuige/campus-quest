# backend/tests/unit/points/test_wallet_clamp_properties.py
"""Property-based invariants of the wallet display clamp (spec §15.1).

The clamp is a pure display decision (``_wallet_response``); the
example suite (``test_wallet_clamp``) pins known shapes — this module
asserts the RULES over the whole input domain instead, per the
property-based practice (owner-approved P2 batch):

- boundedness: the three user-facing figures never render negative;
- partition: ``available_points`` and ``point_debt`` are a lossless
  pair — exactly one of them is nonzero, and
  ``available - debt == raw`` (the raw balance is recoverable from the
  display pair, never destroyed by the clamp);
- ordering: ``spendable <= available`` (reservations can only withhold);
- passthrough: ``earned`` rides untouched (redeem/reward semantics
  live elsewhere; the clamp must not touch them).

Deliberately NOT asserted: the max() formulas themselves — restating
the implementation would be a tautology that no shared bug can fail.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.modules.points.ledger_service import WalletSummary
from app.modules.points.router import _wallet_response

_MILLION = 1_000_000_000


@st.composite
def raw_wallet_summaries(draw: st.DrawFn) -> WalletSummary:
    """A coherent raw summary: spendable is DERIVED (raw minus a
    non-negative reservation total), never generated independently —
    the constraint lives in the strategy, not in an ``assume()``."""
    raw_available = draw(st.integers(min_value=-_MILLION, max_value=_MILLION))
    earned = draw(st.integers(min_value=0, max_value=_MILLION))
    reservations = draw(st.integers(min_value=0, max_value=_MILLION))
    return WalletSummary(
        available_points=raw_available,
        earned_points=earned,
        spendable_points=raw_available - reservations,
    )


@settings(max_examples=200)
@given(raw_wallet_summaries())
@example(WalletSummary(available_points=0, earned_points=0, spendable_points=0))
@example(WalletSummary(available_points=-150, earned_points=200, spendable_points=-150))
@example(WalletSummary(available_points=100, earned_points=100, spendable_points=70))
@example(
    # reservations exceeding a positive balance clamp spendable only
    WalletSummary(available_points=100, earned_points=100, spendable_points=-30)
)
def test_clamp_family_bounds_partition_and_ordering(
    summary: WalletSummary,
) -> None:
    rendered = _wallet_response(summary)

    assert rendered.available_points >= 0
    assert rendered.spendable_points >= 0
    assert rendered.point_debt >= 0

    # Partition: at most one of the pair is nonzero...
    assert min(rendered.available_points, rendered.point_debt) == 0
    # ...and the pair is lossless for the raw balance.
    assert rendered.available_points - rendered.point_debt == summary.available_points

    assert rendered.spendable_points <= rendered.available_points
    assert rendered.earned_points == summary.earned_points
    # Type discipline (spec §31.14): the figures stay integers.
    for value in (
        rendered.available_points,
        rendered.spendable_points,
        rendered.point_debt,
    ):
        assert isinstance(value, int) and not isinstance(value, bool)


@settings(max_examples=100)
@given(st.integers(min_value=-_MILLION, max_value=_MILLION))
def test_debt_is_exactly_the_negative_part(raw_available: int) -> None:
    """The overdraft surfaces exactly the negative half of the raw
    balance — the §15.1 definition as a partition property."""
    summary = WalletSummary(
        available_points=raw_available,
        earned_points=0,
        spendable_points=min(raw_available, 0),
    )
    rendered = _wallet_response(summary)
    if raw_available < 0:
        assert rendered.point_debt == -raw_available
        assert rendered.available_points == 0
    else:
        assert rendered.point_debt == 0
        assert rendered.available_points == raw_available
