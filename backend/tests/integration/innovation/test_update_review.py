"""Published updates keep the approved snapshot until a new real decision."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.modules.innovation.review_models import (
    AchievementReviewCase,
    AchievementWorkflow,
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
    scanner as scanner,
)
from tests.integration.innovation.test_achievement_review_operations import (
    storage as storage,
)

pytestmark = pytest.mark.integration
PUBLIC = "/api/v1/ie/achievements"


@dataclass
class AdvancingClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@pytest.fixture
def clock():
    return AdvancingClock(datetime.now(UTC))


async def decide(client, submitted, headers, decision="APPROVED"):
    case = await claim(client, submitted["review_case"], headers)
    command = {
        "request_id": str(uuid4()),
        "version": case["version"],
        "revision_id": case["revision_id"],
        "decision": decision,
        "reason": "请补充说明" if decision == "RETURNED" else "",
    }
    result = await client.post(
        f"{OPS}/{case['id']}/decision", headers=headers, json=command
    )
    assert result.status_code == 200, result.text
    return result.json(), command


@pytest.mark.parametrize("closed_by", ["RETURNED", "WITHDRAWN"])
async def test_update_preserves_public_until_approval_and_old_commands_are_fenced(
    client, db_session, clock, storage, closed_by
):
    _, parent, achievement, owner, base, command, first = await review(
        client, db_session, clock, storage
    )
    _, ops = await account(db_session, clock, operator=True)
    _, viewer = await account(db_session, clock)
    await decide(client, first, ops)
    url = f"{PUBLIC}/{achievement.id}"
    original = (await client.get(url, headers=viewer)).json()
    clock.current += timedelta(seconds=1)
    original_id = (await client.get(f"{base}/workflow", headers=owner)).json()[
        "public_revision_id"
    ]
    edit = {
        "title": "新版成果",
        "description": "需要重新核实的内容",
        "work_url": "",
        "award_text": "",
        "version": achievement.version,
    }
    assert (await client.patch(base, headers=owner, json=edit)).status_code == 200
    current = (await client.get(f"{base}/workflow", headers=owner)).json()
    command.update(
        request_id=str(uuid4()),
        workflow_version=current["version"],
        achievement_version=achievement.version,
    )
    legacy = await client.post(f"{base}/publish-update", headers=owner, json=command)
    assert legacy.status_code == 404
    submitted = await client.post(f"{base}/submit-update", headers=owner, json=command)
    assert submitted.status_code == 200, submitted.text
    pending = submitted.json()
    case = pending["review_case"]
    assert case["operation"] == "UPDATE" and case["status"] == "SUBMITTED"
    assert pending["first_review_state"] == "APPROVED"
    assert pending["public_revision_id"] == original_id
    assert (await client.get(url, headers=viewer)).json() == original
    assert (
        await client.post(f"{base}/submit-update", headers=owner, json=command)
    ).json() == pending
    conflicting = {**command, "achievement_version": achievement.version + 1}
    assert (
        await client.post(f"{base}/submit-update", headers=owner, json=conflicting)
    ).status_code == 409
    assert (
        await client.post(
            f"{base}/submit-update",
            headers=owner,
            json={
                **command,
                "request_id": str(uuid4()),
                "workflow_version": pending["version"],
            },
        )
    ).status_code == 409
    assert (
        await client.patch(
            base, headers=owner, json={**edit, "version": achievement.version}
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{base}/evidence",
            headers=owner,
            json={
                "content_type": "application/pdf",
                "size": 100,
                "request_id": str(uuid4()),
            },
        )
    ).status_code == 409
    # A separate project draft edit cannot change the pending or public snapshot.
    parent.summary = "仅保存的项目新稿"
    parent.version += 1
    await db_session.flush()
    claimed = await claim(client, case, ops)
    detail = (await client.get(f"{OPS}/{case['id']}", headers=ops)).json()
    assert detail["project_content"]["summary"] != parent.summary
    withdrawal = {
        "workflow_version": pending["version"],
        "case_id": case["id"],
        "case_version": claimed["version"],
    }
    old_decision = None
    if closed_by == "RETURNED":
        _, old_decision = await decide(
            client, {"review_case": claimed}, ops, "RETURNED"
        )
    else:
        closed = await client.post(f"{base}/withdraw", headers=owner, json=withdrawal)
        assert closed.status_code == 200, closed.text
    assert (await client.get(url, headers=viewer)).json() == original
    closed_state = (await client.get(f"{base}/workflow", headers=owner)).json()
    assert closed_state["first_review_state"] == "APPROVED"
    assert closed_state["review_case"]["status"] == closed_by
    assert (
        await client.patch(
            base, headers=owner, json={**edit, "version": achievement.version}
        )
    ).status_code == 200
    command.update(
        request_id=str(uuid4()),
        workflow_version=closed_state["version"],
        project_version=parent.version,
        achievement_version=achievement.version,
    )
    newer = (
        await client.post(f"{base}/submit-update", headers=owner, json=command)
    ).json()
    assert newer["review_case"]["id"] != case["id"]
    assert (
        await client.post(f"{base}/withdraw", headers=owner, json=withdrawal)
    ).status_code == 409
    if old_decision:
        # Exact replay returns that old outcome; it must not close the newer case.
        assert (
            await client.post(
                f"{OPS}/{case['id']}/decision", headers=ops, json=old_decision
            )
        ).status_code == 200
        assert (await client.get(f"{base}/workflow", headers=owner)).json()[
            "review_case"
        ]["status"] == "SUBMITTED"
    approved, decision = await decide(client, newer, ops)
    assert approved["operation"] == "UPDATE"
    changed = (await client.get(url, headers=viewer)).json()
    assert changed["achievement"]["title"] == "新版成果"
    assert changed["project"]["summary"] == parent.summary
    assert changed["first_approved_at"] == original["first_approved_at"]
    assert changed["latest_reviewed_at"] is not None
    assert changed["latest_reviewed_at"] != original["latest_reviewed_at"]
    assert changed["updated_at"] == changed["latest_reviewed_at"]
    assert changed["updated_after_first_review"] is True
    assert "owner_profile" not in changed and "evidence" not in changed
    assert (
        await client.post(
            f"{OPS}/{approved['id']}/decision", headers=ops, json=decision
        )
    ).json() == approved
    workflow = await db_session.get(
        AchievementWorkflow, achievement.id, populate_existing=True
    )
    assert str(workflow.public_revision_id) == newer["review_case"]["revision_id"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AchievementReviewCase)
            .where(AchievementReviewCase.achievement_id == achievement.id)
        )
        == 3
    )
    assert (
        await db_session.get(AchievementReviewCase, UUID(case["id"]))
    ).status == closed_by


async def test_update_approval_preserves_takedown(client, db_session, clock, storage):
    _, _, achievement, owner, base, command, first = await review(
        client, db_session, clock, storage
    )
    _, ops = await account(db_session, clock, operator=True)
    _, viewer = await account(db_session, clock)
    await decide(client, first, ops)
    current = (await client.get(f"{base}/workflow", headers=owner)).json()
    workflow = await db_session.get(AchievementWorkflow, achievement.id)
    workflow.moderation_state = "TAKEN_DOWN"
    await db_session.flush()
    command.update(request_id=str(uuid4()), workflow_version=current["version"])
    pending = await client.post(f"{base}/submit-update", headers=owner, json=command)
    assert pending.status_code == 200, pending.text
    await decide(client, pending.json(), ops)
    await db_session.refresh(workflow)
    assert workflow.moderation_state == "TAKEN_DOWN"
    assert (
        str(workflow.public_revision_id) == pending.json()["review_case"]["revision_id"]
    )
    assert (
        await client.get(f"{PUBLIC}/{achievement.id}", headers=viewer)
    ).status_code == 404


async def test_legacy_update_without_case_does_not_claim_review(
    client, db_session, clock, storage
):
    from app.modules.innovation.review_models import AchievementRevision

    _, _, achievement, _, _, _, first = await review(client, db_session, clock, storage)
    _, ops = await account(db_session, clock, operator=True)
    _, viewer = await account(db_session, clock)
    await decide(client, first, ops)
    original = await db_session.get(
        AchievementRevision, UUID(first["review_case"]["revision_id"])
    )
    legacy = AchievementRevision(
        achievement_id=achievement.id,
        number=2,
        operation="UPDATE",
        creation_request_id=uuid4(),
        creation_payload_fingerprint="0" * 64,
        project_version=original.project_version,
        achievement_version=original.achievement_version,
        project_content=original.project_content,
        achievement_content=original.achievement_content,
        owner_profile=original.owner_profile,
        created_at=clock.now(),
    )
    db_session.add(legacy)
    await db_session.flush()
    workflow = await db_session.get(AchievementWorkflow, achievement.id)
    workflow.public_revision_id = legacy.id
    await db_session.flush()
    visible = (await client.get(f"{PUBLIC}/{achievement.id}", headers=viewer)).json()
    assert visible["updated_after_first_review"] is True
    assert visible["latest_reviewed_at"] is None
