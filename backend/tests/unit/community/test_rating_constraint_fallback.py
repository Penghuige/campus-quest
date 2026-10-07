# backend/tests/unit/community/test_rating_constraint_fallback.py
"""Deterministic pin for the IntegrityError constraint-name fallback.

Coverage forensics (2026-10-08 flake hunt): the message-regex fallback
in ``rating_service._constraint_name`` was only ever exercised when the
concurrent-ratings race produced an exception object WITHOUT the
driver's ``constraint_name`` attribute — a timing-dependent shape that
made the community module's coverage flap 0.2pp between identical runs
(the PR #34 ratchet event). The defensive path is real product code
(unnamed or wrapped violations are exactly when it earns its keep), so
its coverage must not depend on race luck: this module feeds it both
shapes directly — attribute present, attribute absent with the name in
the message, and neither — and asserts the extraction contract.
"""

from __future__ import annotations

import re

from sqlalchemy.exc import IntegrityError

from app.modules.community.rating_service import _constraint_name

_MESSAGE = re.compile(r'constraint "(?P<name>[^"]+)"')


class _DriverError(Exception):
    """A stand-in driver error: shape controlled per case."""

    def __init__(self, message: str, attribute: str | None = None) -> None:
        super().__init__(message)
        if attribute is not None:
            self.constraint_name = attribute


def _wrap(orig: Exception) -> IntegrityError:
    return IntegrityError("statement", {}, orig)  # type: ignore[arg-type]


def test_driver_attribute_wins_over_the_message() -> None:
    error = _wrap(
        _DriverError(
            'duplicate key ... constraint "uq_some_index"', attribute="uq_real_name"
        )
    )
    assert _constraint_name(error) == "uq_real_name"


def test_missing_attribute_falls_back_to_the_message_regex() -> None:
    # The flake's exact path: no attribute, the name only in the text.
    error = _wrap(
        _DriverError('duplicate key ... constraint "uq_task_ratings" runs 1,2')
    )
    assert _constraint_name(error) == "uq_task_ratings"


def test_neither_shape_yields_none() -> None:
    error = _wrap(_DriverError("some unnamed integrity problem"))
    assert _constraint_name(error) is None


def test_blank_attribute_falls_back_to_the_message() -> None:
    # Falsy attributes count as absent: ``name`` must be a non-empty str.
    error = _wrap(
        _DriverError('violation ... constraint "uq_blank_case"', attribute="")
    )
    assert _constraint_name(error) == "uq_blank_case"
