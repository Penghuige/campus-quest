"""PENDING_APPROVAL students never act on the achievement surfaces; the
public achievement list stays open to them (the reading-surface rule).

Batch 3 adds the owner side (drafts, evidence, submissions) and the
operator review queue; every acting entry answers APPROVAL_PENDING for
a pending student, while /ie/achievements remains a browse door.
"""

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
ACHIEVEMENT = uuid4()


async def _pending(db, clock):
    return await account(db, clock, status="PENDING_APPROVAL")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "GET",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/achievements",
            None,
        ),
        (
            "POST",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/achievements",
            {"request_id": "r", "title": "x"},
        ),
        (
            "POST",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/achievements/{ACHIEVEMENT}/submit",
            {"request_id": "r"},
        ),
        (
            "POST",
            f"/api/v1/ie/me/project-drafts/{PROJECT}/achievements/{ACHIEVEMENT}/evidence",
            {"request_id": "r", "size": 1, "content_type": "image/png"},
        ),
        (
            "GET",
            "/api/v1/ie/ops/achievement-reviews",
            None,
        ),
    ],
)
async def test_pending_student_cannot_touch_achievement_surfaces(
    client, db_session, clock, method, path, body
):
    _, headers = await _pending(db_session, clock)
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] in {APPROVAL_PENDING, "PERMISSION_DENIED"}


async def test_the_public_achievement_list_stays_a_browse_door(
    client, db_session, clock
):
    _, headers = await _pending(db_session, clock)
    listing = await client.get("/api/v1/ie/achievements", headers=headers)
    assert listing.status_code == 200, listing.text
