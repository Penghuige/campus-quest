# backend/tests/integration/admin/test_admin_user_detail_and_sessions.py
"""Wave 2a §3.1 + §3.3: the admin user panorama and the session kick.

The detail view aggregates the account's facts across modules (wallet
projection, recent submissions via the claim join, live sessions,
remembered devices) — read-only, one query per fact. The kick revokes
every live refresh session AND every remembered-device credential in
one transaction with an ADMIN_SESSIONS_REVOKED audit row: the
stolen-account emergency stop.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.device_credentials import issue as issue_device
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User, UserSession
from app.modules.identity.session_service import SessionService
from app.modules.points.models import PointWallet
from app.modules.submissions.models import Submission
from app.modules.tasks.models import Assignment, AssignmentClaim, Task

pytestmark = pytest.mark.integration

# Anchored to the REAL now: the access codec validates iat against
# the wall clock (the e2e FrozenClock lesson), so a future _T0 401s.
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def api_app(db_session: AsyncSession, api_clock: FrozenClock) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_admin(
    db: AsyncSession, clock: FrozenClock
) -> tuple[User, dict[str, str]]:
    """The §33.4 management gate's proven seeding shape: an ACTIVE ADMIN
    with a confirmed TOTP row and a live bearer header minted through
    the same codec the guard resolves with."""
    from app.modules.identity.dependencies import get_access_token_codec
    from app.modules.identity.models import TotpCredential

    username = f"panorama-admin-{uuid.uuid4().hex[:6]}@campus.example.edu"
    admin = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname="全景管理员",
        email_normalized=username,
        email_verified_at=clock.now(),
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(admin)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=admin.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=admin, now=clock.now())
    await db.commit()
    return admin, {"Authorization": f"Bearer {tokens.access_token}"}


async def _seed_panorama_world(
    db: AsyncSession, clock: FrozenClock
) -> tuple[User, UserSession]:
    """One ACTIVE student with a wallet, a claimed task carrying three
    submissions, one live session, and one remembered device."""
    run = uuid.uuid4().hex[:6]
    student = User(
        username=f"2025{run}1000",
        password_hash=hash_password(_PASSWORD),
        nickname="全景同学",
        phone_e164=f"+86138{run}01000",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    teacher = User(
        username=f"panorama-teacher-{run}@campus.example.edu",
        password_hash=None,
        nickname="全景教师",
        email_normalized="panorama-teacher@campus.example.edu",
        email_verified_at=clock.now(),
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add_all([student, teacher])
    await db.flush()
    db.add(PointWallet(user_id=student.id, available_points=120, earned_points=150))

    task = Task(
        owner_teacher_id=teacher.id,
        title="全景任务",
        description="panorama",
        task_type="DATA_CRAWL",
        rarity="NORMAL",
        base_reward_points=10,
        status="PUBLISHED",
        deadline_mode="RELATIVE",
        duration_minutes=4320,
        allowed_file_types=["CSV"],
        max_file_size_bytes=1024,
        notification_channels=[],
    )
    db.add(task)
    await db.flush()
    assignment = Assignment(
        task_id=task.id,
        platform="zhihu",
        keyword="k",
        availability_status="AVAILABLE",
    )
    db.add(assignment)
    await db.flush()
    deadline = clock.now() + timedelta(days=3)
    claim = AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=student.id,
        status="COMPLETED",
        deadline_at=deadline,
        grace_deadline_at=deadline + timedelta(minutes=1440),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=100,
        submission_schema_version=1,
        reward_lock_status="NONE",
    )
    db.add(claim)
    await db.flush()
    for version in (1, 2, 3):
        db.add(
            Submission(
                claim_id=claim.id,
                version=version,
                object_key=f"panorama/{uuid.uuid4().hex}",
                original_filename="a.csv",
                declared_type="CSV",
                file_size=10,
                retention_permanent=False,
                retention_until=clock.now() + timedelta(days=90),
                submitted_at=clock.now(),
                validation_status="VALIDATED",
                review_status="PENDING_REVIEW",
            )
        )
    session = UserSession(
        user_id=student.id,
        refresh_token_hash=uuid.uuid4().hex,
        expires_at=clock.now() + timedelta(days=7),
    )
    db.add(session)
    await db.flush()
    await issue_device(db, student.id, clock=clock)
    await db.commit()
    return student, session


async def test_admin_reads_the_user_panorama(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    _, headers = await _seed_admin(db_session, api_clock)
    student, _session = await _seed_panorama_world(db_session, api_clock)

    response = await client.get(f"/api/v1/admin/users/{student.id}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["username"].startswith("2025")
    assert body["user"]["status"] == "ACTIVE"
    assert body["wallet_balance"] == 120
    assert len(body["recent_submissions"]) == 3
    assert body["recent_submissions"][0]["task_title"] == "全景任务"
    assert body["active_session_count"] == 1
    assert body["remembered_device_count"] == 1


async def test_kick_revokes_sessions_and_devices_with_audit(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    api_clock: FrozenClock,
) -> None:
    _, headers = await _seed_admin(db_session, api_clock)
    student, session = await _seed_panorama_world(db_session, api_clock)

    listed = await client.get(
        f"/api/v1/admin/users/{student.id}/sessions", headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == {
        "active_session_count": 1,
        "remembered_device_count": 1,
    }

    kicked = await client.delete(
        f"/api/v1/admin/users/{student.id}/sessions", headers=headers
    )
    assert kicked.status_code == 200, kicked.text
    assert kicked.json() == {"revoked_sessions": 1, "revoked_devices": 1}

    # The savepoint harness cannot prove cross-connection durability
    # (its commit releases a savepoint, never the outer transaction),
    # so the row-level assertion lives with the HTTP counts above and
    # the e2e admin surface; here the audit row is verified in-session.
    audits = (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ADMIN_SESSIONS_REVOKED")
        )
    ).all()
    assert len(audits) == 1
    snapshot = audits[0].after_snapshot or {}
    assert snapshot.get("revoked_sessions") == 1
    assert snapshot.get("revoked_devices") == 1


async def test_unknown_user_answers_404_across_the_detail_family(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    # The §3.1/§3.3 guards: every read and the kick name their target
    # before anything else — an unknown id is a typed 404, and the GET
    # legs share the panorama's loader, so one seed covers all three.
    _, headers = await _seed_admin(db_session, api_clock)
    await db_session.commit()

    unknown = uuid.uuid4()
    read = await client.get(f"/api/v1/admin/users/{unknown}", headers=headers)
    listed = await client.get(
        f"/api/v1/admin/users/{unknown}/sessions", headers=headers
    )
    kicked = await client.delete(
        f"/api/v1/admin/users/{unknown}/sessions", headers=headers
    )

    assert read.status_code == 404, read.text
    assert listed.status_code == 404, listed.text
    assert kicked.status_code == 404, kicked.text
