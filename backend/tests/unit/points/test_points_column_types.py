# backend/tests/unit/points/test_points_column_types.py
"""The points module's quantity columns are integers only (spec §31.14;
points-ledger matrix E19 / gap G-2).

Integral points are a schema-level product rule: every points/amount
column across the ledger, the wallet projection, the reward catalogue,
and the redemption/reservation snapshots is Integer or BigInteger —
never a floating-point storage type — so no code path can drift a
quantity into fractional arithmetic the spec never allows. Model-level
freeze in the by-construction discipline of the DTO field-set freezes
(identity's ``test_user_public_excludes_private_fields``): the quantity
columns are pinned as a closed named table with their EXACT integer
types, and every column of every points model is swept for Float/
Numeric storage.

Pure ORM-metadata assertions — no database, no migration readout. The
shipping migrations match by construction (every model column here was
born in a migration); this freeze is the anti-drift pin for future
column additions and type edits.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Float, Integer, Numeric

from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
    RewardReviewGrant,
)

# The quantity columns and their exact integer storage types, as a
# closed table: a rename, a type edit, or a removal must fail here, and
# a NEW quantity column must be added consciously (spec §31.14).
_QUANTITY_COLUMN_TYPES: dict[type[Any], dict[str, type[Any]]] = {
    PointsLedger: {"amount": BigInteger},
    PointWallet: {
        "available_points": BigInteger,
        "earned_points": BigInteger,
    },
    RewardItem: {
        "point_cost": Integer,
        "stock": Integer,
        "per_user_term_limit": Integer,
    },
    RewardRedemption: {"points": BigInteger},
    PointReservation: {"points": BigInteger},
}

# Every model the module owns joins the no-floating-point sweep, the
# quantity-bearing tables and the grant table alike.
_POINTS_MODELS: tuple[type[Any], ...] = (
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
    PointReservation,
    RewardReviewGrant,
)


def test_quantity_columns_freeze_their_exact_integer_types() -> None:
    """Each named quantity column keeps its exact Integer/BigInteger
    storage — the spec §31.14 "积分列全 integer" pin, name by name."""
    for model, expected_columns in _QUANTITY_COLUMN_TYPES.items():
        table = model.__table__
        for column_name, expected_type in expected_columns.items():
            assert column_name in table.columns, (
                f"{model.__name__}.{column_name} disappeared — the §31.14 "
                "integer freeze table needs a conscious update"
            )
            actual = type(table.columns[column_name].type)
            assert actual is expected_type, (
                f"{model.__name__}.{column_name} drifted from "
                f"{expected_type.__name__} to {actual.__name__} (spec §31.14: "
                "points columns are integers only)"
            )


def test_no_points_model_column_uses_floating_point_storage() -> None:
    """No column of any points model is Float/Numeric — Double and REAL
    subclass Float and DECIMAL subclasses Numeric, so the isinstance
    sweep rejects every fractional storage type, present or future."""
    for model in _POINTS_MODELS:
        for column_name, column in model.__table__.columns.items():
            assert not isinstance(column.type, (Float, Numeric)), (
                f"{model.__name__}.{column_name} uses floating-point storage "
                f"{type(column.type).__name__} — spec §31.14 mandates integer "
                "points columns with no fractional drift"
            )
