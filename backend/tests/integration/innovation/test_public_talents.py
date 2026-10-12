"""Real HTTP campus query, immutable projection and explicit authorization."""

import importlib.util
from uuid import UUID

import pytest
from sqlalchemy import update

from app.modules.identity.models import User
from app.modules.innovation.talent_models import TalentProfile
from tests.integration.innovation import test_talent_files as file_support
from tests.integration.innovation.talent_support import BASE, command, saved
from tests.integration.innovation.test_achievement_review_operations import account
from tests.integration.innovation.test_talent_files import clock as clock
from tests.integration.innovation.test_talent_files import ready, select_and_publish
from tests.integration.innovation.test_talent_files import scanner as scanner
from tests.integration.innovation.test_talent_files import storage as storage

pytestmark = pytest.mark.integration
file_client = file_support.client
PUBLIC = "/api/v1/ie/talents"


@pytest.fixture
async def client(file_client, clock, storage):
    if importlib.util.find_spec("app.modules.innovation.public_talent_router"):
        from app.modules.innovation.public_talent_router import (
            get_public_talent_service,
        )
        from app.modules.innovation.public_talent_service import PublicTalentService

        file_client._transport.app.dependency_overrides[get_public_talent_service] = (
            lambda: PublicTalentService(clock=clock, storage=storage)
        )
    yield file_client


async def published(client, db, clock, patch=None):
    owner, headers, row, _ = await saved(client, db, clock, patch=patch)
    response = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert response.status_code == 200
    return owner, headers, response.json()["current"]


async def test_hidden_talent_is_absent_from_search_and_direct_links(
    client, db_session, clock
):
    _, _, row, _ = await saved(client, db_session, clock)
    _, viewer = await account(db_session, clock)
    response = await client.get(PUBLIC, headers=viewer)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert (
        await client.get(PUBLIC + "/" + row["id"], headers=viewer)
    ).status_code == 404


async def test_pagination_ties_are_stable_and_tag_filters_are_bounded(
    client, db_session, clock
):
    _, _, first = await published(client, db_session, clock)
    _, _, second = await published(client, db_session, clock)
    _, viewer = await account(db_session, clock)
    expected = sorted([first["id"], second["id"]])
    ids = []
    for offset in range(2):
        result = await client.get(
            PUBLIC, headers=viewer, params={"limit": 1, "offset": offset}
        )
        assert result.status_code == 200 and result.json()["total"] == 2
        ids.append(result.json()["items"][0]["id"])
    assert ids == expected
    tag = first["fields"]["tag_ids"][0]
    repeated = await client.get(PUBLIC, headers=viewer, params=[("tag_ids", tag)] * 6)
    assert repeated.status_code == 200 and repeated.json()["total"] == 2
    tags = (await client.get("/api/v1/ie/talent-tags", headers=viewer)).json()["items"]
    assert (
        await client.get(
            PUBLIC, headers=viewer, params=[("tag_ids", t["id"]) for t in tags[:6]]
        )
    ).status_code == 422


async def test_public_projection_excludes_private_sentinels(client, db_session, clock):
    owner, headers, row = await published(
        client,
        db_session,
        clock,
        {
            "contacts": [{"channel": "WECHAT", "value": "private_contact"}],
            "experience": "公开模拟经历",
        },
    )
    await client.put(
        BASE,
        headers=headers,
        json={
            **command(row),
            "fields": {**row["fields"], "introduction": "private_draft_sentinel"},
        },
    )
    _, viewer = await account(db_session, clock)
    for path in [PUBLIC, PUBLIC + "/" + row["id"]]:
        response = await client.get(path, headers=viewer)
        assert response.status_code == 200, response.text
        for value in [
            "private_contact",
            "private_draft_sentinel",
            "student_no",
            "user_id",
            "object_key",
            "upload_url",
            str(owner.id),
            "scan_attempt_token",
        ]:
            assert value not in response.text
        assert response.headers["cache-control"] == "private, no-store"
    assert "公开模拟经历" not in (await client.get(PUBLIC, headers=viewer)).text
    assert (
        await client.get(PUBLIC, headers=viewer, params={"q": "private_contact"})
    ).json()["total"] == 0


