"""R3 permission, snapshot, explicit authorization and replay boundaries."""

from uuid import uuid4

import pytest

from tests.integration.innovation.recruitment_support import (
    client as client,
)
from tests.integration.innovation.recruitment_support import (
    clock as clock,
)
from tests.integration.innovation.recruitment_support import (
    command,
    created,
    fields,
    world,
)
from tests.integration.innovation.test_achievement_review_operations import account

pytestmark = pytest.mark.integration


async def test_publish_requires_qualification_and_owned_project(
    client, db_session, clock
):
    _, _, headers, base, row = await created(client, db_session, clock, qualified=False)
    denied = await client.post(base + "/publish", headers=headers, json=command(row))
    assert denied.status_code == 403
    _, outsider = await account(db_session, clock)
    assert (await client.get(base, headers=outsider)).status_code == 404


async def test_saved_changes_do_not_change_published_revision(
    client, db_session, clock
):
    _, _, headers, base, row = await created(client, db_session, clock)
    published = await client.post(base + "/publish", headers=headers, json=command(row))
    assert published.status_code == 200, published.text
    row = published.json()["current"]
    saved_fields = fields() | {"role_name": "PRIVATE_DRAFT_SENTINEL"}
    saved = await client.patch(
        base, headers=headers, json=command(row) | {"fields": saved_fields}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_unpublished_changes"] is True
    from sqlalchemy import select

    from app.modules.innovation.recruitment_models import RecruitmentRevision

    revision = await db_session.scalar(select(RecruitmentRevision))
    assert revision.content["role_name"] == "研究伙伴"
    assert "contacts" not in revision.content


async def test_close_reopen_and_replay_cannot_change_new_cycle(
    client, db_session, clock
):
    _, _, headers, base, row = await created(client, db_session, clock)
    row = (
        await client.post(base + "/publish", headers=headers, json=command(row))
    ).json()["current"]
    old_command = command(row)
    closed = (
        await client.post(base + "/close", headers=headers, json=old_command)
    ).json()
    opened = (
        await client.post(
            base + "/reopen", headers=headers, json=command(closed["current"])
        )
    ).json()
    replay = await client.post(base + "/close", headers=headers, json=old_command)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["applied_version"] == closed["applied_version"]
    assert replay.json()["current"]["status"] == "OPEN"
    assert replay.json()["current"]["cycle_id"] == opened["current"]["cycle_id"]
    changed = await client.post(
        base + "/close",
        headers=headers,
        json=old_command | {"version": opened["current"]["version"]},
    )
    assert changed.status_code == 409


async def test_revoke_last_contact_requires_explicit_close(client, db_session, clock):
    _, _, headers, base, row = await created(client, db_session, clock)
    row = (
        await client.post(base + "/publish", headers=headers, json=command(row))
    ).json()["current"]
    rejected = await client.post(
        base + "/revoke-contact",
        headers=headers,
        json=command(row) | {"channel": "WECHAT", "close_if_last": False},
    )
    assert rejected.status_code == 409
    revoked = await client.post(
        base + "/revoke-contact",
        headers=headers,
        json=command(row) | {"channel": "WECHAT", "close_if_last": True},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["current"]["status"] == "CLOSED"
    assert revoked.json()["current"]["fields"]["contacts"] == []
    assert (
        await client.post(
            base + "/reopen", headers=headers, json=command(revoked.json()["current"])
        )
    ).status_code == 422


async def test_create_and_update_unknown_result_retry(client, db_session, clock):
    _, _, headers, base = await world(db_session, clock)
    payload = {"request_id": str(uuid4()), "fields": fields()}
    first = await client.post(base, headers=headers, json=payload)
    assert first.status_code == 201, first.text
    replay = await client.post(base, headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert (
        await client.post(base, headers=headers, json=payload | {"fields": {}})
    ).status_code == 409
    path = base + "/" + first.json()["id"]
    update = command(first.json()) | {"fields": fields() | {"role_name": "另一个岗位"}}
    saved = await client.patch(path, headers=headers, json=update)
    again = await client.patch(path, headers=headers, json=update)
    assert saved.status_code == again.status_code == 200
    assert saved.json()["version"] == again.json()["version"]


async def test_revoked_contact_requires_fresh_authorization(client, db_session, clock):
    _, _, headers, base, row = await created(client, db_session, clock)
    row = (
        await client.post(base + "/publish", headers=headers, json=command(row))
    ).json()["current"]
    stale_edit = command(row) | {"fields": fields() | {"role_name": "未保存修改"}}
    revoked = (
        await client.post(
            base + "/revoke-contact",
            headers=headers,
            json=command(row) | {"channel": "WECHAT", "close_if_last": True},
        )
    ).json()["current"]
    assert (
        await client.patch(base, headers=headers, json=stale_edit)
    ).status_code == 409
    assert (
        await client.post(base + "/reopen", headers=headers, json=command(revoked))
    ).status_code == 422
    fresh = await client.patch(
        base, headers=headers, json=command(revoked) | {"fields": fields()}
    )
    assert fresh.status_code == 200
    assert fresh.json()["status"] == "CLOSED"
    assert fresh.json()["public_contact_channels"] == []
    opened = await client.post(
        base + "/reopen", headers=headers, json=command(fresh.json())
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["current"]["public_contact_channels"] == ["WECHAT"]


async def test_audit_failure_rolls_back_publication(client, db_session, clock):
    from sqlalchemy import func, select

    from app.modules.audit.context import AuditContext
    from app.modules.audit.service import AuditLogWriter
    from app.modules.identity.enums import Role
    from app.modules.identity.events import Actor
    from app.modules.innovation.recruitment_models import (
        RecruitmentActionReceipt,
        RecruitmentContactAuthorization,
        RecruitmentRevision,
    )
    from app.modules.innovation.recruitment_schemas import RecruitmentVersionCommand
    from app.modules.innovation.recruitment_service import RecruitmentService

    class UnavailableAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("controlled audit failure")

    owner, project, headers, base, row = await created(client, db_session, clock)
    owner_id, project_id = owner.id, project.id
    service = RecruitmentService(clock=clock, audit=UnavailableAudit())
    with pytest.raises(RuntimeError, match="controlled audit failure"):
        await service.publish(
            db_session,
            actor=Actor(user_id=owner_id, role=Role.STUDENT),
            project_id=project_id,
            recruitment_id=row["id"],
            payload=RecruitmentVersionCommand(**command(row)),
            context=AuditContext(None, None),
        )
    for model in (
        RecruitmentRevision,
        RecruitmentContactAuthorization,
        RecruitmentActionReceipt,
    ):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    current = await client.get(base, headers=headers)
    assert current.status_code == 200
    assert current.json()["status"] == "DRAFT"


async def test_takedown_cannot_be_restored_by_owner(client, db_session, clock):
    from uuid import UUID

    from sqlalchemy import update

    from app.modules.innovation.recruitment_models import RecruitmentDraft

    _, _, headers, base, row = await created(client, db_session, clock)
    row = (
        await client.post(base + "/publish", headers=headers, json=command(row))
    ).json()["current"]
    await db_session.execute(
        update(RecruitmentDraft)
        .where(RecruitmentDraft.id == UUID(row["id"]))
        .values(moderation_state="TAKEN_DOWN")
    )
    await db_session.commit()
    for action in ("publish", "confirm-active"):
        assert (
            await client.post(base + "/" + action, headers=headers, json=command(row))
        ).status_code == 409
    closed = (
        await client.post(base + "/close", headers=headers, json=command(row))
    ).json()["current"]
    assert (
        await client.post(base + "/reopen", headers=headers, json=command(closed))
    ).status_code == 409


async def test_database_enforces_private_content_bounds(client, db_session, clock):
    from uuid import UUID

    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from app.modules.innovation.recruitment_models import RecruitmentDraft

    _, _, _, _, row = await created(client, db_session, clock)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            update(RecruitmentDraft)
            .where(RecruitmentDraft.id == UUID(row["id"]))
            .values(fields=fields() | {"project_title": "x" * 121})
        )


async def test_database_rejects_public_history_mutation(client, db_session, clock):
    from sqlalchemy import select, update
    from sqlalchemy.exc import DBAPIError

    from app.modules.innovation.recruitment_models import (
        RecruitmentActionReceipt,
        RecruitmentContactAuthorization,
        RecruitmentRevision,
    )

    _, _, headers, base, row = await created(client, db_session, clock)
    response = await client.post(base + "/publish", headers=headers, json=command(row))
    assert response.status_code == 200
    mutations = [
        update(RecruitmentRevision).values(
            content=fields() | {"role_name": "mutation"}
        ),
        update(RecruitmentContactAuthorization).values(value="mutated"),
        update(RecruitmentActionReceipt).values(applied_version=999),
    ]
    for statement in mutations:
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    auth = await db_session.scalar(select(RecruitmentContactAuthorization))
    assert auth.value == "demo_partner"
