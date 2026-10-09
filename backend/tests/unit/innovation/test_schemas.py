"""Private draft transport boundaries, from the implementation plan."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.innovation.schemas import (
    ProjectDraftCreate,
    ProjectDraftResponse,
    ProjectDraftUpdate,
)


@pytest.mark.parametrize(
    ("patch", "valid"),
    [
        ({"title": "  新想法  "}, True),
        ({"title": "   "}, False),
        ({"title": "名" * 121}, False),
        ({"summary": "简" * 2001}, False),
        ({"direction": "方" * 121}, False),
        ({"stage": "阶" * 81}, False),
        ({"team_status": "队" * 1001}, False),
        ({"owner_user_id": str(uuid4())}, False),
        ({"status": "PUBLISHED"}, False),
    ],
)
def test_draft_content_validation(patch: dict, valid: bool) -> None:
    payload = {"request_id": str(uuid4()), "title": "项目", **patch}
    if not valid:
        with pytest.raises(ValidationError):
            ProjectDraftCreate.model_validate(payload)
        return
    draft = ProjectDraftCreate.model_validate(payload)
    assert draft.title == "新想法"
    assert draft.summary == draft.direction == draft.stage == draft.team_status == ""


def test_update_requires_version_and_normalizes_all_fields() -> None:
    payload = {
        "title": " 标题 ",
        "summary": " 简介 ",
        "direction": " 方向 ",
        "stage": " 起步 ",
        "team_status": " 一人 ",
    }
    for version in (None, 0, -1, True):
        with pytest.raises(ValidationError):
            ProjectDraftUpdate.model_validate({**payload, "version": version})
    draft = ProjectDraftUpdate.model_validate({**payload, "version": 1})
    assert draft.model_dump() == {
        "title": "标题",
        "summary": "简介",
        "direction": "方向",
        "stage": "起步",
        "team_status": "一人",
        "version": 1,
    }


def test_private_response_field_whitelist() -> None:
    assert set(ProjectDraftResponse.model_fields) == {
        "id",
        "title",
        "summary",
        "direction",
        "stage",
        "team_status",
        "version",
        "created_at",
        "updated_at",
    }
