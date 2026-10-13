"""Real HTTP campus query, immutable projection and explicit authorization."""

import importlib.util
from uuid import UUID

import pytest
from sqlalchemy import event, update

from app.modules.identity.models import User
from app.modules.innovation.talent_models import TalentFile, TalentProfile
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


async def test_material_filter_counts_only_current_public_files(
    client, db_session, clock, storage
):
    _, owner, first = await published(client, db_session, clock)
    _, _, second = await published(
        client,
        db_session,
        clock,
        {
            "links": [{"title": "模拟作品", "url": "https://example.invalid/work"}],
            "experience": "仅自述经历，不代表附件",
        },
    )
    private = await ready(client, owner, storage)
    # Even a saved selection is private until explicit publication.
    draft = await client.put(
        BASE,
        headers=owner,
        json={
            **command(first),
            "fields": {**first["fields"], "file_ids": [private["id"]]},
        },
    )
    assert draft.status_code == 200
    _, viewer = await account(db_session, clock)
    for query in [{}, {"has_public_materials": "false"}]:
        response = await client.get(PUBLIC, headers=viewer, params=query)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 2
        assert all(
            item["has_public_materials"] is False for item in response.json()["items"]
        )
    with_materials = await client.get(
        PUBLIC, headers=viewer, params={"has_public_materials": "true"}
    )
    assert with_materials.status_code == 200, with_materials.text
    assert with_materials.json()["total"] == 0
    row = await select_and_publish(client, owner, [private["id"]])
    another = await ready(
        client, owner, storage
    )  # unselected upload must not count twice
    assert another["id"] != private["id"]
    with_materials = await client.get(
        PUBLIC, headers=viewer, params={"has_public_materials": "true", "limit": 1}
    )
    assert with_materials.json()["total"] == 1
    assert with_materials.json()["items"][0]["id"] == first["id"]
    assert with_materials.json()["items"][0]["has_public_materials"] is True
    assert (
        len(
            (await client.get(PUBLIC + "/" + row["id"], headers=viewer)).json()["files"]
        )
        == 1
    )
    without = await client.get(
        PUBLIC, headers=viewer, params={"has_public_materials": "false"}
    )
    assert [item["id"] for item in without.json()["items"]] == [second["id"]]
    for query in [
        {"offset": 1},
        {"q": "does-not-match"},
        {"cooperation_state": "NOT_AVAILABLE"},
    ]:
        response = await client.get(
            PUBLIC, headers=viewer, params={"has_public_materials": "true", **query}
        )
        assert response.json()["items"] == []
        assert response.json()["total"] == (1 if "offset" in query else 0)
    # A newer public snapshot without files excludes historical and private uploads.
    row = await select_and_publish(client, owner, [])
    response = await client.get(
        PUBLIC, headers=viewer, params={"has_public_materials": "true"}
    )
    assert response.json()["total"] == 0
    assert (await client.get(PUBLIC + "/" + row["id"], headers=viewer)).json()[
        "files"
    ] == []


async def test_revoke_last_material_changes_flag_and_filter_immediately(
    client, db_session, clock, storage
):
    _, owner, _, _ = await saved(client, db_session, clock)
    files = [await ready(client, owner, storage) for _ in range(2)]
    row = await select_and_publish(client, owner, [f["id"] for f in files])
    _, viewer = await account(db_session, clock)
    for index, file in enumerate(files):
        revoked = await client.post(
            f"{BASE}/files/{file['id']}/revoke", headers=owner, json=command(row)
        )
        assert revoked.status_code == 200, revoked.text
        row = revoked.json()["current"]
        expected = index == 0
        response = await client.get(PUBLIC, headers=viewer)
        assert response.json()["items"][0]["has_public_materials"] is expected
        filtered = await client.get(
            PUBLIC,
            headers=viewer,
            params={"has_public_materials": str(expected).lower()},
        )
        assert filtered.json()["total"] == 1
        detail = await client.get(PUBLIC + "/" + row["id"], headers=viewer)
        assert bool(detail.json()["files"]) is expected


