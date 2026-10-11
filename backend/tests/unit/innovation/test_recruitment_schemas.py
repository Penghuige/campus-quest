"""Publication validation is stronger than private draft validation."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tests.integration.innovation.recruitment_support import fields


def schemas():
    name = "app.modules.innovation.recruitment_schemas"
    assert importlib.util.find_spec(name) is not None, (
        "R3 recruitment schemas are missing"
    )
    return importlib.import_module(name)


def test_incomplete_draft_saves_but_does_not_publish():
    model = schemas().RecruitmentFields()
    assert model.contacts == []
    with pytest.raises(ValueError):
        model.validate_publication(datetime.now(UTC))


@pytest.mark.parametrize("headcount", [1, 999])
def test_valid_publication(headcount):
    model = schemas().RecruitmentFields(**(fields() | {"headcount": headcount}))
    model.validate_publication(datetime.now(UTC))


@pytest.mark.parametrize(
    "patch",
    [
        {"headcount": 0},
        {"headcount": 1000},
        {"headcount": True},
        {"beginner_friendly": "true"},
        {"owner_id": "not allowed"},
        {"deadline_at": "2027-01-01T00:00:00"},
        {"contacts": [{"channel": "EMAIL", "value": "javascript:alert(1)"}]},
        {"contacts": [{"channel": "PHONE", "value": "abc"}]},
        {"contacts": [{"channel": "WECHAT", "value": "https://example.com"}]},
        {"contacts": [{"channel": "QQ", "value": "12345"}] * 2},
    ],
)
def test_reject_invalid_transport(patch):
    with pytest.raises(ValidationError):
        schemas().RecruitmentFields(**(fields() | patch))


@pytest.mark.parametrize(
    "patch",
    [
        {"contacts": []},
        {"goal": ""},
        {"initial_plan": ""},
        {"idea_stage": None},
        {"beginner_friendly": None},
        {"duties": ""},
    ],
)
def test_reject_incomplete_publication(patch):
    model = schemas().RecruitmentFields(**(fields() | patch))
    with pytest.raises(ValueError):
        model.validate_publication(datetime.now(UTC))


def test_deadline_boundary():
    now = datetime.now(UTC)
    model = schemas().RecruitmentFields(
        **(fields() | {"deadline_at": now + timedelta(milliseconds=1)})
    )
    model.validate_publication(now)
    with pytest.raises(ValueError):
        model.validate_publication(now + timedelta(milliseconds=1))


def test_deadline_normalizes_offset_to_utc():
    model = schemas().RecruitmentFields(
        **(fields() | {"deadline_at": "2027-01-01T08:00:00+08:00"})
    )
    assert model.deadline_at.isoformat() == "2027-01-01T00:00:00+00:00"
