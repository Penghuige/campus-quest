"""Real HTTP recovery when an assigned operator becomes ineligible."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.core.errors import BusinessError
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.innovation.models import OperationsGrant
from app.modules.innovation.review_models import AchievementReviewCase, ReviewConflict
from app.modules.innovation.review_operations_service import ReviewOperationsService
from app.modules.innovation.review_schemas import ReviewVersionCommand
from tests.integration.innovation.test_achievement_evidence import PDF, intent
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
from tests.integration.innovation.test_review_races import committed as committed
from tests.integration.innovation.test_review_races import waiting

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("invalidation", ["revoke", "suspend", "role"])
async def test_ineligible_assignee_is_visible_and_reclaimable_without_owner_resubmit(
    client, db_session, clock, storage, invalidation
):
    _, _, _, _, _, _, submitted = await review(client, db_session, clock, storage)
    previous, previous_headers = await account(db_session, clock, operator=True)
    next_operator, next_headers = await account(db_session, clock, operator=True)
    assigned = await claim(client, submitted["review_case"], previous_headers)
    if invalidation == "revoke":
        grant = await db_session.get(OperationsGrant, previous.id)
        grant.enabled = False
    elif invalidation == "suspend":
        previous.status = "SUSPENDED"
    else:
        previous.role = "TEACHER"
    await db_session.commit()
    assert (
        await client.get(f"{OPS}/{assigned['id']}", headers=previous_headers)
    ).status_code == 403
    queue = await client.get(OPS, headers=next_headers)
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()["items"]] == [assigned["id"]]
    assert not queue.json()["items"][0]["claimed_by_me"]
    recovered = await claim(client, assigned, next_headers)
    assert recovered["version"] == assigned["version"] + 1
    assert (
        await client.get(f"{OPS}/{assigned['id']}", headers=next_headers)
    ).status_code == 200
    assert (
        await client.get(f"{OPS}/{assigned['id']}", headers=previous_headers)
    ).status_code == 403
    stale = await client.post(
        f"{OPS}/{assigned['id']}/claim",
        headers=previous_headers,
        json={"version": assigned["version"]},
    )
    assert stale.status_code == 403
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "IE_REVIEW_CLAIM_RECOVER",
            AuditLog.actor_user_id == next_operator.id,
            AuditLog.target_id == assigned["id"],
        )
    )
    assert audit is not None
    # Restoring the old operator must not restore access or steal a valid claim.
    previous.role, previous.status = "STUDENT", "ACTIVE"
    grant = await db_session.get(OperationsGrant, previous.id)
    grant.enabled = True
    await db_session.commit()
    assert (
        await client.get(f"{OPS}/{assigned['id']}", headers=previous_headers)
    ).status_code == 403
    stolen = await client.post(
        f"{OPS}/{assigned['id']}/claim",
        headers=previous_headers,
        json={"version": recovered["version"]},
    )
    assert stolen.status_code == 409


async def another_achievement(client, parent, headers, storage):
    collection = f"/api/v1/ie/me/project-drafts/{parent.id}/achievements"
    created = await client.post(
        collection,
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "title": "同项目第二份成果",
            "description": "另一阶段的可操作原型",
        },
    )
    assert created.status_code == 201, created.text
    achievement = created.json()
    base = f"{collection}/{achievement['id']}"
    signed = await client.post(f"{base}/evidence", headers=headers, json=intent())
    assert signed.status_code == 201
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    checked = await client.post(
        f"{base}/evidence/{signed.json()['evidence']['id']}/complete", headers=headers
    )
    assert checked.status_code == 200 and checked.json()["state"] == "READY"
    workflow = (await client.get(f"{base}/workflow", headers=headers)).json()
    submitted = await client.post(
        f"{base}/submit",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "workflow_version": workflow["version"],
            "project_version": parent.version,
            "achievement_version": achievement["version"],
            "evidence_ids": [signed.json()["evidence"]["id"]],
        },
    )
    assert submitted.status_code == 200, submitted.text
    return submitted.json()["review_case"]


async def test_project_conflict_releases_all_pending_claims_but_not_other_projects(
    client, db_session, clock, storage
):
    _, parent, _, owner_headers, _, _, submitted = await review(
        client, db_session, clock, storage
    )
    second = await another_achievement(client, parent, owner_headers, storage)
    _, _, _, _, _, _, other_project = await review(client, db_session, clock, storage)
    operator, headers = await account(db_session, clock, operator=True)
    _, replacement = await account(db_session, clock, operator=True)
    history = await another_achievement(client, parent, owner_headers, storage)
    historical_claim = await claim(client, history, headers)
    approved = await client.post(
        f"{OPS}/{history['id']}/decision",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "version": historical_claim["version"],
            "revision_id": history["revision_id"],
            "decision": "APPROVED",
        },
    )
    assert approved.status_code == 200
    cases = [submitted["review_case"], second, other_project["review_case"]]
    assigned = [await claim(client, case, headers) for case in cases]
    response = await client.post(
        f"{OPS}/{assigned[0]['id']}/conflict",
        headers=headers,
        json={"version": assigned[0]["version"]},
    )
    assert response.status_code == 204
    for case in assigned[:2]:
        row = await db_session.scalar(
            select(AchievementReviewCase)
            .where(AchievementReviewCase.id == UUID(case["id"]))
            .execution_options(populate_existing=True)
        )
        assert row.assigned_user_id is None
        assert row.version == case["version"] + 1
        assert (
            await client.get(f"{OPS}/{case['id']}", headers=headers)
        ).status_code == 403
    other = await db_session.get(AchievementReviewCase, UUID(assigned[2]["id"]))
    assert other.assigned_user_id == operator.id
    assert other.version == assigned[2]["version"]
    historical_row = await db_session.scalar(
        select(AchievementReviewCase)
        .where(AchievementReviewCase.id == UUID(history["id"]))
        .execution_options(populate_existing=True)
    )
    assert historical_row.assigned_user_id == operator.id
    assert (
        historical_row.status == "APPROVED"
        and historical_row.version == approved.json()["version"]
    )
    remaining = (await client.get(OPS, headers=headers)).json()["items"]
    assert [item["id"] for item in remaining] == [assigned[2]["id"]]
    available = (await client.get(OPS, headers=replacement)).json()["items"]
    assert {item["id"] for item in available} == {case["id"] for case in assigned[:2]}
    for item in available:
        await claim(client, item, replacement)


async def test_reclaim_rechecks_restored_authority_after_waiting_for_old_user_lock(
    committed, clock, storage
):
    sessions, _, operators, submitted, _ = committed
    service = ReviewOperationsService(clock=clock, storage=storage)
    async with sessions() as seed:
        assigned = await service.claim(
            seed,
            actor=operators[0],
            case_id=submitted.review_case.id,
            payload=ReviewVersionCommand(version=submitted.review_case.version),
        )
        grant = await seed.get(OperationsGrant, operators[0].user_id)
        grant.enabled = False
        await seed.commit()
    pid = asyncio.get_running_loop().create_future()

    async def reclaim():
        async with sessions() as db:
            pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            try:
                return await service.claim(
                    db,
                    actor=operators[1],
                    case_id=assigned.id,
                    payload=ReviewVersionCommand(version=assigned.version),
                )
            except BusinessError as exc:
                await db.rollback()
                return exc

    task = None
    try:
        async with sessions() as holder:
            await holder.execute(
                select(User).where(User.id == operators[0].user_id).with_for_update()
            )
            grant = await holder.get(OperationsGrant, operators[0].user_id)
            grant.enabled = True
            await holder.flush()
            task = asyncio.create_task(reclaim())
            await waiting(sessions, await pid)
            await holder.commit()
        result = await asyncio.wait_for(task, 8)
        assert isinstance(result, BusinessError) and result.status_code == 409
        async with sessions() as reader:
            current = await reader.get(AchievementReviewCase, assigned.id)
            assert current.assigned_user_id == operators[0].user_id
            assert current.version == assigned.version
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["reclaim", "project_conflict"])
async def test_assignment_recovery_audit_failure_rolls_back_all_claim_changes(
    client, db_session, clock, storage, operation
):
    _, parent, _, owner_headers, _, _, submitted = await review(
        client, db_session, clock, storage
    )
    second = await another_achievement(client, parent, owner_headers, storage)
    previous, previous_headers = await account(db_session, clock, operator=True)
    replacement, _ = await account(db_session, clock, operator=True)
    previous_id, replacement_id, project_id = previous.id, replacement.id, parent.id
    assigned = [
        await claim(client, case, previous_headers)
        for case in [submitted["review_case"], second]
    ]

    class FailingAudit:
        def __init__(self):
            self.calls = 0

        async def append(self, db, **kwargs):
            self.calls += 1
            # The bulk operation must also roll back its first completed append.
            if operation == "project_conflict" and self.calls == 1:
                from app.modules.audit.service import AuditLogWriter

                return await AuditLogWriter().append(db, **kwargs)
            raise RuntimeError("recovery audit failed")

    audit = FailingAudit()
    service = ReviewOperationsService(clock=clock, storage=storage, audit=audit)
    if operation == "reclaim":
        grant = await db_session.get(OperationsGrant, previous_id)
        grant.enabled = False
        await db_session.commit()
    with pytest.raises(RuntimeError, match="recovery audit failed"):
        if operation == "reclaim":
            await service.claim(
                db_session,
                actor=Actor(user_id=replacement_id, role=Role.STUDENT),
                case_id=UUID(assigned[0]["id"]),
                payload=ReviewVersionCommand(version=assigned[0]["version"]),
            )
        else:
            await service.declare_conflict(
                db_session,
                actor=Actor(user_id=previous_id, role=Role.STUDENT),
                case_id=UUID(assigned[0]["id"]),
                payload=ReviewVersionCommand(version=assigned[0]["version"]),
            )
    await db_session.rollback()
    for case in assigned:
        row = await db_session.get(AchievementReviewCase, UUID(case["id"]))
        assert row.assigned_user_id == previous_id and row.version == case["version"]
    assert (
        await db_session.scalar(
            select(ReviewConflict.user_id).where(
                ReviewConflict.project_id == project_id,
                ReviewConflict.user_id == previous_id,
            )
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(AuditLog.id).where(
                AuditLog.action.in_(
                    ["IE_REVIEW_CLAIM_RECOVER", "IE_REVIEW_CONFLICT_RELEASE"]
                ),
                AuditLog.target_id.in_([case["id"] for case in assigned]),
            )
        )
        is None
    )
