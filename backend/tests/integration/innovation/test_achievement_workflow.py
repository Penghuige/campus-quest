"""First-review snapshots, owner version commands and explicit publication."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.modules.innovation.models import AchievementDraft, OwnerProfile, ProjectDraft
from tests.integration.innovation.test_achievement_evidence import (
    PDF,
    intent,
    path,
    world,
)
from tests.integration.innovation.test_achievement_evidence import client as client
from tests.integration.innovation.test_achievement_evidence import clock as clock
from tests.integration.innovation.test_achievement_evidence import scanner as scanner
from tests.integration.innovation.test_achievement_evidence import storage as storage

pytestmark = pytest.mark.integration


async def prepared(db, clock, client, storage):
    owner, parent, achievement, headers = await world(db, clock)
    db.add(
        OwnerProfile(
            user_id=owner.id,
            name="本人姓名",
            student_no="000002",
            major="专业",
            grade="2026",
        )
    )
    parent.summary, parent.direction, parent.stage, parent.team_status = (
        "概况",
        "教育",
        "原型",
        "两人团队",
    )
    achievement.description = "已经完成可运行原型"
    await db.flush()
    signed = (
        await client.post(path(parent, achievement), headers=headers, json=intent())
    ).json()
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    checked = await client.post(
        f"{path(parent, achievement)}/{signed['evidence']['id']}/complete",
        headers=headers,
    )
    assert checked.json()["state"] == "READY"
    base = path(parent, achievement).removesuffix("/evidence")
    workflow = (await client.get(f"{base}/workflow", headers=headers)).json()
    payload = {
        "request_id": str(uuid4()),
        "workflow_version": workflow["version"],
        "project_version": parent.version,
        "achievement_version": achievement.version,
        "evidence_ids": [signed["evidence"]["id"]],
    }
    return owner, parent, achievement, headers, base, payload


async def test_submit_freezes_saved_content_and_withdraw_allows_edit(
    client, db_session, clock, storage
):
    from app.modules.innovation.evidence_models import AchievementEvidence
    from app.modules.innovation.review_models import (
        AchievementReviewCase,
        AchievementRevision,
    )

    owner, parent, achievement, headers, base, payload = await prepared(
        db_session, clock, client, storage
    )
    submitted = await client.post(f"{base}/submit", headers=headers, json=payload)
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert (
        result["first_review_state"] == "SUBMITTED"
        and result["public_revision_id"] is None
    )
    assert "student_no" not in submitted.text and "object_key" not in submitted.text
    retry = await client.post(f"{base}/submit", headers=headers, json=payload)
    assert retry.json() == result
    assert (
        await client.post(
            f"{base}/submit", headers=headers, json={**payload, "project_version": 2}
        )
    ).status_code == 409
    revision = await db_session.get(
        AchievementRevision, UUID(result["review_case"]["revision_id"])
    )
    assert revision.owner_profile["name"] == "本人姓名"
    assert revision.project_content["summary"] == "概况"
    saved_title = revision.achievement_content["title"]
    parent.summary = "提交后更改的项目草稿"
    await db_session.flush()
    edit = {
        "title": "改名",
        "description": "新说明",
        "work_url": "",
        "award_text": "",
        "version": achievement.version,
    }
    assert (await client.patch(base, headers=headers, json=edit)).status_code == 409
    proof = await db_session.get(AchievementEvidence, UUID(payload["evidence_ids"][0]))
    assert proof.referenced
    assert (
        await client.delete(f"{base}/evidence/{proof.id}", headers=headers)
    ).status_code == 409
    withdrawal = {
        "workflow_version": result["version"],
        "case_id": result["review_case"]["id"],
        "case_version": result["review_case"]["version"],
    }
    closed = await client.post(f"{base}/withdraw", headers=headers, json=withdrawal)
    assert closed.status_code == 200 and closed.json()["first_review_state"] == "DRAFT"
    assert (await client.patch(base, headers=headers, json=edit)).status_code == 200
    await db_session.refresh(revision)
    assert (
        revision.project_content["summary"] == "概况"
        and revision.achievement_content["title"] == saved_title
    )
    case = await db_session.get(AchievementReviewCase, UUID(withdrawal["case_id"]))
    assert case.status == "WITHDRAWN"


@pytest.mark.parametrize(
    "missing", ["profile", "project", "description", "evidence", "version"]
)
async def test_submission_rejects_incomplete_or_stale_saved_state(
    client, db_session, clock, storage, missing
):
    from app.modules.innovation.review_models import AchievementReviewCase

    owner, parent, achievement, headers, base, payload = await prepared(
        db_session, clock, client, storage
    )
    if missing == "profile":
        from sqlalchemy import delete

        await db_session.execute(
            delete(OwnerProfile).where(OwnerProfile.user_id == owner.id)
        )
    elif missing == "project":
        parent.stage = ""
    elif missing == "description":
        achievement.description = ""
    elif missing == "evidence":
        payload["evidence_ids"] = []
    else:
        payload["achievement_version"] += 1
    await db_session.flush()
    rejected = await client.post(f"{base}/submit", headers=headers, json=payload)
    assert rejected.status_code in (409, 422)
    assert not list(await db_session.scalars(select(AchievementReviewCase)))


async def test_publish_update_is_explicit_immutable_and_never_restores_takedown(
    client, db_session, clock, storage
):
    from app.modules.innovation.review_models import (
        AchievementReviewCase,
        AchievementRevision,
        AchievementWorkflow,
    )

    _, _, achievement, headers, base, payload = await prepared(
        db_session, clock, client, storage
    )
    submitted = (
        await client.post(f"{base}/submit", headers=headers, json=payload)
    ).json()
    case = await db_session.get(
        AchievementReviewCase, UUID(submitted["review_case"]["id"])
    )
    workflow = await db_session.get(AchievementWorkflow, achievement.id)
    # Boundary setup only: Task4 verifies the real operator decision transaction.
    case.status = "APPROVED"
    case.decided_at = clock.now()
    case.decision_request_id = uuid4()
    case.decision_payload_fingerprint = "0" * 64
    workflow.first_review_state = "APPROVED"
    workflow.public_revision_id = case.revision_id
    workflow.first_approved_at = clock.now()
    workflow.latest_update_at = clock.now()
    await db_session.flush()
    first_id, approved_at = workflow.public_revision_id, workflow.first_approved_at
    edit = {
        "title": "公开更新",
        "description": "第二阶段",
        "work_url": "",
        "award_text": "",
        "version": achievement.version,
    }
    assert (await client.patch(base, headers=headers, json=edit)).status_code == 200
    await db_session.refresh(workflow)
    assert workflow.public_revision_id == first_id
    current = (await client.get(f"{base}/workflow", headers=headers)).json()
    assert current["has_unpublished_changes"]
    payload.update(
        request_id=str(uuid4()),
        workflow_version=current["version"],
        achievement_version=achievement.version,
    )
    published = await client.post(
        f"{base}/publish-update", headers=headers, json=payload
    )
    assert published.status_code == 200, published.text
    assert published.json()["public_revision_id"] != str(first_id)
    await db_session.refresh(workflow)
    assert workflow.first_approved_at == approved_at
    assert len(list(await db_session.scalars(select(AchievementReviewCase)))) == 1
    revision = await db_session.get(AchievementRevision, workflow.public_revision_id)
    assert revision.achievement_content["title"] == "公开更新"
    await db_session.execute(
        update(AchievementWorkflow)
        .where(AchievementWorkflow.achievement_id == achievement.id)
        .values(moderation_state="TAKEN_DOWN")
    )
    payload.update(
        request_id=str(uuid4()), workflow_version=published.json()["version"]
    )
    assert (
        await client.post(f"{base}/publish-update", headers=headers, json=payload)
    ).status_code == 200
    await db_session.refresh(workflow)
    assert workflow.moderation_state == "TAKEN_DOWN"


async def test_database_rejects_revision_and_checked_content_mutation(
    client, db_session, clock, storage
):
    from sqlalchemy.exc import DBAPIError

    from app.modules.innovation.evidence_models import AchievementEvidence
    from app.modules.innovation.review_models import AchievementRevision

    _, _, _, headers, base, payload = await prepared(db_session, clock, client, storage)
    submitted = (
        await client.post(f"{base}/submit", headers=headers, json=payload)
    ).json()
    rid = UUID(submitted["review_case"]["revision_id"])
    with pytest.raises(DBAPIError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(AchievementRevision)
                .where(AchievementRevision.id == rid)
                .values(achievement_content={"title": "偷偷替换"})
            )
    with pytest.raises(DBAPIError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(AchievementEvidence)
                .where(AchievementEvidence.id == UUID(payload["evidence_ids"][0]))
                .values(sha256="1" * 64)
            )


async def test_new_submission_uses_new_revision_and_old_withdrawal_cannot_close_it(
    client, db_session, clock, storage
):
    _, parent, achievement, headers, base, payload = await prepared(
        db_session, clock, client, storage
    )
    first = (await client.post(f"{base}/submit", headers=headers, json=payload)).json()
    withdrawal = {
        "workflow_version": first["version"],
        "case_id": first["review_case"]["id"],
        "case_version": first["review_case"]["version"],
    }
    closed = (
        await client.post(f"{base}/withdraw", headers=headers, json=withdrawal)
    ).json()
    payload.update(request_id=str(uuid4()), workflow_version=closed["version"])
    second = await client.post(f"{base}/submit", headers=headers, json=payload)
    assert second.status_code == 200
    assert second.json()["review_case"]["id"] != first["review_case"]["id"]
    assert (
        await client.post(f"{base}/withdraw", headers=headers, json=withdrawal)
    ).status_code == 409


async def test_two_committed_withdrawals_only_one_succeeds(db_engine, clock, storage):
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.errors import BusinessError
    from app.modules.audit.models import AuditLog
    from app.modules.identity.enums import Role
    from app.modules.identity.events import Actor
    from app.modules.identity.models import User, UserSession
    from app.modules.innovation.evidence_models import AchievementEvidence
    from app.modules.innovation.evidence_schemas import EvidenceIntentCreate
    from app.modules.innovation.evidence_service import EvidenceService
    from app.modules.innovation.models import OwnerQualification
    from app.modules.innovation.review_models import (
        AchievementReviewCase,
        AchievementRevision,
        AchievementWorkflow,
        RevisionEvidence,
    )
    from app.modules.innovation.review_schemas import (
        SavedRevisionCommand,
        WithdrawCommand,
    )
    from app.modules.innovation.review_service import AchievementReviewService
    from tests.integration.innovation.test_achievement_evidence import Scanner

    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    service = AchievementReviewService(clock=clock)
    async with sessions() as seed:
        owner, parent, achievement, _ = await world(seed, clock)
        uid, pid, aid = owner.id, parent.id, achievement.id
        args = {
            "actor": Actor(user_id=uid, role=Role.STUDENT),
            "project_id": pid,
            "achievement_id": aid,
        }
        seed.add(
            OwnerProfile(
                user_id=uid, name="姓名", student_no="001", major="专业", grade="2026"
            )
        )
        parent.summary, parent.direction, parent.stage, parent.team_status = (
            "介绍",
            "方向",
            "阶段",
            "团队",
        )
        achievement.description = "成果说明"
        evidence = EvidenceService(clock=clock, storage=storage, scanner=Scanner())
        signed, _ = await evidence.create_intent(
            seed, **args, payload=EvidenceIntentCreate(**intent())
        )
        storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
        await evidence.complete(seed, **args, evidence_id=signed.evidence.id)
        workflow = await service.workflow(seed, **args)
        submitted = await service.submit(
            seed,
            **args,
            payload=SavedRevisionCommand(
                request_id=uuid4(),
                workflow_version=workflow.version,
                project_version=parent.version,
                achievement_version=achievement.version,
                evidence_ids=[signed.evidence.id],
            ),
        )
        command = WithdrawCommand(
            workflow_version=submitted.version,
            case_id=submitted.review_case.id,
            case_version=submitted.review_case.version,
        )
    try:

        async def withdraw():
            async with sessions() as db:
                try:
                    return await service.withdraw(db, **args, payload=command)
                except BusinessError as exc:
                    return exc

        results = await asyncio.wait_for(asyncio.gather(withdraw(), withdraw()), 8)
        assert sum(isinstance(result, BusinessError) for result in results) == 1
        async with sessions() as check:
            row = await check.get(AchievementReviewCase, command.case_id)
            assert row.status == "WITHDRAWN" and row.version == 2
    finally:
        async with sessions() as cleanup:
            for model, predicate in (
                (AchievementWorkflow, AchievementWorkflow.achievement_id == aid),
                (AchievementReviewCase, AchievementReviewCase.achievement_id == aid),
                (RevisionEvidence, RevisionEvidence.achievement_id == aid),
                (AchievementRevision, AchievementRevision.achievement_id == aid),
                (AchievementEvidence, AchievementEvidence.achievement_id == aid),
                (AchievementDraft, AchievementDraft.id == aid),
                (OwnerQualification, OwnerQualification.user_id == uid),
                (OwnerProfile, OwnerProfile.user_id == uid),
                (AuditLog, AuditLog.actor_user_id == uid),
                (ProjectDraft, ProjectDraft.id == pid),
                (UserSession, UserSession.user_id == uid),
                (User, User.id == uid),
            ):
                await cleanup.execute(delete(model).where(predicate))
            await cleanup.commit()
