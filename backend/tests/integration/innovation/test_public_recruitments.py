"""Published whitelist, active publisher and immediate visibility boundaries."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from app.modules.identity.models import User
from app.modules.innovation.models import OwnerQualification, ProjectDraft
from app.modules.innovation.recruitment_models import RecruitmentDraft
from tests.integration.innovation.recruitment_support import command, created, fields
from tests.integration.innovation.talent_support import account
from tests.integration.innovation.talent_support import client as client
from tests.integration.innovation.talent_support import clock as clock

pytestmark = pytest.mark.integration
PUBLIC = "/api/v1/ie/recruitments"


async def published(client, db, clock):
    owner, project, headers, base, row = await created(client, db, clock)
    result = await client.post(base + "/publish", headers=headers, json=command(row))
    assert result.status_code == 200, result.text
    return owner, project, headers, base, result.json()["current"]


async def test_saved_drafts_never_enter_public_payload_or_search(
    client, db_session, clock
):
    _, project, headers, base, row = await published(client, db_session, clock)
    await db_session.execute(
        update(ProjectDraft)
        .where(ProjectDraft.id == project.id)
        .values(title="PRIVATE_PARENT_SENTINEL")
    )
    await db_session.commit()
    await client.patch(
        base,
        headers=headers,
        json=command(row)
        | {"fields": fields() | {"role_name": "PRIVATE_ROLE_SENTINEL"}},
    )
    _, viewer = await account(db_session, clock)
    listing = await client.get(PUBLIC, headers=viewer)
    assert listing.status_code == 200, listing.text
    assert listing.headers["Cache-Control"] == "private, no-store"
    assert listing.json()["total"] == 1
    item = listing.json()["items"][0]
    assert item["content"]["role_name"] == "研究伙伴"
    assert item["contact_channels"] == ["WECHAT"]
    detail = await client.get(PUBLIC + "/" + row["id"], headers=viewer)
    assert detail.status_code == 200
    for response in (listing, detail):
        for secret in (
            "PRIVATE_PARENT_SENTINEL",
            "PRIVATE_ROLE_SENTINEL",
            "demo_partner",
            "123456",
            "owner_user_id",
            "creation_fingerprint",
        ):
            assert secret not in response.text
    hidden = await client.get(
        PUBLIC, headers=viewer, params={"q": "PRIVATE_ROLE_SENTINEL"}
    )
    assert hidden.json()["total"] == 0


@pytest.mark.parametrize(
    "reason", ["closed", "expired", "taken_down", "suspended", "qualification"]
)
async def test_all_public_surfaces_apply_current_visibility(
    client, db_session, clock, reason
):
    owner, _, headers, base, row = await published(client, db_session, clock)
    if reason == "closed":
        assert (
            await client.post(base + "/close", headers=headers, json=command(row))
        ).status_code == 200
    elif reason in ("expired", "taken_down"):
        patch = (
            {"deadline_at": clock.now()}
            if reason == "expired"
            else {"moderation_state": "TAKEN_DOWN"}
        )
        await db_session.execute(
            update(RecruitmentDraft)
            .where(RecruitmentDraft.id == UUID(row["id"]))
            .values(**patch)
        )
    elif reason == "suspended":
        await db_session.execute(
            update(User).where(User.id == owner.id).values(status="SUSPENDED")
        )
    else:
        await db_session.execute(
            update(OwnerQualification)
            .where(OwnerQualification.user_id == owner.id)
            .values(status="PENDING", approved_by=None, approved_at=None)
        )
    await db_session.commit()
    _, viewer = await account(db_session, clock)
    assert (await client.get(PUBLIC, headers=viewer)).json()["total"] == 0
    for suffix in ("", "/contacts"):
        response = await client.get(PUBLIC + "/" + row["id"] + suffix, headers=viewer)
        assert response.status_code == 404, response.text


async def test_public_pagination_filters_and_deadline_boundary(
    client, db_session, clock
):
    _, _, headers, base, row = await published(client, db_session, clock)
    collection = base.rsplit("/", 1)[0]
    future = clock.now() + timedelta(days=1)
    for index in range(3):
        response = await client.post(
            collection,
            headers=headers,
            json={
                "request_id": str(uuid4()),
                "fields": fields()
                | {
                    "role_name": f"岗位{index}",
                    "beginner_friendly": False,
                    "deadline_at": future.isoformat(),
                },
            },
        )
        result = await client.post(
            collection + "/" + response.json()["id"] + "/publish",
            headers=headers,
            json=command(response.json()),
        )
        assert result.status_code == 200
    _, viewer = await account(db_session, clock)
    one = await client.get(PUBLIC, headers=viewer, params={"limit": 1})
    two = await client.get(PUBLIC, headers=viewer, params={"limit": 1, "offset": 1})
    assert one.json()["total"] == two.json()["total"] == 4
    assert one.json()["items"][0]["id"] != two.json()["items"][0]["id"]
    assert (
        await client.get(PUBLIC, headers=viewer, params={"beginner_friendly": True})
    ).json()["total"] == 1
    assert (await client.get(PUBLIC, headers=viewer, params={"q": "%"})).json()[
        "total"
    ] == 0
    assert (
        await client.get(PUBLIC, headers=viewer, params={"limit": 51})
    ).status_code == 422
    assert (
        await client.get(PUBLIC, headers=viewer, params={"q": "x" * 121})
    ).status_code == 422


async def test_deadline_is_immediate_before_worker_runs(client, db_session, clock):
    from app.core.clock import FrozenClock
    from app.core.errors import BusinessError
    from app.modules.identity.enums import Role
    from app.modules.identity.events import Actor
    from app.modules.innovation.public_recruitment_service import (
        PublicRecruitmentService,
    )

    owner, _, headers, base, row = await published(client, db_session, clock)
    deadline = clock.now() + timedelta(days=1)
    edited = await client.patch(
        base,
        headers=headers,
        json=command(row)
        | {"fields": fields() | {"deadline_at": deadline.isoformat()}},
    )
    assert edited.status_code == 200
    row = (
        await client.post(
            base + "/publish", headers=headers, json=command(edited.json())
        )
    ).json()["current"]
    actor = Actor(user_id=owner.id, role=Role.STUDENT)
    before = PublicRecruitmentService(
        clock=FrozenClock(deadline - timedelta(milliseconds=1))
    )
    assert (
        await before.get_visible(
            db_session, actor=actor, recruitment_id=UUID(row["id"])
        )
    ).id == UUID(row["id"])
    for instant in (deadline, deadline + timedelta(milliseconds=1)):
        service = PublicRecruitmentService(clock=FrozenClock(instant))
        with pytest.raises(BusinessError) as error:
            await service.get_visible(
                db_session, actor=actor, recruitment_id=UUID(row["id"])
            )
        assert error.value.status_code == 404
