"""PENDING_APPROVAL students never act on the recruitment surfaces; the
public recruitment/project browse doors stay open to them."""

from uuid import uuid4

import pytest

from tests.integration.innovation.talent_support import account
from tests.integration.innovation.talent_support import (
    client as client,
)
from tests.integration.innovation.talent_support import (
    clock as clock,
)

pytestmark = pytest.mark.integration

APPROVAL_PENDING = "APPROVAL_PENDING"
PROJECT = uuid4()
RECRUITMENT = uuid4()


async def _pending(db, clock):
    return await account(db, clock, status="PENDING_APPROVAL")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "GET",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/recruitments",
            None,
        ),
        (
            "POST",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/recruitments",
            {"request_id": "r", "fields": {}},
        ),
        (
            "POST",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/recruitments/{RECRUITMENT}/publish",
            {"request_id": "r", "version": 1},
        ),
    ],
)
async def test_pending_student_cannot_touch_recruitment_surfaces(
    client, db_session, clock, method, path, body
):
    _, headers = await _pending(db_session, clock)
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == APPROVAL_PENDING


async def test_public_recruitment_and_project_lists_stay_browse_doors(
    client, db_session, clock
):
    _, headers = await _pending(db_session, clock)
    for path in ("/api/v1/ie/recruitments", "/api/v1/ie/projects"):
        listing = await client.get(path, headers=headers)
        assert listing.status_code == 200, (path, listing.text)
