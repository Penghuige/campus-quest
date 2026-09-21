# backend/tests/unit/points/test_insufficient_points_detail.py
"""The InsufficientPointsError public detail never renders a negative
spendable (spec §15.1 display rule; PR #2 closure review): the deciding
comparison keeps the RAW value — a reward reversal can overdraft the
wallet negative — while the error envelope clamps at 0 like every other
user-facing surface."""


from app.modules.points.redemption_service import InsufficientPointsError


def test_detail_clamps_negative_spendable_to_zero() -> None:
    error = InsufficientPointsError(required=500, spendable=-150)
    assert error.details == {"required": 500, "spendable": 0}


def test_detail_passes_through_non_negative_spendable() -> None:
    error = InsufficientPointsError(required=500, spendable=120)
    assert error.details == {"required": 500, "spendable": 120}
