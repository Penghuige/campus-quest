# backend/tests/unit/integrations/test_masking.py
"""Log-masking forms for contact data and log-sensitive identifiers.

Unit coverage for the shared masking helpers (spec §5.4, §40; backend-
engineering §15) and the rate limiter's identifier dispatch: a student
number IS log-sensitive (it is the student's login username, spec §5.2), so
the 429 log line must never carry one verbatim — same rule as phones and
emails.
"""

from __future__ import annotations

import pytest

from app.integrations.masking import mask_email, mask_phone, mask_student_number
from app.integrations.rate_limit import _masked_identifier

_STUDENT_NUMBER = "20250010001"
_PHONE = "+8613700137001"
_EMAIL = "user@pku.edu.cn"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20250010001", "20****01"),
        ("123456", "12****56"),  # spec §5.2 minimum length still affords the form
        ("1" * 20, "11****11"),  # fixed mask regardless of length
    ],
)
def test_mask_student_number_keeps_ends_and_a_fixed_mask(
    value: str, expected: str
) -> None:
    assert mask_student_number(value) == expected


@pytest.mark.parametrize("value", ["12345", "1234", "1", ""])
def test_mask_student_number_masks_short_values_fully(value: str) -> None:
    # First-2+last-2 would reveal most of a short value; below the §5.2
    # student-number minimum nothing is kept — only the masked shape.
    assert mask_student_number(value) == "****"


def test_masked_identifier_dispatch() -> None:
    # Phones and emails keep their established adapter forms; digit-only
    # identifiers (login usernames / registration student numbers) take the
    # student-number mask; anything else (non-digit usernames, user ids)
    # passes through exact.
    assert _masked_identifier(_PHONE) == mask_phone(_PHONE)
    assert _masked_identifier(_EMAIL) == mask_email(_EMAIL)
    assert _masked_identifier(_STUDENT_NUMBER) == mask_student_number(_STUDENT_NUMBER)
    assert _masked_identifier(_STUDENT_NUMBER) != _STUDENT_NUMBER
    assert _masked_identifier("alice") == "alice"


@pytest.mark.parametrize("identifier", [_PHONE, _EMAIL, _STUDENT_NUMBER])
def test_rate_limit_identifier_never_survives_masking_verbatim(
    identifier: str,
) -> None:
    # The property the 429 log line depends on: no contact or digit-only
    # identifier is emitted in full.
    assert identifier not in _masked_identifier(identifier)
