"""PENDING_APPROVAL students never act on the project-library surfaces.

Batch 2 translates the owner surfaces to require_approved_student_actor
(the batch-1 convention): a not-yet-approved student may browse the
campus shell but has no draft, owner-profile, qualification, or
operations surface. These tests pin the translated guard on every
entry point the batch mounts.
"""

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


async def _pending(db, clock):
    return await account(db, clock, status="PENDING_APPROVAL")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/ie/me/project-drafts", None),
        ("POST", "/api/v1/ie/me/project-drafts", {"request_id": "r", "title": "x"}),
        ("GET", "/api/v1/ie/me/owner-profile", None),
        ("PUT", "/api/v1/ie/me/owner-profile", {"request_id": "r", "version": 0}),
        ("GET", "/api/v1/ie/me/owner-qualification", None),
        ("POST", "/api/v1/ie/me/owner-qualification", {"request_id": "r"}),
        ("GET", "/api/v1/ie/me/capabilities", None),
    ],
)
async def test_pending_student_cannot_touch_owner_surfaces(
    client, db_session, clock, method, path, body
):
    _, headers = await _pending(db_session, clock)
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == APPROVAL_PENDING


async def test_admin_surfaces_reject_pending_students(client, db_session, clock):
    _, headers = await _pending(db_session, clock)
    listing = await client.get("/api/v1/admin/ie/owner-qualifications", headers=headers)
    assert listing.status_code in (403, 404), listing.text
    assert listing.json()["error"]["code"] in {APPROVAL_PENDING, "PERMISSION_DENIED"}
