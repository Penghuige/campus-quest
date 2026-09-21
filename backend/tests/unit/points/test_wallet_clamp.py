# backend/tests/unit/points/test_wallet_clamp.py
"""The wallet display clamp mapping (PR #2 hardening, Task 3).

``points.router._wallet_response`` is the display-only serializer: the
raw ``WalletSummary`` (which keeps TRUE negative balances — migration
0012's overdraft ruling, the ledger==wallet invariant is rebuildable)
maps to the user-facing DTO as:

- ``available_points  = max(raw_balance, 0)``
- ``spendable_points  = max(raw_balance - reservations, 0)`` (arrives
  pre-derived in the summary's ``spendable_points``)
- ``point_debt        = max(-raw_balance, 0)``

Pure mapping tests — no database, no HTTP (the API surface asserts the
same shape end-to-end in tests/integration/points/test_points_api.py,
including the real-ledger path to a -150 wallet).
"""

from __future__ import annotations

import pytest

from app.modules.points.ledger_service import WalletSummary
from app.modules.points.router import WalletResponse, _wallet_response


def test_healthy_wallet_maps_unchanged_with_zero_debt() -> None:
    response = _wallet_response(
        WalletSummary(available_points=1000, earned_points=1000, spendable_points=900)
    )
    assert response == WalletResponse(
        available_points=1000,
        earned_points=1000,
        spendable_points=900,
        point_debt=0,
    )


def test_zero_wallet_maps_zeros() -> None:
    response = _wallet_response(
        WalletSummary(available_points=0, earned_points=0, spendable_points=0)
    )
    assert response.point_debt == 0
    assert (response.available_points, response.spendable_points) == (0, 0)


def test_overdrawn_wallet_clamps_and_carries_the_debt() -> None:
    """The -150 overdraft (grant 200, spend 150, full reversal): both
    balance figures clamp at 0 and the overdraft surfaces as
    point_debt — earned is never touched by spending (spec §17.1)."""
    response = _wallet_response(
        WalletSummary(available_points=-150, earned_points=200, spendable_points=-150)
    )
    assert response == WalletResponse(
        available_points=0,
        earned_points=200,
        spendable_points=0,
        point_debt=150,
    )


@pytest.mark.parametrize(
    ("available", "spendable", "expected_debt"),
    [
        (-1, -1, 1),  # the smallest overdraft
        (-150, -150, 150),  # the S1 T5 asset figure
        (-150, -200, 150),  # debt is the BALANCE mirror, never the spendable's
    ],
)
def test_point_debt_mirrors_the_raw_balance_only(
    available: int, spendable: int, expected_debt: int
) -> None:
    """point_debt = max(-raw_balance, 0) exactly — even when ACTIVE
    reservations push spendable deeper than the balance itself."""
    response = _wallet_response(
        WalletSummary(
            available_points=available, earned_points=0, spendable_points=spendable
        )
    )
    assert response.available_points == 0
    assert response.spendable_points == 0
    assert response.point_debt == expected_debt


def test_positive_wallet_with_frozen_spendable_clamps_spendable_only() -> None:
    """A positive balance with reservations deeper than the balance
    (theoretical under the redemption gate, pinned anyway): spendable
    clamps at 0, available stays true-positive, no debt is invented."""
    response = _wallet_response(
        WalletSummary(available_points=100, earned_points=100, spendable_points=-50)
    )
    assert response == WalletResponse(
        available_points=100,
        earned_points=100,
        spendable_points=0,
        point_debt=0,
    )
