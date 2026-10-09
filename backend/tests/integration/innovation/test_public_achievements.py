"""Visible projection uses published revisions and current authenticated viewers."""

from uuid import uuid4

import pytest
from sqlalchemy import update

from app.modules.identity.models import User
from app.modules.innovation.review_models import AchievementWorkflow
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

pytestmark = pytest.mark.integration
PUBLIC = "/api/v1/ie/achievements"


async def test_public_projection_visibility_and_private_fields(
    client, db_session, clock, storage
):
    _, _, achievement, owner_headers, base, command, submitted = await review(
        client, db_session, clock, storage
    )
    _, ops = await account(db_session, clock, operator=True)
    viewer, headers = await account(db_session, clock)
    case = submitted["review_case"]
    assert (await client.get(PUBLIC)).status_code == 401
    assert (await client.get(PUBLIC, headers=headers)).json()["items"] == []
    claimed = await claim(client, case, ops)
    decision = {
        "request_id": str(uuid4()),
        "version": claimed["version"],
        "revision_id": case["revision_id"],
        "decision": "APPROVED",
        "reason": "",
    }
    assert (
        await client.post(f"{OPS}/{case['id']}/decision", headers=ops, json=decision)
    ).status_code == 200
    original = (await client.get(f"{PUBLIC}/{achievement.id}", headers=headers)).json()
    assert original["updated_after_first_review"] is False
    listed = await client.get(PUBLIC, headers=headers, params={"limit": 1, "offset": 0})
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert listed.json()["items"] == [original]
    assert (
        await client.get(PUBLIC, headers=headers, params={"limit": 1, "offset": 1})
    ).json()["items"] == []
    assert (
        await client.get(PUBLIC, headers=headers, params={"limit": 51})
    ).status_code == 422
    edit = {
        "title": "私有更新",
        "description": "修改说明",
        "work_url": "",
        "award_text": "",
        "version": achievement.version,
    }
    assert (
        await client.patch(base, headers=owner_headers, json=edit)
    ).status_code == 200
    assert (
        await client.get(f"{PUBLIC}/{achievement.id}", headers=headers)
    ).json() == original
    workflow = (await client.get(f"{base}/workflow", headers=owner_headers)).json()
    command.update(
        request_id=str(uuid4()),
        workflow_version=workflow["version"],
        achievement_version=achievement.version,
    )
    assert (
        await client.post(f"{base}/publish-update", headers=owner_headers, json=command)
    ).status_code == 200
    changed = await client.get(f"{PUBLIC}/{achievement.id}", headers=headers)
    assert (
        changed.json()["achievement"]["title"] == "私有更新"
        and changed.json()["updated_after_first_review"]
    )
    assert changed.json()["first_approved_at"] == original["first_approved_at"]
    assert all(
        field not in changed.text
        for field in (
            "owner_profile",
            "student_no",
            "evidence",
            "object_key",
            "review_case",
        )
    )
    eid = command["evidence_ids"][0]
    assert (
        await client.get(f"{base}/evidence/{eid}/content", headers=headers)
    ).status_code == 404
    await db_session.execute(
        update(AchievementWorkflow)
        .where(AchievementWorkflow.achievement_id == achievement.id)
        .values(moderation_state="TAKEN_DOWN")
    )
    assert (await client.get(PUBLIC, headers=headers)).json()["items"] == []
    assert (
        await client.get(f"{PUBLIC}/{achievement.id}", headers=headers)
    ).status_code == 404
    await db_session.execute(
        update(User).where(User.id == viewer.id).values(status="SUSPENDED")
    )
    assert (await client.get(PUBLIC, headers=headers)).status_code == 403
    assert (
        await client.get(f"{PUBLIC}/{achievement.id}", headers=headers)
    ).status_code == 403
