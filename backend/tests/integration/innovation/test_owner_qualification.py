"""Real HTTP qualification flow; identity application is not achievement review."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.clock import FrozenClock
from app.core.errors import BusinessError
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditLogWriter
from app.modules.identity.dependencies import get_access_token_codec, get_business_clock
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.identity.models import TotpCredential, User, UserSession
from app.modules.identity.routing_common import REFRESH_COOKIE_NAME
from app.modules.identity.session_service import SessionService
from app.modules.innovation.models import OwnerProfile, OwnerQualification
from app.modules.innovation.owner_schemas import OwnerProfileSave
from app.modules.innovation.owner_service import OwnerProfileService
from app.modules.innovation.qualification_schemas import (
    QualificationApply,
    QualificationApprove,
)
from app.modules.innovation.qualification_service import OwnerQualificationService
from app.modules.system.service import SystemSettingService

pytestmark = pytest.mark.integration
SELF = "/api/v1/ie/me/owner-qualification"
ADMIN = "/api/v1/admin/ie/owner-qualifications"
PROFILE = "/api/v1/ie/me/owner-profile"
FIELDS = {
    "name": "资格示例",
    "student_no": "00156789",
    "major": "示例专业",
    "grade": "2026级",
}


@pytest.fixture
def clock():
    return FrozenClock(datetime.now(UTC).replace(microsecond=0))


@pytest_asyncio.fixture
async def client(db_session, clock) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def db():
        yield db_session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[get_business_clock] = lambda: clock
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def person(db, clock, role=Role.STUDENT, *, totp=True):
    row = User(
        username=f"oq-{uuid4().hex}",
        password_hash="unused",
        nickname="资格测试",
        role=role.value,
        status="ACTIVE",
    )
    db.add(row)
    await db.flush()
    if role == Role.ADMIN and totp:
        db.add(
            TotpCredential(
                user_id=row.id, secret_encrypted=b"unused", confirmed_at=clock.now()
            )
        )
        await db.flush()
    _, token = await SessionService(
        clock=clock, access_codec=get_access_token_codec()
    ).issue_session(db, user=row, now=clock.now())
    return row, {"Authorization": f"Bearer {token.access_token}"}


async def test_real_http_application_and_admin_activation(client, db_session, clock):
    student, sh = await person(db_session, clock)
    admin, ah = await person(db_session, clock, Role.ADMIN)
    initial = await client.get(SELF, headers=sh)
    assert initial.status_code == 200
    assert initial.json()["status"] == "NOT_APPLIED"
    assert (
        await client.post(SELF, headers=sh, json={"version": 0, "profile_version": 1})
    ).status_code == 409
    assert (
        await client.put(PROFILE, headers=sh, json={**FIELDS, "version": 0})
    ).status_code == 200
    assert (await client.get(SELF, headers=sh)).json()["status"] == "NOT_APPLIED"
    applied = await client.post(
        SELF, headers=sh, json={"version": 0, "profile_version": 1}
    )
    assert applied.status_code == 200 and applied.json()["status"] == "PENDING"
    assert applied.json()["version"] == 1
    assert applied.headers["cache-control"] == "private, no-store"
    queue = await client.get(ADMIN, headers=ah)
    assert queue.status_code == 200 and queue.json()["total"] == 1
    assert queue.json()["items"][0]["user_id"] == str(student.id)
    detail = await client.get(f"{ADMIN}/{student.id}", headers=ah)
    assert detail.status_code == 200 and detail.json()["profile"] == FIELDS
    assert detail.headers["cache-control"] == "private, no-store"
    approved = await client.post(
        f"{ADMIN}/{student.id}/approve", headers=ah, json={"version": 1}
    )
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    assert approved.json()["version"] == 2
    assert (await client.get(SELF, headers=sh)).json()["status"] == "APPROVED"
    assert (await client.get("/api/v1/ie/me/capabilities", headers=sh)).json() == {
        "operations_enabled": False
    }
    assert (await client.get("/api/v1/me", headers=sh)).json()["role"] == "STUDENT"
    assert (
        await client.post(
            f"{ADMIN}/{student.id}/approve", headers=ah, json={"version": 1}
        )
    ).status_code == 409
    assert (
        await client.post(SELF, headers=sh, json={"version": 2, "profile_version": 1})
    ).status_code == 409
    logs = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.target_type == "ie_owner_qualification")
        )
    )
    assert {log.action for log in logs} >= {
        "IE_OWNER_QUALIFICATION_APPLY",
        "IE_OWNER_QUALIFICATION_REVEAL",
        "IE_OWNER_QUALIFICATION_APPROVE",
    }
    for log in logs:
        assert log.reason
        if log.action == "IE_OWNER_QUALIFICATION_REVEAL":
            assert log.before_snapshot is None and log.after_snapshot is None
        assert not any(
            value
            in repr((log.details, log.before_snapshot, log.after_snapshot, log.reason))
            for value in FIELDS.values()
        )


async def test_snapshot_update_and_stale_admin_decision(client, db_session, clock):
    student, sh = await person(db_session, clock)
    _, ah = await person(db_session, clock, Role.ADMIN)
    await client.put(PROFILE, headers=sh, json={**FIELDS, "version": 0})
    applied = await client.post(
        SELF, headers=sh, json={"version": 0, "profile_version": 1}
    )
    assert applied.status_code == 200
    await client.put(
        PROFILE, headers=sh, json={**FIELDS, "major": "已保存的新专业", "version": 1}
    )
    old = await client.get(f"{ADMIN}/{student.id}", headers=ah)
    assert old.json()["profile"]["major"] == FIELDS["major"]
    assert (
        await client.post(SELF, headers=sh, json={"version": 1, "profile_version": 1})
    ).status_code == 409
    updated = await client.post(
        SELF, headers=sh, json={"version": 1, "profile_version": 2}
    )
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert (
        await client.post(
            f"{ADMIN}/{student.id}/approve", headers=ah, json={"version": 1}
        )
    ).status_code == 409
    assert (await client.get(f"{ADMIN}/{student.id}", headers=ah)).json()["profile"][
        "major"
    ] == "已保存的新专业"
    assert (
        await client.post(
            f"{ADMIN}/{student.id}/approve", headers=ah, json={"version": 2}
        )
    ).status_code == 200
    await client.put(
        PROFILE, headers=sh, json={**FIELDS, "grade": "资料更新", "version": 2}
    )
    assert (await client.get(SELF, headers=sh)).json()["status"] == "APPROVED"


async def test_authority_and_private_field_boundaries(client, db_session, clock):
    student, sh = await person(db_session, clock)
    other, oh = await person(db_session, clock)
    _, ah = await person(db_session, clock, Role.ADMIN)
    await client.put(PROFILE, headers=sh, json={**FIELDS, "version": 0})
    assert (
        await client.post(SELF, headers=sh, json={"version": 0, "profile_version": 1})
    ).status_code == 200
    assert (await client.get(SELF, headers=oh)).json()["status"] == "NOT_APPLIED"
    for path in (SELF, ADMIN, f"{ADMIN}/{student.id}"):
        assert (await client.get(path)).status_code == 401
    for path in (ADMIN, f"{ADMIN}/{student.id}"):
        assert (await client.get(path, headers=sh)).status_code == 403
    assert (await client.get(f"{SELF}/{student.id}", headers=oh)).status_code == 404
    assert (await client.get(f"{ADMIN}/{other.id}", headers=ah)).status_code == 404
    for response in (
        (await client.get(SELF, headers=sh)).json(),
        (await client.get(ADMIN, headers=ah)).json(),
    ):
        assert not any(value in repr(response) for value in FIELDS.values())
    for role in (Role.TEACHER, Role.ADMIN):
        _, wrong = await person(db_session, clock, role)
        assert (await client.get(SELF, headers=wrong)).status_code == 403
    _, no_totp = await person(db_session, clock, Role.ADMIN, totp=False)
    assert (await client.get(ADMIN, headers=no_totp)).status_code == 403
    student.status = "SUSPENDED"
    await db_session.flush()
    assert (await client.get(SELF, headers=sh)).status_code == 403


async def test_extra_fields_and_management_network(client, db_session, clock):
    student, sh = await person(db_session, clock)
    admin, ah = await person(db_session, clock, Role.ADMIN)
    for extra in (
        {"qualified": True},
        {"role": "ADMIN"},
        {"user_id": str(student.id)},
        {"name": "不能夹带资料"},
    ):
        assert (
            await client.post(
                SELF, headers=sh, json={"version": 0, "profile_version": 1, **extra}
            )
        ).status_code == 422
    settings = SystemSettingService()
    administrator = Actor(user_id=admin.id, role=Role.ADMIN)
    await settings.set_management_network_policy(
        db_session, actor=administrator, cidrs=["192.0.2.0/24"], reason="测试管理网络"
    )
    await settings.set_management_network_policy(
        db_session, actor=administrator, enabled=True, reason="测试管理网络"
    )
    for path in (ADMIN, f"{ADMIN}/{student.id}"):
        assert (await client.get(path, headers=ah)).status_code == 403
    assert (
        await client.post(
            f"{ADMIN}/{student.id}/approve", headers=ah, json={"version": 1}
        )
    ).status_code == 403


async def test_failed_audit_prevents_apply_reveal_and_approval(db_session, clock):
    student, _ = await person(db_session, clock)
    admin, _ = await person(db_session, clock, Role.ADMIN)
    sa, aa = (
        Actor(user_id=student.id, role=Role.STUDENT),
        Actor(user_id=admin.id, role=Role.ADMIN),
    )
    await OwnerProfileService(clock=clock).save(
        db_session, actor=sa, payload=OwnerProfileSave(**FIELDS, version=0)
    )

    class FailedAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("audit unavailable")

    failing = OwnerQualificationService(clock=clock, audit=FailedAudit())
    payload = QualificationApply(version=0, profile_version=1)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await failing.apply(db_session, actor=sa, payload=payload)
    await db_session.rollback()
    assert await db_session.get(OwnerQualification, sa.user_id) is None
    await OwnerQualificationService(clock=clock).apply(
        db_session, actor=sa, payload=payload
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await failing.reveal(db_session, actor=aa, user_id=sa.user_id)
    await db_session.rollback()
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await failing.approve(
            db_session,
            actor=aa,
            user_id=sa.user_id,
            payload=QualificationApprove(version=1),
        )
    await db_session.rollback()
    row = await db_session.get(OwnerQualification, sa.user_id)
    assert row is not None and row.status == "PENDING" and row.version == 1
    assert row.approved_at is None and row.approved_by is None


@pytest.mark.parametrize("action", ["apply", "approve"])
async def test_independent_pg_connections_transition_once(db_engine, clock, action):
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    ids = []
    try:
        async with sessions() as seed:
            student, _ = await person(seed, clock)
            admins = [await person(seed, clock, Role.ADMIN) for _ in range(2)]
            ids = [student.id, *(a.id for a, _ in admins)]
            sa = Actor(user_id=student.id, role=Role.STUDENT)
            aa = [Actor(user_id=a.id, role=Role.ADMIN) for a, _ in admins]
            await OwnerProfileService(clock=clock).save(
                seed, actor=sa, payload=OwnerProfileSave(**FIELDS, version=0)
            )
            if action == "approve":
                await OwnerQualificationService(clock=clock).apply(
                    seed,
                    actor=sa,
                    payload=QualificationApply(version=0, profile_version=1),
                )
        barrier = asyncio.Barrier(2)
        connections = set()

        async def transition(index):
            async with sessions() as db:
                connections.add(await db.scalar(text("select pg_backend_pid()")))
                await asyncio.wait_for(barrier.wait(), timeout=10)
                service = OwnerQualificationService(clock=clock)
                try:
                    if action == "apply":
                        result = await service.apply(
                            db,
                            actor=sa,
                            payload=QualificationApply(version=0, profile_version=1),
                        )
                    else:
                        result = await service.approve(
                            db,
                            actor=aa[index],
                            user_id=sa.user_id,
                            payload=QualificationApprove(version=1),
                        )
                    return result.status
                except BusinessError as exc:
                    await db.rollback()
                    return exc.status_code

        results = await asyncio.gather(transition(0), transition(1))
        assert len(connections) == 2
        assert sorted(map(str, results)) == [
            "409",
            "PENDING" if action == "apply" else "APPROVED",
        ]
        async with sessions() as reader:
            row = await reader.get(OwnerQualification, ids[0])
            assert row is not None and row.version == (1 if action == "apply" else 2)
            logs = list(
                await reader.scalars(
                    select(AuditLog).where(
                        AuditLog.target_type == "ie_owner_qualification",
                        AuditLog.target_id == str(ids[0]),
                    )
                )
            )
            target_action = (
                "IE_OWNER_QUALIFICATION_APPLY"
                if action == "apply"
                else "IE_OWNER_QUALIFICATION_APPROVE"
            )
            assert len([log for log in logs if log.action == target_action]) == 1
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                delete(OwnerQualification).where(OwnerQualification.user_id.in_(ids))
            )
            await cleanup.execute(
                delete(OwnerProfile).where(OwnerProfile.user_id.in_(ids))
            )
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.actor_user_id.in_(ids))
            )
            await cleanup.execute(
                delete(UserSession).where(UserSession.user_id.in_(ids))
            )
            await cleanup.execute(
                delete(TotpCredential).where(TotpCredential.user_id.in_(ids))
            )
            await cleanup.execute(delete(User).where(User.id.in_(ids)))
            await cleanup.commit()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "UNKNOWN"},
        {"version": 0},
        {"profile_version": 0},
        {"name": " "},
        {"status": "APPROVED"},
        {"approved_at": datetime.now(UTC)},
    ],
)
async def test_qualification_database_constraints(db_session, clock, changes):
    student, _ = await person(db_session, clock)
    with pytest.raises(DBAPIError) as refused:
        async with db_session.begin_nested():
            db_session.add(
                OwnerQualification(
                    **{
                        "user_id": student.id,
                        "status": "PENDING",
                        "version": 1,
                        "profile_version": 1,
                        **FIELDS,
                        "requested_at": clock.now(),
                        **changes,
                    }
                )
            )
            await db_session.flush()
    assert refused.value.orig.sqlstate == "23514"


async def test_refresh_cookie_alone_cannot_read_or_apply_qualification(
    client, db_session, clock
):
    student, bearer = await person(db_session, clock)
    saved = await client.put(PROFILE, headers=bearer, json={**FIELDS, "version": 0})
    assert saved.status_code == 200
    assert (await client.get(SELF, headers=bearer)).status_code == 200
    _, tokens = await SessionService(
        clock=clock, access_codec=get_access_token_codec()
    ).issue_session(db_session, user=student, now=clock.now())
    client.cookies.set(REFRESH_COOKIE_NAME, tokens.refresh_token)
    try:
        read = await client.get(SELF)
        applied = await client.post(SELF, json={"version": 0, "profile_version": 1})
        for response in (read, applied):
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
        assert await db_session.get(OwnerQualification, student.id) is None
    finally:
        client.cookies.clear()


async def test_operations_student_cannot_manage_owner_qualifications(
    client, db_session, clock
):
    applicant, applicant_headers = await person(db_session, clock)
    operator, operator_headers = await person(db_session, clock)
    _, admin_headers = await person(db_session, clock, Role.ADMIN)
    saved = await client.put(
        PROFILE, headers=applicant_headers, json={**FIELDS, "version": 0}
    )
    assert saved.status_code == 200
    applied = await client.post(
        SELF, headers=applicant_headers, json={"version": 0, "profile_version": 1}
    )
    assert applied.status_code == 200
    granted = await client.put(
        f"/api/v1/admin/ie/operations-grants/{operator.id}",
        headers=admin_headers,
        json={"enabled": True, "version": 0, "reason": "授权权限回归测试运营"},
    )
    assert granted.status_code == 200 and granted.json()["enabled"] is True
    assert (
        await client.get("/api/v1/ie/me/capabilities", headers=operator_headers)
    ).json() == {"operations_enabled": True}
    assert (await client.get("/api/v1/me", headers=operator_headers)).json()[
        "role"
    ] == "STUDENT"

    queue = await client.get(ADMIN, headers=operator_headers)
    detail = await client.get(f"{ADMIN}/{applicant.id}", headers=operator_headers)
    approved = await client.post(
        f"{ADMIN}/{applicant.id}/approve",
        headers=operator_headers,
        json={"version": 1},
    )
    for response in (queue, detail, approved):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
        assert not any(value in repr(response.json()) for value in FIELDS.values())
    row = await db_session.get(OwnerQualification, applicant.id)
    assert row is not None and row.status == "PENDING" and row.version == 1
    reveals = list(
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.actor_user_id == operator.id,
                AuditLog.action == "IE_OWNER_QUALIFICATION_REVEAL",
            )
        )
    )
    assert reveals == []