async def test_material_filter_has_stable_pagination_without_per_profile_queries(
    client, db_session, db_engine, clock, storage
):
    ids = []
    for _ in range(3):
        _, owner, _, _ = await saved(client, db_session, clock)
        file = await ready(client, owner, storage)
        row = await select_and_publish(client, owner, [file["id"]])
        ids.append(row["id"])
    await published(client, db_session, clock)  # no materials must not affect total
    _, viewer = await account(db_session, clock)
    selects = []

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(1)

    event.listen(db_engine.sync_engine, "before_cursor_execute", count_selects)
    try:
        found = []
        counts = []
        for offset in range(3):
            selects.clear()
            response = await client.get(
                PUBLIC,
                headers=viewer,
                params={
                    "has_public_materials": "true",
                    "limit": 1,
                    "offset": offset,
                    "cooperation_state": "LOOKING",
                    "q": "模拟新生",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 3
            found.append(response.json()["items"][0]["id"])
            counts.append(len(selects))
        assert found == sorted(ids)
        selects.clear()
        full = await client.get(
            PUBLIC, headers=viewer, params={"has_public_materials": "true", "limit": 50}
        )
        assert len(full.json()["items"]) == 3
        assert counts == [len(selects)] * 3  # list size cannot add DB round trips
        assert len(selects) > 0
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", count_selects)


@pytest.mark.parametrize("unavailable", ["revoked_at", "removed_at"])
async def test_unreadable_current_materials_do_not_set_public_flag(
    client, db_session, clock, storage, unavailable
):
    _, owner, _, _ = await saved(client, db_session, clock)
    file = await ready(client, owner, storage)
    row = await select_and_publish(client, owner, [file["id"]])
    value = clock.now()
    await db_session.execute(
        update(TalentFile)
        .where(TalentFile.id == UUID(file["id"]))
        .values(**{unavailable: value})
    )
    await db_session.commit()
    _, viewer = await account(db_session, clock)
    listing = await client.get(
        PUBLIC, headers=viewer, params={"has_public_materials": "false"}
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["has_public_materials"] is False
    assert (await client.get(PUBLIC + "/" + row["id"], headers=viewer)).json()[
        "files"
    ] == []


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


async def test_unselected_file_and_nonexistent_profile_have_neutral_denial(
    client, db_session, clock, storage
):
    from uuid import uuid4

    _, owner, _, _ = await saved(client, db_session, clock)
    selected = await ready(client, owner, storage)
    private = await ready(client, owner, storage)
    row = await select_and_publish(client, owner, [selected["id"]])
    _, viewer = await account(db_session, clock)
    await db_session.commit()
    denied = await client.get(
        f"{PUBLIC}/{row['id']}/files/{private['id']}/content",
        headers=viewer,
        params={"expected_public_version": row["public_version"]},
    )
    absent = await client.post(
        f"{PUBLIC}/{uuid4()}/contacts",
        headers=viewer,
        json={"expected_public_version": 1},
    )
    assert denied.status_code == absent.status_code == 404
    assert "object_key" not in denied.text + absent.text


@pytest.mark.parametrize("change", ["missing", "size", "mime", "digest"])
async def test_public_download_denies_changed_or_unavailable_checked_bytes(
    client, db_session, clock, storage, change
):
    from dataclasses import replace

    _, owner, _, _ = await saved(client, db_session, clock)
    file = await ready(client, owner, storage)
    row = await select_and_publish(client, owner, [file["id"]])
    _, viewer = await account(db_session, clock)
    key = storage.upload_urls[-1].object_key
    if change == "missing":
        storage.fail_with(OSError("storage unavailable"))
    elif change == "mime":
        storage.objects[key] = replace(storage.objects[key], content_type="image/png")
    else:
        original = file_support.PDF
        storage.put_object(
            object_key=key,
            content=original + b"changed" if change == "size" else b"X" * len(original),
        )
    response = await client.get(
        f"{PUBLIC}/{row['id']}/files/{file['id']}/content",
        headers=viewer,
        params={"expected_public_version": row["public_version"]},
    )
    assert response.status_code == 409, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-disposition" not in response.headers
    assert file_support.PDF not in response.content


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
