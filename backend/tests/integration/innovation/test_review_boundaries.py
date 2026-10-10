"""Actual PostgreSQL/HTTP replay, revocation and private-file failure boundaries.

External storage is the only programmable boundary. Assertions protect
against duplicate cases, retained conflicted claims and unverified bytes.
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.core.errors import BusinessError
from app.integrations.object_storage import ObjectHead
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.innovation.evidence_models import AchievementEvidence
from app.modules.innovation.models import OwnerQualification
from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementWorkflow,
)
from app.modules.innovation.review_operations_service import ReviewOperationsService
from tests.integration.innovation.test_achievement_evidence import (
    PDF,
    intent,
    path,
    world,
)
from tests.integration.innovation.test_achievement_review_operations import (
    OPS,
    account,
    claim,
    review,
)
from tests.integration.innovation.test_achievement_review_operations import (
    client as client,
)
from tests.integration.innovation.test_achievement_review_operations import (
    clock as clock,
)
from tests.integration.innovation.test_achievement_review_operations import (
    scanner as scanner,
)
from tests.integration.innovation.test_achievement_review_operations import (
    storage as storage,
)
from tests.integration.innovation.test_achievement_workflow import prepared

pytestmark = pytest.mark.integration


async def test_claim_replay_and_conflict_release_preserve_one_current_assignee(
    client, db_session, clock, storage
):
    _, _, _, _, _, _, submitted = await review(client, db_session, clock, storage)
    _, headers = await account(db_session, clock, operator=True)
    _, other = await account(db_session, clock, operator=True)
    case = submitted["review_case"]
    claimed = await claim(client, case, headers)
    for version in (case["version"], claimed["version"]):
        replay = await client.post(
            f"{OPS}/{case['id']}/claim", headers=headers, json={"version": version}
        )
        assert replay.status_code == 200 and replay.json() == claimed
    stale = await client.post(
        f"{OPS}/{case['id']}/conflict",
        headers=headers,
        json={"version": case["version"]},
    )
    assert stale.status_code == 409
    released = await client.post(
        f"{OPS}/{case['id']}/conflict",
        headers=headers,
        json={"version": claimed["version"]},
    )
    assert released.status_code == 204
    assert (await client.get(f"{OPS}/{case['id']}", headers=headers)).status_code == 403
    queue = (await client.get(OPS, headers=other)).json()
    assert queue["total"] == 1 and not queue["items"][0]["claimed_by_me"]
    claimed_other = await claim(client, queue["items"][0], other)
    assert claimed_other["version"] == claimed["version"] + 2


async def test_closed_and_missing_cases_cannot_be_claimed_or_decided(
    client, db_session, clock, storage
):
    _, _, _, owner_headers, base, _, submitted = await review(
        client, db_session, clock, storage
    )
    _, headers = await account(db_session, clock, operator=True)
    case = submitted["review_case"]
    decision = {
        "request_id": str(uuid4()),
        "version": case["version"],
        "revision_id": case["revision_id"],
        "decision": "APPROVED",
    }
    assert (
        await client.post(
            f"{OPS}/{case['id']}/decision", headers=headers, json=decision
        )
    ).status_code == 403
    assert (
        await client.post(
            f"{OPS}/{uuid4()}/claim", headers=headers, json={"version": 1}
        )
    ).status_code == 404
    result = await client.post(
        f"{base}/withdraw",
        headers=owner_headers,
        json={
            "workflow_version": submitted["version"],
            "case_id": case["id"],
            "case_version": case["version"],
        },
    )
    assert result.status_code == 200
    assert (
        await client.post(
            f"{OPS}/{case['id']}/claim",
            headers=headers,
            json={"version": case["version"] + 1},
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{OPS}/{case['id']}/conflict",
            headers=headers,
            json={"version": case["version"] + 1},
        )
    ).status_code == 409


@pytest.mark.parametrize(
    "change", ["owner-suspended", "owner-teacher", "qualification-removed"]
)
async def test_owner_authority_change_blocks_existing_operator_access(
    client, db_session, clock, storage, change
):
    owner, _, _, _, _, _, submitted = await review(client, db_session, clock, storage)
    _, headers = await account(db_session, clock, operator=True)
    case = await claim(client, submitted["review_case"], headers)
    if change == "qualification-removed":
        await db_session.execute(
            delete(OwnerQualification).where(OwnerQualification.user_id == owner.id)
        )
    elif change == "owner-teacher":
        owner.role = "TEACHER"
    else:
        owner.status = "SUSPENDED"
    await db_session.flush()
    assert (await client.get(f"{OPS}/{case['id']}", headers=headers)).status_code == 409


@pytest.mark.parametrize(
    "change", ["missing", "actor-admin", "database-teacher", "suspended"]
)
async def test_review_service_revalidates_current_account_even_without_router(
    client, db_session, clock, storage, change
):
    operator, _ = await account(db_session, clock, operator=True)
    uid = operator.id
    actor_role = Role.STUDENT
    if change == "missing":
        uid = uuid4()
    elif change == "actor-admin":
        actor_role = Role.ADMIN
    elif change == "database-teacher":
        operator.role = "TEACHER"
    else:
        operator.status = "SUSPENDED"
    await db_session.flush()
    service = ReviewOperationsService(clock=clock, storage=storage)
    with pytest.raises(BusinessError) as failure:
        await service.list_queue(
            db_session, actor=Actor(user_id=uid, role=actor_role), limit=20, offset=0
        )
    assert failure.value.status_code == 403


@pytest.mark.parametrize(
    "failure", ["provider", "size", "type", "digest", "unreferenced"]
)
async def test_operator_private_read_refuses_unavailable_or_changed_objects(
    client, db_session, clock, storage, failure
):
    _, _, _, _, _, command, submitted = await review(client, db_session, clock, storage)
    _, headers = await account(db_session, clock, operator=True)
    case = await claim(client, submitted["review_case"], headers)
    eid = UUID(command["evidence_ids"][0])
    material = await db_session.get(AchievementEvidence, eid)
    key = material.object_key
    if failure == "provider":
        storage.failures.append(OSError("storage unavailable"))
    elif failure == "size":
        storage.put_object(object_key=key, content=PDF + b"extra")
    elif failure == "type":
        storage.objects[key] = ObjectHead(
            object_key=key, size=len(PDF), content_type="image/png"
        )
    elif failure == "digest":
        storage.put_object(object_key=key, content=PDF.replace(b"<<>>", b"<xx>"))
    else:
        eid = uuid4()
    response = await client.get(
        f"{OPS}/{case['id']}/evidence/{eid}/content", headers=headers
    )
    assert response.status_code == (404 if failure == "unreferenced" else 409)
    assert response.headers["content-type"].startswith("application/json")
    assert PDF not in response.content and key not in response.text


async def test_pending_case_and_unapproved_update_cannot_bypass_workflow(
    client, db_session, clock, storage
):
    _, parent, achievement, headers, base, command = await prepared(
        db_session, clock, client, storage
    )
    assert (
        await client.post(f"{base}/submit-update", headers=headers, json=command)
    ).status_code == 409
    duplicate = {**command, "evidence_ids": command["evidence_ids"] * 2}
    assert (
        await client.post(f"{base}/submit", headers=headers, json=duplicate)
    ).status_code == 422
    wrong = {**command, "evidence_ids": [str(uuid4())]}
    assert (
        await client.post(f"{base}/submit", headers=headers, json=wrong)
    ).status_code == 409
    submitted = await client.post(f"{base}/submit", headers=headers, json=command)
    assert submitted.status_code == 200
    pending = submitted.json()
    again = {
        **command,
        "request_id": str(uuid4()),
        "workflow_version": pending["version"],
    }
    assert (
        await client.post(f"{base}/submit", headers=headers, json=again)
    ).status_code == 409
    assert (
        await client.post(
            f"{base}/withdraw",
            headers=headers,
            json={
                "workflow_version": pending["version"],
                "case_id": str(uuid4()),
                "case_version": 1,
            },
        )
    ).status_code == 404
    cases = list(
        await db_session.scalars(
            select(AchievementReviewCase).where(
                AchievementReviewCase.achievement_id == achievement.id
            )
        )
    )
    assert len(cases) == 1
    assert (
        await client.get(
            f"/api/v1/ie/me/project-drafts/{parent.id}/achievements/{uuid4()}/workflow",
            headers=headers,
        )
    ).status_code == 404


async def test_revoked_qualification_blocks_new_snapshot(
    client, db_session, clock, storage
):
    owner, parent, achievement, headers, base, command = await prepared(
        db_session, clock, client, storage
    )
    qualification = await db_session.get(OwnerQualification, owner.id)
    await db_session.delete(qualification)
    await db_session.flush()
    assert (
        await client.post(f"{base}/submit", headers=headers, json=command)
    ).status_code == 409
    workflow = await db_session.get(AchievementWorkflow, achievement.id)
    assert workflow.first_review_state == "DRAFT"


async def test_withdrawn_revision_still_protects_its_historical_proof(
    client, db_session, clock, storage
):
    _, parent, achievement, headers, base, command, submitted = await review(
        client, db_session, clock, storage
    )
    case = submitted["review_case"]
    assert (
        await client.post(
            f"{base}/withdraw",
            headers=headers,
            json={
                "workflow_version": submitted["version"],
                "case_id": case["id"],
                "case_version": case["version"],
            },
        )
    ).status_code == 200
    eid = command["evidence_ids"][0]
    assert (
        await client.delete(f"{path(parent, achievement)}/{eid}", headers=headers)
    ).status_code == 409
    assert (
        await client.get(f"{path(parent, achievement)}/{eid}/content", headers=headers)
    ).content == PDF


async def test_owner_proxy_storage_outage_returns_no_file(
    client, db_session, clock, storage
):
    _, parent, achievement, headers, _, command = await prepared(
        db_session, clock, client, storage
    )
    storage.failures.append(OSError("storage unavailable"))
    response = await client.get(
        f"{path(parent, achievement)}/{command['evidence_ids'][0]}/content",
        headers=headers,
    )
    assert response.status_code == 409 and PDF not in response.content


@pytest.mark.parametrize("entry", ["project", "profile", "campus"])
@pytest.mark.parametrize("change", ["missing", "role", "suspended"])
async def test_service_reads_revalidate_live_identity(db_session, clock, entry, change):
    from app.modules.innovation.owner_service import OwnerProfileService
    from app.modules.innovation.public_achievement_service import (
        PublicAchievementService,
    )
    from app.modules.innovation.service import ProjectDraftService

    user, _ = await account(db_session, clock)
    uid = user.id
    if change == "missing":
        uid = uuid4()
    elif change == "role":
        user.role = "TEACHER"
    else:
        user.status = "SUSPENDED"
    await db_session.flush()
    actor = Actor(user_id=uid, role=Role.STUDENT)
    with pytest.raises(BusinessError) as failure:
        if entry == "project":
            await ProjectDraftService(clock=clock).list_owned(
                db_session, actor=actor, limit=20, offset=0
            )
        elif entry == "profile":
            await OwnerProfileService(clock=clock).get_owned(db_session, actor=actor)
        else:
            await PublicAchievementService().list_visible(
                db_session, actor=actor, limit=20, offset=0
            )
    assert failure.value.status_code == (
        401 if change == "missing" and entry != "campus" else 403
    )


@pytest.mark.parametrize("entry", ["grant", "qualification"])
async def test_suspended_admin_cannot_read_other_users_authority(
    db_session, clock, entry
):
    from app.modules.innovation.operations_service import OperationsGrantService
    from app.modules.innovation.qualification_service import OwnerQualificationService

    admin, _ = await account(db_session, clock, role="ADMIN")
    student, _ = await account(db_session, clock)
    admin.status = "SUSPENDED"
    await db_session.flush()
    actor = Actor(user_id=admin.id, role=Role.ADMIN)
    with pytest.raises(BusinessError) as failure:
        if entry == "grant":
            await OperationsGrantService(clock=clock).read(
                db_session, actor=actor, user_id=student.id
            )
        else:
            await OwnerQualificationService(clock=clock).queue(
                db_session, actor=actor, limit=20, offset=0
            )
    assert failure.value.status_code == 403


@pytest.mark.parametrize("mismatch", ["head", "bytes"])
async def test_declared_size_must_match_both_head_and_bytes(
    client, db_session, clock, storage, mismatch
):
    _, parent, achievement, headers = await world(db_session, clock)
    body = intent()
    created = (
        await client.post(path(parent, achievement), headers=headers, json=body)
    ).json()
    key = storage.upload_urls[-1].object_key
    storage.put_object(
        object_key=key,
        content=PDF + b"extra",
        size=len(PDF) if mismatch == "bytes" else None,
    )
    base = f"{path(parent, achievement)}/{created['evidence']['id']}"
    response = await client.post(f"{base}/complete", headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "REJECTED"
    assert response.json()["sha256"] is None
    assert (await client.get(f"{base}/content", headers=headers)).status_code == 409


async def test_removed_upload_key_and_active_scan_lease_cannot_be_reused(
    client, db_session, clock, storage
):
    _, parent, achievement, headers = await world(db_session, clock)
    body = intent()
    created = (
        await client.post(path(parent, achievement), headers=headers, json=body)
    ).json()
    base = f"{path(parent, achievement)}/{created['evidence']['id']}"
    assert (await client.delete(base, headers=headers)).status_code == 204
    assert (
        await client.post(path(parent, achievement), headers=headers, json=body)
    ).status_code == 409
    fresh = (
        await client.post(path(parent, achievement), headers=headers, json=intent())
    ).json()["evidence"]
    row = await db_session.get(AchievementEvidence, UUID(fresh["id"]))
    row.state, row.check_token, row.checking_until = (
        "CHECKING",
        uuid4(),
        clock.now() + timedelta(seconds=150),
    )
    await db_session.flush()
    response = await client.post(
        f"{path(parent, achievement)}/{row.id}/complete", headers=headers
    )
    assert response.status_code == 409
    assert row.sha256 is None
