"""Talent transport limits are independent of publication completeness."""

import importlib
from uuid import uuid4

import pytest
from pydantic import ValidationError


def schemas():
    name = "app.modules.innovation.talent_schemas"
    assert importlib.util.find_spec(name) is not None, "Talent schema not implemented"
    return importlib.import_module(name)


def minimal():
    return {
        "name": "模拟新生",
        "faculty": "模拟学院",
        "grade": "2026",
        "introduction": "模拟自述",
        "advantages": "制作PPT",
        "tag_ids": [uuid4()],
        "cooperation_state": "LOOKING",
    }


def test_empty_draft_is_valid_but_publication_requires_basic_fields():
    row = schemas().TalentFields()
    assert row.contacts == [] and row.file_ids == []
    with pytest.raises(ValueError):
        row.validate_publication()
    complete = schemas().TalentFields(**minimal())
    complete.validate_publication()
    assert complete.experience == "" and complete.weekly_hours is None


@pytest.mark.parametrize(
    "field,limit",
    [
        ("name", 50),
        ("faculty", 100),
        ("grade", 30),
        ("introduction", 1000),
        ("advantages", 2000),
        ("skill_notes", 1000),
        ("cooperation_direction", 500),
        ("desired_roles", 500),
        ("experience", 3000),
    ],
)
def test_unicode_text_limit(field, limit):
    model = schemas().TalentFields(**{**minimal(), field: "😀" * limit})
    assert len(getattr(model, field)) == limit
    with pytest.raises(ValidationError):
        schemas().TalentFields(**{**minimal(), field: "😀" * (limit + 1)})


@pytest.mark.parametrize(
    "patch",
    [
        {"weekly_hours": -1},
        {"weekly_hours": 169},
        {"weekly_hours": True},
        {"weekly_hours": 1.5},
        {"tag_ids": [uuid4()] * 2},
        {"tag_ids": [uuid4() for _ in range(11)]},
        {"file_ids": [uuid4() for _ in range(6)]},
        {"file_ids": [uuid4()] * 2},
        {"contacts": [{"channel": "WECHAT", "value": "demo_test"}] * 2},
        {"links": [{"title": "x", "url": "javascript:alert(1)"}]},
        {"links": [{"title": "x", "url": "https://"}]},
        {"links": [{"title": "x", "url": "https://example.com"}] * 6},
        {"student_no": "12345"},
        {"owner_user_id": str(uuid4())},
    ],
)
def test_invalid_fields_do_not_enter_drafts(patch):
    with pytest.raises(ValidationError):
        schemas().TalentFields(**{**minimal(), **patch})


@pytest.mark.parametrize("value", [0, 168, None])
def test_zero_hours_is_not_missing(value):
    assert schemas().TalentFields(weekly_hours=value).weekly_hours == value


def test_contact_normalization_uses_existing_channel_contract():
    contact = schemas().TalentContact(channel="EMAIL", value="  DEMO@EXAMPLE.COM ")
    assert contact.value == "demo@example.com"
    with pytest.raises(ValidationError):
        schemas().TalentContact(channel="QQ", value="012345")
    with pytest.raises(ValidationError):
        schemas().TalentSaveCommand(request_id=uuid4(), version=True, fields={})
