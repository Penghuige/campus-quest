# backend/tests/integration/identity/test_approval_gate_api.py
"""The approval gate + admin verdicts (V1.1 spec 2026-10-16 §2).

A PENDING_APPROVAL student logs in and browses, but every acting
surface answers 403 APPROVAL_PENDING until the admin approves; the
verdicts themselves are audited state transitions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

pytestmark = pytest.mark.integration

_GATE_REDIS_DB = 6
_T0 = datetime.now(UTC).replace(microsecond=0)
_ADMIN_EMAIL = "gate-admin@campus.example.edu"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_GATE_REDIS_DB}"))


@pytest_asyncio.fixture
async def api_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_test_redis_url(), decode_responses=True)
    try:
        await client.flushdb()
        yield client
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def api_email() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_email: FakeEmailSender,
    fake_limiter: FakeRateLimiter,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_identity_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_email_sender] = lambda: api_email
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_admin(db: AsyncSession) -> User:
    admin = User(
        username=_ADMIN_EMAIL,
        password_hash=None,
        nickname="审批管理员",
        email_normalized=_ADMIN_EMAIL,
        email_verified_at=_T0,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(admin)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=admin.id,
            secret_encrypted=Fernet(
                get_settings().totp_encryption_key.encode()
            ).encrypt(b"GATEADMINSECRET234567"),
            confirmed_at=_T0,
        )
    )
    await db.flush()
    return admin


async def _seed_pending_student(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        password_hash=hash_password("correct-horse-battery"),
        nickname="待审同学",
        phone_e164=f"+86138001{username[-5:]}",
        role=Role.STUDENT,
        status=UserStatus.PENDING_APPROVAL,
    )
    db.add(user)
    await db.flush()
    return user


async def _mint_bearer(db: AsyncSession, user: User) -> str:
    from app.core.security import AccessTokenCodec
    from app.modules.identity.session_service import SessionService

    sessions = SessionService(
        clock=FrozenClock(_T0),
        access_codec=AccessTokenCodec(
            secret=get_settings().token_secret,
            ttl_minutes=get_settings().access_token_ttl_minutes,
        ),
    )
    _, tokens = await sessions.issue_session(db, user=user, now=_T0)
    await db.commit()
    return tokens.access_token


def _envelope(response: httpx.Response) -> dict:
    return response.json()["error"]


async def test_pending_student_frozen_across_acting_surfaces(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student = await _seed_pending_student(db_session, "20250015551")
    bearer = await _mint_bearer(db_session, student)
    headers = {"Authorization": f"Bearer {bearer}"}

    # Browsing stays open.
    tasks_listed = await client.get("/api/v1/tasks", headers=headers)
    assert tasks_listed.status_code == 200, tasks_listed.text

    # Claim: 403 APPROVAL_PENDING (needs a published task; the guard
    # fires before any task lookup, so a random id exposes exactly the
    # gate's answer).
    claim = await client.post(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000001/claim",
        headers=headers,
        json={},
    )
    assert claim.status_code == 403, claim.text
    assert _envelope(claim)["code"] == "APPROVAL_PENDING"

    # Community write: same envelope (the task comment thread).
    comment = await client.post(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000003/comments",
        headers=headers,
        json={"content": "待审不能发", "anonymous": False},
    )
    assert comment.status_code == 403, comment.text
    assert _envelope(comment)["code"] == "APPROVAL_PENDING"

    # Avatar write: same envelope.
    avatar = await client.post("/api/v1/me/avatar", headers=headers)
    assert avatar.status_code == 403, avatar.text
    assert _envelope(avatar)["code"] == "APPROVAL_PENDING"


async def test_admin_approve_unlocks_and_audits(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    student = await _seed_pending_student(db_session, "20250015552")
    admin_bearer = await _mint_bearer(db_session, admin)

    approved = await client.post(
        f"/api/v1/admin/users/{student.id}/approve",
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "ACTIVE"
    assert body["approved_at"] is not None

    db_session.expunge_all()
    reloaded = await db_session.get(User, student.id)
    assert reloaded is not None
    assert reloaded.approved_at is not None
    assert reloaded.status == UserStatus.ACTIVE.value

    actions = set(
        await db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.actor_user_id == admin.id,
                AuditLog.target_type == "user",
            )
        )
    )
    assert "USER_APPROVED" in actions

    # And the claim gate no longer answers APPROVAL_PENDING.
    student_bearer = await _mint_bearer(db_session, reloaded)
    claim = await client.post(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000002/claim",
        headers={"Authorization": f"Bearer {student_bearer}"},
        json={},
    )
    assert claim.status_code != 403 or _envelope(claim)["code"] != "APPROVAL_PENDING"


async def test_admin_reject_bans_with_reason(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    student = await _seed_pending_student(db_session, "20250015553")
    admin_bearer = await _mint_bearer(db_session, admin)

    rejected = await client.post(
        f"/api/v1/admin/users/{student.id}/reject",
        headers={"Authorization": f"Bearer {admin_bearer}"},
        json={"reason": "资料不符"},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "BANNED"
    assert body["rejected_reason"] == "资料不符"

    db_session.expunge_all()
    reloaded = await db_session.get(User, student.id)
    assert reloaded is not None
    assert reloaded.rejected_reason == "资料不符"
    actions = set(
        await db_session.scalars(
            select(AuditLog.action).where(AuditLog.actor_user_id == admin.id)
        )
    )
    assert "USER_REJECTED" in actions


async def test_double_approve_is_a_conflict(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    student = await _seed_pending_student(db_session, "20250015554")
    admin_bearer = await _mint_bearer(db_session, admin)

    first = await client.post(
        f"/api/v1/admin/users/{student.id}/approve",
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/api/v1/admin/users/{student.id}/approve",
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert second.status_code == 409, second.text
    assert _envelope(second)["code"] == "CONFLICT"


async def test_pending_list_via_users_filter(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    await _seed_pending_student(db_session, "20250015555")
    admin_bearer = await _mint_bearer(db_session, admin)

    listed = await client.get(
        "/api/v1/admin/users",
        params={"status": "PENDING_APPROVAL"},
        headers={"Authorization": f"Bearer {admin_bearer}"},
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    usernames = [item["username"] for item in items]
    # Containment, not exclusivity: the shared local test DB may hold
    # committed PENDING rows from the composition smoke's committed-mode
    # runs (CI's fresh DB never does); this test owns only its own seed.
    assert "20250015555" in usernames
    # G11: the identifying contact fact arrives MASKED, never raw.
    assert items[0]["phone_masked"].startswith("+86")
    assert "****" in items[0]["phone_masked"]
