from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.innovation.achievement_schemas import (
    AchievementDraftCreate,
    AchievementDraftUpdate,
)


def test_partial_draft_and_trim() -> None:
    item = AchievementDraftCreate(request_id=uuid4(), title="  第一版原型  ")
    assert item.title == "第一版原型"
    assert item.description == item.work_url == item.award_text == ""


@pytest.mark.parametrize(
    "field,limit", [("title", 120), ("description", 4000), ("award_text", 1000)]
)
def test_unicode_limits(field, limit):
    fields = {"title": "成果", field: "🌱" * limit}
    AchievementDraftCreate(request_id=uuid4(), **fields)
    with pytest.raises(ValidationError):
        AchievementDraftCreate(
            request_id=uuid4(), **{**fields, field: "🌱" * (limit + 1)}
        )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https:example.com",
        "http:///example.com",
        "//example.com",
        "ftp://example.com",
        "https://user:password@example.com",
        "https://",
        "https://example.com/\nsecret",
        "https://example.com/" + "a" * 2000,
    ],
)
def test_invalid_work_links(url):
    with pytest.raises(ValidationError):
        AchievementDraftCreate(request_id=uuid4(), title="成果", work_url=url)


@pytest.mark.parametrize(
    "url", ["", " https://example.com/作品?q=原型#进展 ", "http://example.com/work"]
)
def test_optional_valid_links_preserve_text(url):
    assert (
        AchievementDraftCreate(request_id=uuid4(), title="成果", work_url=url).work_url
        == url.strip()
    )


@pytest.mark.parametrize(
    "change",
    [
        {"title": " "},
        {"owner_id": str(uuid4())},
        {"approved": True},
        {"version": True},
        {"version": "1"},
        {"version": 0},
    ],
)
def test_update_contract(change):
    with pytest.raises(ValidationError):
        AchievementDraftUpdate.model_validate(
            {
                "title": "成果",
                "description": "",
                "work_url": "",
                "award_text": "",
                "version": 1,
                **change,
            }
        )


@pytest.mark.parametrize("missing", ["description", "work_url", "award_text"])
def test_update_requires_explicit_content_to_avoid_silent_erasure(missing):
    fields = {
        "title": "成果",
        "description": "",
        "work_url": "",
        "award_text": "",
        "version": 1,
    }
    del fields[missing]
    with pytest.raises(ValidationError):
        AchievementDraftUpdate.model_validate(fields)