async def test_literal_search_all_tags_and_cooperation_filters(
    client, db_session, clock
):
    _, headers, row = await published(
        client,
        db_session,
        clock,
        {"name": "模拟_%新人", "cooperation_state": "NOT_AVAILABLE"},
    )
    tags = (await client.get("/api/v1/ie/talent-tags", headers=headers)).json()["items"]
    tag = row["fields"]["tag_ids"][0]
    other = next(t["id"] for t in tags if t["id"] != tag)
    _, viewer = await account(db_session, clock)
    for params in [
        {"q": "_%"},
        {"tag_ids": tag},
        {"cooperation_state": "NOT_AVAILABLE"},
    ]:
        assert (await client.get(PUBLIC, headers=viewer, params=params)).json()[
            "total"
        ] == 1
    for params in [
        {"q": "absent"},
        [("tag_ids", tag), ("tag_ids", other)],
        {"cooperation_state": "LOOKING"},
    ]:
        assert (await client.get(PUBLIC, headers=viewer, params=params)).json()[
            "total"
        ] == 0
    assert (
        await client.get(PUBLIC, headers=viewer, params={"limit": 1, "offset": 1})
    ).json()["items"] == []
    assert (
        await client.get(PUBLIC, headers=viewer, params={"q": "a" * 101})
    ).status_code == 422
    assert (
        await client.get(PUBLIC, headers=viewer, params={"limit": 51})
    ).status_code == 422


@pytest.mark.parametrize("restriction", ["owner", "governance", "viewer"])
async def test_current_identity_and_governance_apply_to_list_and_detail(
    client, db_session, clock, restriction
):
    owner, _, row = await published(client, db_session, clock)
    viewer_user, viewer = await account(db_session, clock)
    if restriction == "governance":
        await db_session.execute(
            update(TalentProfile)
            .where(TalentProfile.id == UUID(row["id"]))
            .values(moderation_state="TAKEN_DOWN")
        )
    else:
        await db_session.execute(
            update(User)
            .where(User.id == (owner.id if restriction == "owner" else viewer_user.id))
            .values(status="SUSPENDED")
        )
    await db_session.commit()
    listing = await client.get(PUBLIC, headers=viewer)
    detail = await client.get(PUBLIC + "/" + row["id"], headers=viewer)
    if restriction == "viewer":
        assert listing.status_code == detail.status_code == 403
    else:
        assert listing.status_code == 200 and listing.json()["total"] == 0
        assert detail.status_code == 404


async def test_contacts_and_files_require_current_version_and_close_stops_access(
    client, db_session, clock, storage
):
    _, headers, row = await published(
        client,
        db_session,
        clock,
        {"contacts": [{"channel": "WECHAT", "value": "demo_contact"}]},
    )
    file = await ready(client, headers, storage)
    row = await select_and_publish(client, headers, [file["id"]])
    _, viewer = await account(db_session, clock)
    path = PUBLIC + "/" + row["id"]
    version = {"expected_public_version": row["public_version"]}
    detail = await client.get(path, headers=viewer)
    assert (
        detail.status_code == 200
        and detail.json()["files"][0]["name"] == "模拟作品.pdf"
    )
    assert "demo_contact" not in detail.text
    contacts = await client.post(path + "/contacts", headers=viewer, json=version)
    assert (
        contacts.status_code == 200
        and contacts.json()["contacts"][0]["value"] == "demo_contact"
    )
    download = await client.get(
        path + f"/files/{file['id']}/content", headers=viewer, params=version
    )
    assert (
        download.status_code == 200
        and download.headers["x-content-type-options"] == "nosniff"
    )
    updated = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert updated.status_code == 200
    assert (
        await client.post(path + "/contacts", headers=viewer, json=version)
    ).status_code == 409
    assert (
        await client.get(
            path + f"/files/{file['id']}/content", headers=viewer, params=version
        )
    ).status_code == 409
    await client.post(
        BASE + "/hide", headers=headers, json=command(updated.json()["current"])
    )
    assert (await client.get(path, headers=viewer)).status_code == 404
    assert (
        await client.post(path + "/contacts", headers=viewer, json=version)
    ).status_code == 404
    assert (
        await client.get(
            path + f"/files/{file['id']}/content", headers=viewer, params=version
        )
    ).status_code == 404
