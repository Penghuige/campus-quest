"""Real HTTP operator authority, private snapshot access and decision effects."""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_access_token_codec, get_business_clock
from app.modules.identity.models import User
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import OperationsGrant
from tests.integration.innovation.test_achievement_evidence import PDF
from tests.integration.innovation.test_achievement_evidence import clock as clock
from tests.integration.innovation.test_achievement_evidence import scanner as scanner
from tests.integration.innovation.test_achievement_evidence import storage as storage
from tests.integration.innovation.test_achievement_workflow import prepared

pytestmark = pytest.mark.integration
OPS = "/api/v1/ie/ops/achievement-reviews"


@pytest.fixture
async def client(db_session, clock, storage, scanner):
    from app.modules.innovation.evidence_router import get_evidence_service
    from app.modules.innovation.evidence_service import EvidenceService
    from app.modules.innovation.review_operations_router import (
        get_review_operations_service,
    )
    from app.modules.innovation.review_operations_service import ReviewOperationsService

    app = create_app()

    async def db():
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    app.dependency_overrides[get_evidence_service] = lambda: EvidenceService(
        clock=clock, storage=storage, scanner=scanner
    )
    app.dependency_overrides[get_review_operations_service] = lambda: (
        ReviewOperationsService(clock=clock, storage=storage)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as result:
        yield result


async def account(db, clock, *, operator=False, role="STUDENT"):
    user = User(
        username=f"operator-{uuid4().hex}",
        password_hash="unused",
        nickname="运营",
        role=role,
        status="ACTIVE",
    )
    db.add(user)
    await db.flush()
    if operator:
        db.add(
            OperationsGrant(
                user_id=user.id,
                enabled=True,
                version=1,
                changed_by=user.id,
                updated_at=clock.now(),
            )
        )
    await db.flush()
    token = (
        await SessionService(
            clock=clock, access_codec=get_access_token_codec()
        ).issue_session(db, user=user, now=clock.now())
    )[1]
    return user, {"Authorization": f"Bearer {token.access_token}"}


async def review(client, db, clock, storage):
    owner, parent, achievement, headers, base, command = await prepared(
        db, clock, client, storage
    )
    submitted = await client.post(f"{base}/submit", headers=headers, json=command)
    assert submitted.status_code == 200
    return owner, parent, achievement, headers, base, command, submitted.json()


async def claim(client, case, headers):
    result = await client.post(
        f"{OPS}/{case['id']}/claim", headers=headers, json={"version": case["version"]}
    )
    assert result.status_code == 200, result.text
    return result.json()


async def test_operator_claim_gates_private_material_and_publication(
    client, db_session, clock, storage
):
    from app.modules.notifications.models import Notification, NotificationDelivery

    owner, parent, achievement, owner_headers, base, command, submitted = await review(
        client, db_session, clock, storage
    )
    operator, headers = await account(db_session, clock, operator=True)
    _, outsider = await account(db_session, clock)
    _, another = await account(db_session, clock, operator=True)
    _, admin = await account(db_session, clock, role="ADMIN")
    case = submitted["review_case"]
    assert (
        await client.get(f"{OPS}/{case['id']}", headers=outsider)
    ).status_code == 403
    assert (await client.get(f"{OPS}/{case['id']}", headers=admin)).status_code == 403
    assert (await client.get(f"{OPS}/{case['id']}", headers=headers)).status_code == 403
    queue = await client.get(OPS, headers=headers)
    assert queue.status_code == 200 and queue.json()["total"] == 1
    assert "student_no" not in queue.text and "evidence" not in queue.text
    assert (
        await client.get(f"/api/v1/ie/achievements/{achievement.id}", headers=outsider)
    ).status_code == 404
    claimed = await claim(client, case, headers)
    assert (
        await client.post(
            f"{OPS}/{case['id']}/claim",
            headers=another,
            json={"version": case["version"]},
        )
    ).status_code == 409
    detail = await client.get(f"{OPS}/{case['id']}", headers=headers)
    assert (
        detail.status_code == 200
        and detail.json()["owner_profile"]["student_no"] == "000002"
    )
    eid = command["evidence_ids"][0]
    file = await client.get(
        f"{OPS}/{case['id']}/evidence/{eid}/content", headers=headers
    )
    assert file.content == PDF and file.headers["x-content-type-options"] == "nosniff"
    assert (
        await client.get(f"{OPS}/{case['id']}/evidence/{eid}/content", headers=outsider)
    ).status_code == 403
    decision = {
        "request_id": str(uuid4()),
        "version": claimed["version"],
        "revision_id": case["revision_id"],
        "decision": "APPROVED",
        "reason": "",
    }
    approved = await client.post(
        f"{OPS}/{case['id']}/decision", headers=headers, json=decision
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert (
        await client.post(
            f"{OPS}/{case['id']}/decision", headers=headers, json=decision
        )
    ).json() == approved.json()
    assert (
        await client.post(
            f"{OPS}/{case['id']}/decision",
            headers=headers,
            json={**decision, "decision": "RETURNED", "reason": "改主意"},
        )
    ).status_code == 409
    rows = list(
        await db_session.scalars(
            select(Notification).where(Notification.user_id == owner.id)
        )
    )
    assert len(rows) == 1 and rows[0].event_type == "IE_ACHIEVEMENT_APPROVED"
    assert "核实通过" in rows[0].body
    assert (
        len(
            list(
                await db_session.scalars(
                    select(NotificationDelivery).where(
                        NotificationDelivery.user_id == owner.id
                    )
                )
            )
        )
        == 1
    )
    visible = await client.get(
        f"/api/v1/ie/achievements/{achievement.id}", headers=outsider
    )
    assert visible.status_code == 200
    assert visible.json()["achievement"]["title"] == achievement.title
    assert all(
        field not in visible.text
        for field in ("student_no", "owner_profile", "evidence", "object_key")
    )
    assert (await client.get(f"{OPS}/{case['id']}", headers=headers)).status_code == 409


async def test_self_conflict_declared_conflict_and_revocation_are_enforced(
    client, db_session, clock, storage
):
    owner, _, _, owner_headers, _, _, submitted = await review(
        client, db_session, clock, storage
    )
    db_session.add(
        OperationsGrant(
            user_id=owner.id,
            enabled=True,
            version=1,
            changed_by=owner.id,
            updated_at=clock.now(),
        )
    )
    await db_session.flush()
    case = submitted["review_case"]
    assert (
        await client.post(
            f"{OPS}/{case['id']}/claim",
            headers=owner_headers,
            json={"version": case["version"]},
        )
    ).status_code == 403
    operator, headers = await account(db_session, clock, operator=True)
    assert (
        await client.post(
            f"{OPS}/{case['id']}/conflict",
            headers=headers,
            json={"version": case["version"]},
        )
    ).status_code == 204
    assert (await client.get(OPS, headers=headers)).json()["total"] == 0
    assert (
        await client.post(
            f"{OPS}/{case['id']}/claim",
            headers=headers,
            json={"version": case["version"]},
        )
    ).status_code == 403
    operator2, headers2 = await account(db_session, clock, operator=True)
    claimed = await claim(client, case, headers2)
    grant = await db_session.get(OperationsGrant, operator2.id)
    grant.enabled = False
    await db_session.flush()
    assert (
        await client.get(f"{OPS}/{case['id']}", headers=headers2)
    ).status_code == 403
    decision = {
        "request_id": str(uuid4()),
        "version": claimed["version"],
        "revision_id": case["revision_id"],
        "decision": "APPROVED",
        "reason": "",
    }
    assert (
        await client.post(
            f"{OPS}/{case['id']}/decision", headers=headers2, json=decision
        )
    ).status_code == 403


async def test_return_requires_reason_and_owner_can_resubmit(
    client, db_session, clock, storage
):
    _, _, _, owner_headers, base, command, submitted = await review(
        client, db_session, clock, storage
    )
    _, headers = await account(db_session, clock, operator=True)
    case = submitted["review_case"]
    claimed = await claim(client, case, headers)
    decision = {
        "request_id": str(uuid4()),
        "version": claimed["version"],
        "revision_id": case["revision_id"],
        "decision": "RETURNED",
        "reason": "",
    }
    assert (
        await client.post(
            f"{OPS}/{case['id']}/decision", headers=headers, json=decision
        )
    ).status_code == 422
    returned = await client.post(
        f"{OPS}/{case['id']}/decision",
        headers=headers,
        json={**decision, "reason": "请补充运行说明"},
    )
    assert returned.status_code == 200
    workflow = (await client.get(f"{base}/workflow", headers=owner_headers)).json()
    assert (
        workflow["first_review_state"] == "RETURNED"
        and workflow["review_case"]["reason"] == "请补充运行说明"
    )
    command.update(request_id=str(uuid4()), workflow_version=workflow["version"])
    retry = await client.post(f"{base}/submit", headers=owner_headers, json=command)
    assert retry.status_code == 200 and retry.json()["review_case"]["id"] != case["id"]


@pytest.mark.parametrize("failure", ["audit", "notification"])
@pytest.mark.parametrize("operation", ["SUBMIT", "UPDATE"])
async def test_failed_decision_dependency_rolls_back_decision_and_notification(
    db_session, clock, storage, client, failure, operation
):
    from app.modules.audit.service import AuditLogWriter
    from app.modules.identity.enums import Role
    from app.modules.identity.events import Actor
    from app.modules.innovation.review_models import (
        AchievementReviewCase,
        AchievementWorkflow,
    )
    from app.modules.innovation.review_operations_service import ReviewOperationsService
    from app.modules.innovation.review_schemas import ReviewDecisionCommand
    from app.modules.notifications.models import Notification
    from app.modules.notifications.port import NotificationPort

    class FailedAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    class FailedNotification(NotificationPort):
        async def record_event(self, *args, **kwargs):
            raise RuntimeError("notification persistence unavailable")

    owner, _, achievement, owner_headers, base, command, submitted = await review(
        client, db_session, clock, storage
    )
    operator, headers = await account(db_session, clock, operator=True)
    old_public_id = None
    if operation == "UPDATE":
        from tests.integration.innovation.test_update_review import decide

        await decide(client, submitted, headers)
        current = (await client.get(f"{base}/workflow", headers=owner_headers)).json()
        old_public_id = UUID(current["public_revision_id"])
        command.update(request_id=str(uuid4()), workflow_version=current["version"])
        pending = await client.post(
            f"{base}/submit-update", headers=owner_headers, json=command
        )
        assert pending.status_code == 200
        submitted = pending.json()
    claimed = await claim(client, submitted["review_case"], headers)
    aid, oid, uid, cid = achievement.id, operator.id, owner.id, UUID(claimed["id"])
    service = ReviewOperationsService(
        clock=clock,
        storage=storage,
        audit=FailedAudit() if failure == "audit" else None,
        notifications=FailedNotification(clock=clock)
        if failure == "notification"
        else None,
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await service.decision(
            db_session,
            actor=Actor(user_id=oid, role=Role.STUDENT),
            case_id=cid,
            payload=ReviewDecisionCommand(
                request_id=uuid4(),
                version=claimed["version"],
                revision_id=UUID(claimed["revision_id"]),
                decision="APPROVED",
            ),
        )
    await db_session.rollback()
    case = await db_session.get(AchievementReviewCase, cid)
    workflow = await db_session.get(AchievementWorkflow, aid)
    assert case.status == "SUBMITTED" and workflow.public_revision_id == old_public_id
    notifications = list(
        await db_session.scalars(
            select(Notification).where(Notification.user_id == uid)
        )
    )
    assert len(notifications) == (1 if operation == "UPDATE" else 0)


async def test_failed_snapshot_or_file_audit_never_returns_private_data(
    db_session, clock, storage, client
):
    from app.modules.audit.service import AuditLogWriter
    from app.modules.identity.enums import Role
    from app.modules.identity.events import Actor
    from app.modules.innovation.review_operations_service import ReviewOperationsService

    class FailedAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("private audit unavailable")

    _, _, _, _, _, command, submitted = await review(client, db_session, clock, storage)
    operator, headers = await account(db_session, clock, operator=True)
    oid = operator.id
    claimed = await claim(client, submitted["review_case"], headers)
    args = {
        "actor": Actor(user_id=oid, role=Role.STUDENT),
        "case_id": UUID(claimed["id"]),
    }
    service = ReviewOperationsService(clock=clock, storage=storage, audit=FailedAudit())
    with pytest.raises(RuntimeError, match="private audit unavailable"):
        await service.detail(db_session, **args)
    await db_session.rollback()
    with pytest.raises(RuntimeError, match="private audit unavailable"):
        await service.read_content(
            db_session, **args, evidence_id=UUID(command["evidence_ids"][0])
        )
