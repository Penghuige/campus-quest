"""PENDING_APPROVAL students browse the talent surfaces but never act.

The upstream branch was written before the approval workflow landed;
its guards would have let a not-yet-approved student publish a public
profile. Batch 1 translates the action guards to
require_approved_student_actor while the browse side stays
require_active_actor (V1.1 spec 2026-10-16 §2: reading surfaces open,
acting surfaces frozen). These tests pin the translated semantics.
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

TAGS = "/api/v1/ie/talent-tags"
LIST = "/api/v1/ie/talents"


async def _pending(db, clock):
    return await account(db, clock, status="PENDING_APPROVAL")


async def test_pending_student_reads_tags_and_list(client, db_session, clock):
    _, headers = await _pending(db_session, clock)
    tags = await client.get(TAGS, headers=headers)
    listing = await client.get(LIST, headers=headers)
    assert tags.status_code == 200, tags.text
    assert listing.status_code == 200, listing.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/api/v1/ie/me/talent-profile", None),
        (
            "POST",
            "/api/v1/ie/me/talent-profile/publish",
            {"request_id": "r", "version": 1},
        ),
        (
            "POST",
            "/api/v1/ie/me/talent-profile/hide",
            {"request_id": "r", "version": 1},
        ),
        (
            "POST",
            "/api/v1/ie/me/talent-profile/revoke-contact",
            {"request_id": "r", "version": 1, "channel": "WECHAT"},
        ),
        (
            "POST",
            "/api/v1/ie/me/talent-profile/files",
            {"size": 1, "content_type": "image/png", "name": "x.png"},
        ),
        (
            "POST",
            "/api/v1/ie/me/talent-tag-suggestions",
            {"request_id": "r", "name": "新标签"},
        ),
    ],
)
async def test_pending_student_cannot_act(
    client, db_session, clock, method, path, body
):
    _, headers = await _pending(db_session, clock)
    response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "APPROVAL_PENDING"


async def test_pending_owner_stays_out_of_the_public_directory(
    client, db_session, clock
):
    # A visible revision forced in behind the transport layer still does
    # not surface: the public query requires an ACTIVE owner, and the
    # acting guards mean a PENDING_APPROVAL owner can never publish one
    # through the API in the first place.
    from app.modules.innovation.talent_models import (
        TalentProfile,
        TalentRevision,
    )
    from tests.integration.innovation.talent_support import saved

    owner, _, row, payload = await saved(client, db_session, clock)
    owner.status = "PENDING_APPROVAL"
    revision = TalentRevision(
        profile_id=row["id"],
        number=1,
        content={
            "name": payload["fields"]["name"],
            "faculty": payload["fields"]["faculty"],
            "grade": payload["fields"]["grade"],
            "introduction": payload["fields"]["introduction"],
            "advantages": payload["fields"]["advantages"],
            "cooperation_state": "LOOKING",
            "tag_names": [],
        },
        contacts=[],
        created_at=clock.now(),
    )
    db_session.add(revision)
    await db_session.flush()
    profile = await db_session.get(TalentProfile, row["id"])
    profile.visibility = "VISIBLE"
    profile.public_revision_id = revision.id
    profile.public_version = 1
    profile.public_updated_at = clock.now()
    await db_session.flush()

    listing = await client.get(LIST, headers=(await account(db_session, clock))[1])
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 0
