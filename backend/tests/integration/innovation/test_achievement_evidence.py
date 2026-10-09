"""Owner evidence lifecycle, actual sessions/PG and external-boundary fakes.

Mutations: foreign ownership accepted; unchecked bytes READY; missing audit
before private reads; scan failure swallowed; expired/replaced bytes reused.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from app.core.clock import FrozenClock
from app.db.session import get_db_session
from app.integrations.evidence_scanner import (
    EvidenceCheckUnavailableError,
    EvidenceThreatError,
)
from app.main import create_app
from app.modules.identity.dependencies import get_access_token_codec, get_business_clock
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import (
    AchievementDraft,
    OwnerQualification,
    ProjectDraft,
)
from tests.fakes.integrations import FakeObjectStorage

pytestmark = pytest.mark.integration
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class Scanner:
    failure = None

    def scan(self, content):
        if self.failure:
            raise self.failure


async def world(db, clock, *, approved=True):
    owner = User(
        username=f"evidence-{uuid4().hex}",
        password_hash="unused",
        nickname="本人",
        role="STUDENT",
        status="ACTIVE",
    )
    db.add(owner)
    await db.flush()
    parent = ProjectDraft(
        owner_user_id=owner.id,
        creation_request_id=uuid4(),
        creation_payload_fingerprint="0" * 64,
        title="私有项目",
    )
    db.add(parent)
    await db.flush()
    achievement = AchievementDraft(
        project_id=parent.id,
        creation_request_id=uuid4(),
        creation_payload_fingerprint="0" * 64,
        title="阶段原型",
    )
    db.add(achievement)
    if approved:
        db.add(
            OwnerQualification(
                user_id=owner.id,
                status="APPROVED",
                version=1,
                profile_version=1,
                name="姓名",
                student_no="000001",
                major="专业",
                grade="2026",
                requested_at=clock.now(),
                approved_at=clock.now(),
                approved_by=owner.id,
            )
        )
    await db.flush()
    token = (
        await SessionService(
            clock=clock, access_codec=get_access_token_codec()
        ).issue_session(db, user=owner, now=clock.now())
    )[1]
    return owner, parent, achievement, {"Authorization": f"Bearer {token.access_token}"}


@pytest.fixture
def clock():
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def scanner():
    return Scanner()


@pytest.fixture
def storage(clock):
    return FakeObjectStorage(clock=clock)


@pytest.fixture
async def client(db_session, clock, scanner, storage):
    from app.modules.innovation.evidence_router import get_evidence_service
    from app.modules.innovation.evidence_service import EvidenceService

    app = create_app()

    async def db():
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    app.dependency_overrides[get_evidence_service] = lambda: EvidenceService(
        clock=clock, storage=storage, scanner=scanner
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as result:
        yield result


def path(parent, achievement):
    return (
        f"/api/v1/ie/me/project-drafts/{parent.id}"
        f"/achievements/{achievement.id}/evidence"
    )


def intent(**changes):
    return {
        "request_id": str(uuid4()),
        "content_type": "application/pdf",
        "size": len(PDF),
        **changes,
    }


async def ready(client, parent, achievement, headers, storage):
    created = await client.post(
        path(parent, achievement), headers=headers, json=intent()
    )
    assert created.status_code == 201, created.text
    response = created.json()
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    completed = await client.post(
        f"{path(parent, achievement)}/{response['evidence']['id']}/complete",
        headers=headers,
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


async def test_real_http_upload_check_and_private_proxy(
    client, db_session, clock, storage
):
    owner, parent, achievement, headers = await world(db_session, clock)
    body = intent()
    created = await client.post(path(parent, achievement), headers=headers, json=body)
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["client_headers"] == {
        "If-None-Match": "*",
        "Content-Type": "application/pdf",
    }
    assert data["pinned_content_length"] == len(PDF)
    eid = data["evidence"]["id"]
    assert data["evidence"]["state"] == "PENDING"
    key = storage.upload_urls[-1].object_key
    assert key.startswith(f"innovation/evidence/{achievement.id}/")
    retry = await client.post(path(parent, achievement), headers=headers, json=body)
    assert retry.status_code == 200 and retry.json() == data
    changed = await client.post(
        path(parent, achievement), headers=headers, json={**body, "size": 1}
    )
    assert changed.status_code == 409
    storage.put_object(object_key=key, content=PDF)
    checked = await client.post(
        f"{path(parent, achievement)}/{eid}/complete", headers=headers
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["state"] == "READY"
    assert checked.json()["sha256"] == hashlib.sha256(PDF).hexdigest()
    content = await client.get(
        f"{path(parent, achievement)}/{eid}/content", headers=headers
    )
    assert content.content == PDF
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["content-disposition"].startswith("attachment;")
    listed = await client.get(path(parent, achievement), headers=headers)
    assert listed.status_code == 200
    assert "object_key" not in listed.text and "upload_url" not in listed.text


async def test_owner_qualification_scope_and_authentication(
    client, db_session, clock, storage
):
    _, pa, aa, ha = await world(db_session, clock)
    _, pb, ab, hb = await world(db_session, clock)
    _, pu, au, hu = await world(db_session, clock, approved=False)
    checked = await ready(client, pa, aa, ha, storage)
    target = f"{path(pa, aa)}/{checked['id']}/content"
    assert (await client.get(target)).status_code == 401
    assert (await client.get(target, headers=hb)).status_code == 404
    assert (
        await client.get(f"{path(pb, ab)}/{checked['id']}/content", headers=hb)
    ).status_code == 404
    assert (
        await client.post(path(pu, au), headers=hu, json=intent())
    ).status_code == 403
    assert (
        await client.post(path(pb, ab), headers=ha, json=intent())
    ).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        intent(size=0),
        intent(size=10485761),
        intent(content_type="application/zip"),
        intent(object_key="forged"),
    ],
)
async def test_invalid_upload_payload_rejected(client, db_session, clock, payload):
    _, parent, achievement, headers = await world(db_session, clock)
    assert (
        await client.post(path(parent, achievement), headers=headers, json=payload)
    ).status_code == 422


async def test_pending_bytes_and_storage_mismatch_never_ready(
    client, db_session, clock, storage
):
    _, parent, achievement, headers = await world(db_session, clock)
    response = (
        await client.post(path(parent, achievement), headers=headers, json=intent())
    ).json()
    base = f"{path(parent, achievement)}/{response['evidence']['id']}"
    assert (await client.get(f"{base}/content", headers=headers)).status_code == 409
    assert (await client.post(f"{base}/complete", headers=headers)).status_code == 409
    storage.put_object(
        object_key=storage.upload_urls[-1].object_key,
        content=b"MZ" + b"x" * (len(PDF) - 2),
    )
    result = await client.post(f"{base}/complete", headers=headers)
    assert result.status_code == 200 and result.json()["state"] == "REJECTED"
    assert (await client.get(f"{base}/content", headers=headers)).status_code == 409


async def test_scanner_failure_retry_expiry_and_quota(
    client, db_session, clock, storage, scanner
):
    _, parent, achievement, headers = await world(db_session, clock)
    item = (
        await client.post(path(parent, achievement), headers=headers, json=intent())
    ).json()["evidence"]
    base = f"{path(parent, achievement)}/{item['id']}"
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    scanner.failure = EvidenceCheckUnavailableError("scanner unavailable")
    failed = await client.post(f"{base}/complete", headers=headers)
    assert failed.status_code == 200 and failed.json()["state"] == "PENDING"
    assert failed.json()["failure_code"] == "CHECK_UNAVAILABLE"
    scanner.failure = EvidenceThreatError("threat")
    rejected = await client.post(f"{base}/complete", headers=headers)
    assert rejected.json()["state"] == "REJECTED"
    for _ in range(4):
        assert (
            await client.post(path(parent, achievement), headers=headers, json=intent())
        ).status_code == 201
    assert (
        await client.post(path(parent, achievement), headers=headers, json=intent())
    ).status_code == 409
    assert (await client.delete(base, headers=headers)).status_code == 204
    fresh = await client.post(path(parent, achievement), headers=headers, json=intent())
    assert fresh.status_code == 201
    latest = storage.upload_urls[-1]
    storage.put_object(object_key=latest.object_key, content=PDF)
    from app.modules.innovation.evidence_models import AchievementEvidence

    current = fresh.json()["evidence"]
    await db_session.execute(
        update(AchievementEvidence)
        .where(AchievementEvidence.id == current["id"])
        .values(expires_at=clock.now() - timedelta(seconds=1))
    )
    assert (
        await client.post(
            f"{path(parent, achievement)}/{current['id']}/complete", headers=headers
        )
    ).status_code == 409


async def test_modified_bytes_after_check_and_failed_read_audit_block_download(
    db_session, clock, storage, scanner
):
    from app.core.errors import BusinessError
    from app.modules.audit.service import AuditLogWriter
    from app.modules.innovation.evidence_schemas import EvidenceIntentCreate
    from app.modules.innovation.evidence_service import EvidenceService

    owner, parent, achievement, _ = await world(db_session, clock)
    actor = Actor(user_id=owner.id, role=Role.STUDENT)
    service = EvidenceService(clock=clock, storage=storage, scanner=scanner)
    result, _ = await service.create_intent(
        db_session,
        actor=actor,
        project_id=parent.id,
        achievement_id=achievement.id,
        payload=EvidenceIntentCreate(**intent()),
    )
    key = storage.upload_urls[-1].object_key
    storage.put_object(object_key=key, content=PDF)
    await service.complete(
        db_session,
        actor=actor,
        project_id=parent.id,
        achievement_id=achievement.id,
        evidence_id=result.evidence.id,
    )
    storage.put_object(object_key=key, content=PDF.replace(b"<<>>", b"<xx>"))
    with pytest.raises(BusinessError) as error:
        await service.read_content(
            db_session,
            actor=actor,
            project_id=parent.id,
            achievement_id=achievement.id,
            evidence_id=result.evidence.id,
        )
    assert error.value.status_code == 409

    class FailingAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    storage.put_object(object_key=key, content=PDF)
    guarded = EvidenceService(
        clock=clock, storage=storage, scanner=scanner, audit=FailingAudit()
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await guarded.read_content(
            db_session,
            actor=actor,
            project_id=parent.id,
            achievement_id=achievement.id,
            evidence_id=result.evidence.id,
        )


async def test_transient_check_can_retry_clean_and_ready_is_idempotent(
    client,
    db_session,
    clock,
    storage,
    scanner,
):
    _, parent, achievement, headers = await world(db_session, clock)
    item = (
        await client.post(path(parent, achievement), headers=headers, json=intent())
    ).json()["evidence"]
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    base = f"{path(parent, achievement)}/{item['id']}/complete"
    scanner.failure = EvidenceCheckUnavailableError("test unavailable")
    assert (await client.post(base, headers=headers)).json()["state"] == "PENDING"
    scanner.failure = None
    clean = await client.post(base, headers=headers)
    assert clean.json()["state"] == "READY"
    scanner.failure = EvidenceThreatError("must not rescan a READY object")
    repeated = await client.post(base, headers=headers)
    assert repeated.json() == clean.json()


async def test_finish_audit_failure_never_commits_ready(
    db_session, clock, storage, scanner
):
    from app.modules.audit.service import AuditLogWriter
    from app.modules.innovation.evidence_models import AchievementEvidence
    from app.modules.innovation.evidence_schemas import EvidenceIntentCreate
    from app.modules.innovation.evidence_service import EvidenceService

    class FinishAuditFailure(AuditLogWriter):
        async def append(self, db, *, action, **kwargs):
            if action == "IE_EVIDENCE_CHECK_FINISH":
                raise RuntimeError("finish audit unavailable")
            return await super().append(db, action=action, **kwargs)

    owner, parent, achievement, _ = await world(db_session, clock)
    actor = Actor(user_id=owner.id, role=Role.STUDENT)
    service = EvidenceService(
        clock=clock, storage=storage, scanner=scanner, audit=FinishAuditFailure()
    )
    args = {"actor": actor, "project_id": parent.id, "achievement_id": achievement.id}
    result, _ = await service.create_intent(
        db_session, **args, payload=EvidenceIntentCreate(**intent())
    )
    storage.put_object(object_key=storage.upload_urls[-1].object_key, content=PDF)
    eid = result.evidence.id
    with pytest.raises(RuntimeError, match="finish audit unavailable"):
        await service.complete(db_session, **args, evidence_id=eid)
    await db_session.rollback()
    row = await db_session.get(AchievementEvidence, eid)
    assert row.state == "CHECKING" and row.sha256 is None
