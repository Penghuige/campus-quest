"""R4: consent, novice access, immutable public versions and command replay."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.modules.audit.context import AuditContext
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.models import OwnerQualification
from app.modules.innovation.talent_models import (
    TalentActionReceipt,
    TalentProfile,
    TalentRevision,
)
from app.modules.innovation.talent_schemas import TalentVersionCommand
from app.modules.innovation.talent_service import TalentService
from tests.integration.innovation.talent_support import (
    BASE,
    command,
    fields,
    saved,
)
from tests.integration.innovation.talent_support import (
    client as client,
)
from tests.integration.innovation.talent_support import (
    clock as clock,
)
from tests.integration.innovation.test_achievement_review_operations import account

pytestmark = pytest.mark.integration


async def test_owner_can_distinguish_governance_removal_from_display_switch(
    client, db_session, clock
):
    from sqlalchemy import update

    _, headers, row, _ = await saved(client, db_session, clock)
    await db_session.execute(
        update(TalentProfile)
        .where(TalentProfile.id == UUID(row["id"]))
        .values(moderation_state="TAKEN_DOWN")
    )
    response = await client.get(BASE, headers=headers)
    assert response.status_code == 200
    assert response.json()["profile"]["moderation_state"] == "TAKEN_DOWN"


async def test_new_student_can_publish_without_work_or_contact(
    client, db_session, clock
):
    user, headers = await account(db_session, clock)
    empty = await client.get(BASE, headers=headers)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"profile": None}
    assert empty.headers["cache-control"] == "private, no-store"
    tags = await client.get("/api/v1/ie/talent-tags", headers=headers)
    assert tags.status_code == 200, tags.text
    created = await client.put(
        BASE,
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "version": 0,
            "fields": fields(tags.json()["items"][0]["id"]),
        },
    )
    assert created.status_code == 200, created.text
    row = created.json()["current"]
    assert row["visibility"] == "HIDDEN" and row["public_version"] == 0
    assert row["fields"]["contacts"] == []
    assert row["fields"]["file_ids"] == []
    assert row["fields"]["experience"] == ""
    published = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert published.status_code == 200, published.text
    current = published.json()["current"]
    assert current["visibility"] == "VISIBLE"
    assert current["public_version"] == 1
    assert current["public_contact_channels"] == []
    assert current["public_file_ids"] == []
    assert not current["has_unpublished_changes"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(OwnerQualification)
            .where(OwnerQualification.user_id == user.id)
        )
        == 0
    )


async def test_saved_changes_remain_private_and_replay_cannot_reopen(
    client, db_session, clock
):
    _, headers, row, create_payload = await saved(client, db_session, clock)
    create_replay = await client.put(BASE, headers=headers, json=create_payload)
    assert create_replay.status_code == 200
    assert create_replay.json()["replayed"]
    publish = command(row)
    result = await client.post(BASE + "/publish", headers=headers, json=publish)
    assert result.status_code == 200, result.text
    row = result.json()["current"]
    edited = await client.put(
        BASE,
        headers=headers,
        json={
            **command(row),
            "fields": {**row["fields"], "advantages": "PRIVATE_NEW_SENTINEL"},
        },
    )
    assert edited.status_code == 200, edited.text
    row = edited.json()["current"]
    assert row["public_version"] == 1 and row["has_unpublished_changes"]
    closed = await client.post(BASE + "/hide", headers=headers, json=command(row))
    assert closed.status_code == 200, closed.text
    closed_row = closed.json()["current"]
    replay = await client.post(BASE + "/publish", headers=headers, json=publish)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"]
    assert replay.json()["applied_version"] == result.json()["applied_version"]
    assert replay.json()["current"]["visibility"] == "HIDDEN"
    assert replay.json()["current"]["version"] == closed_row["version"]


async def test_last_contact_can_be_revoked_without_hiding_talent(
    client, db_session, clock
):
    _, headers, row, _ = await saved(
        client,
        db_session,
        clock,
        patch={
            "contacts": [{"channel": "WECHAT", "value": "demo_talent"}],
            "cooperation_state": "NOT_AVAILABLE",
        },
    )
    response = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert response.status_code == 200, response.text
    row = response.json()["current"]
    revoked = await client.post(
        BASE + "/revoke-contact",
        headers=headers,
        json={
            **command(row),
            "channel": "WECHAT",
        },
    )
    assert revoked.status_code == 200, revoked.text
    row = revoked.json()["current"]
    assert row["visibility"] == "VISIBLE"
    assert row["public_contact_channels"] == []
    assert row["fields"]["contacts"] == []
    assert row["fields"]["cooperation_state"] == "NOT_AVAILABLE"


async def test_save_conflict_and_identity_limits_preserve_owner_data(
    client, db_session, clock
):
    _, headers, row, payload = await saved(client, db_session, clock)
    _, other_headers = await account(db_session, clock)
    assert (await client.get(BASE, headers=other_headers)).json() == {"profile": None}
    changed_key = await client.put(
        BASE,
        headers=headers,
        json={
            **payload,
            "fields": {**payload["fields"], "name": "OTHER"},
        },
    )
    assert changed_key.status_code == 409
    stale = await client.put(
        BASE,
        headers=headers,
        json={
            **command(row),
            "version": row["version"] + 1,
            "fields": row["fields"],
        },
    )
    assert stale.status_code == 409
    assert (await client.get(BASE, headers=headers)).json()["profile"]["fields"][
        "name"
    ] == "模拟新生"
    _, teacher_headers = await account(db_session, clock, role="TEACHER")
    forbidden = await client.get(BASE, headers=teacher_headers)
    assert forbidden.status_code == 403
    assert forbidden.headers["cache-control"] == "private, no-store"


async def test_invalid_tag_and_incomplete_draft_cannot_publish(
    client, db_session, clock
):
    _, headers, row, _ = await saved(client, db_session, clock)
    unknown = await client.put(
        BASE,
        headers=headers,
        json={
            **command(row),
            "fields": {**row["fields"], "tag_ids": [str(uuid4())]},
        },
    )
    assert unknown.status_code == 409
    incomplete = await client.put(
        BASE,
        headers=headers,
        json={
            **command(row),
            "fields": {**row["fields"], "advantages": ""},
        },
    )
    assert incomplete.status_code == 200, incomplete.text
    denied = await client.post(
        BASE + "/publish", headers=headers, json=command(incomplete.json()["current"])
    )
    assert denied.status_code == 409


async def test_tag_suggestions_are_private_deduplicated_requests(
    client, db_session, clock
):
    _, headers = await account(db_session, clock)
    base = "/api/v1/ie/me/talent-tag-suggestions"
    payload = {"request_id": str(uuid4()), "name": "模拟技能", "reason": "模拟理由"}
    response = await client.post(base, headers=headers, json=payload)
    assert response.status_code == 200, response.text
    duplicate = await client.post(
        base,
        headers=headers,
        json={**payload, "request_id": str(uuid4()), "name": " 模拟技能 "},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == response.json()["id"]
    replay = await client.post(base, headers=headers, json=payload)
    assert replay.status_code == 200 and replay.json()["replayed"]
    changed = await client.post(
        base, headers=headers, json={**payload, "reason": "changed"}
    )
    assert changed.status_code == 409
    tags = (await client.get("/api/v1/ie/talent-tags", headers=headers)).json()["items"]
    assert all(tag["name"] != "模拟技能" for tag in tags)


async def test_moderation_cannot_be_cleared_by_saving_or_publishing(
    client, db_session, clock
):
    _, headers, row, _ = await saved(client, db_session, clock)
    profile_id = UUID(row["id"])
    await db_session.execute(
        update(TalentProfile)
        .where(TalentProfile.id == profile_id)
        .values(moderation_state="TAKEN_DOWN")
    )
    await db_session.commit()
    edited = await client.put(
        BASE,
        headers=headers,
        json={**command(row), "fields": {**row["fields"], "name": "仍为模拟资料"}},
    )
    assert edited.status_code == 200
    denied = await client.post(
        BASE + "/publish", headers=headers, json=command(edited.json()["current"])
    )
    assert denied.status_code == 409
    stored = await db_session.get(TalentProfile, profile_id, populate_existing=True)
    assert stored.moderation_state == "TAKEN_DOWN"
    assert stored.visibility == "HIDDEN" and stored.public_revision_id is None


async def test_failed_audit_rolls_back_publication(client, db_session, clock):
    user, _, row, _ = await saved(client, db_session, clock)
    actor = Actor(user.id, Role.STUDENT)
    profile_id = UUID(row["id"])
    request_id = uuid4()

    class UnavailableAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await TalentService(clock=clock, audit=UnavailableAudit()).publish(
            db_session,
            actor=actor,
            payload=TalentVersionCommand(request_id=request_id, version=row["version"]),
            context=AuditContext(None, None),
        )
    stored = await db_session.get(TalentProfile, profile_id, populate_existing=True)
    assert stored.visibility == "HIDDEN" and stored.version == row["version"]
    assert stored.public_revision_id is None and stored.public_version == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TalentRevision)
            .where(TalentRevision.profile_id == profile_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TalentActionReceipt)
            .where(TalentActionReceipt.request_id == request_id)
        )
        == 0
    )


@pytest.mark.parametrize("table", ["ie_talent_revisions", "ie_talent_receipts"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_public_history_is_immutable_in_postgres(
    client, db_session, clock, table, operation
):
    _, headers, row, _ = await saved(client, db_session, clock)
    published = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert published.status_code == 200
    sql = (
        f"UPDATE {table} SET created_at=created_at WHERE profile_id=:id"
        if operation == "UPDATE"
        else f"DELETE FROM {table} WHERE profile_id=:id"
    )
    with pytest.raises(DBAPIError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(text(sql), {"id": UUID(row["id"])})


async def test_public_pointer_cannot_use_another_students_revision(
    client, db_session, clock
):
    _, headers, row, _ = await saved(client, db_session, clock)
    published = await client.post(BASE + "/publish", headers=headers, json=command(row))
    assert published.status_code == 200
    revision_id = await db_session.scalar(
        select(TalentProfile.public_revision_id).where(
            TalentProfile.id == UUID(row["id"])
        )
    )
    _, _, other, _ = await saved(client, db_session, clock)
    with pytest.raises(DBAPIError, match="fk_ie_talent_public"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(TalentProfile)
                .where(TalentProfile.id == UUID(other["id"]))
                .values(public_revision_id=revision_id)
            )


async def test_tag_suggestion_normalization_handles_expanding_unicode(
    client, db_session, clock
):
    _, headers = await account(db_session, clock)
    payload = {"request_id": str(uuid4()), "name": "ﷺ" * 50}
    response = await client.post(
        "/api/v1/ie/me/talent-tag-suggestions", headers=headers, json=payload
    )
    assert response.status_code == 200, response.text
    duplicate = await client.post(
        "/api/v1/ie/me/talent-tag-suggestions",
        headers=headers,
        json={**payload, "request_id": str(uuid4())},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == response.json()["id"]
