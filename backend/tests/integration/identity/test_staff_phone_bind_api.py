# backend/tests/integration/identity/test_staff_phone_bind_api.py
"""Staff phone binding (V1.1 login iteration, item 1a — the approved
/me/phone/bind contract).

Staff accounts are born without a phone; binding mirrors the student
registration verification craft (E.164 normalize, uniqueness pre-check
on uq_users_phone_e164 BEFORE any SMS burns, 60s cooldown, keyed
digest, 5 attempts) with the PHONE_BIND purpose and a PHONE_BOUND audit
row whose snapshot carries only the masked phone (G11). Students keep
their existing /me/phone/change flow — this surface is staff-only.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
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
from app.modules.identity.models import User
from app.modules.identity.providers import (
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import FakeRateLimiter, FakeSmsSender

pytestmark = pytest.mark.integration

_BIND_REDIS_DB = 12
_T0 = datetime.now(UTC).replace(microsecond=0)
_TEACHER_EMAIL = "bind-teacher@campus.example.edu"
_TEACHER_PASSWORD = "correct-horse-battery"
_STUDENT = "20250019999"
_PHONE_RAW = "13800169999"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_BIND_REDIS_DB}"))


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
def api_sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
    fake_limiter: FakeRateLimiter,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_identity_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_sms_sender] = lambda: api_sms
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_world(db: AsyncSession) -> tuple[User, User]:
    teacher = User(
        username=_TEACHER_EMAIL,
        password_hash=hash_password(_TEACHER_PASSWORD),
        nickname="绑定教师",
        email_normalized=_TEACHER_EMAIL,
        email_verified_at=_T0,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    student = User(
        username=_STUDENT,
        password_hash=None,
        nickname="占号学生",
        phone_e164="+8613800133333",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add_all([teacher, student])
    await db.flush()
    return teacher, student


async def _mint(db: AsyncSession, user: User) -> str:
    from app.core.security import AccessTokenCodec

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


async def test_teacher_binds_phone_with_audit(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    teacher, _ = await _seed_world(db_session)
    headers = {"Authorization": f"Bearer {await _mint(db_session, teacher)}"}

    challenged = await client.post(
        "/api/v1/me/phone/bind", json={"phone": _PHONE_RAW}, headers=headers
    )
    assert challenged.status_code == 200, challenged.text
    assert set(challenged.json()) == {"challenge_id", "expires_at"}
    assert len(api_sms.messages) == 1
    code = api_sms.messages[0].variables["code"]

    confirmed = await client.post(
        "/api/v1/me/phone/bind/confirm",
        json={
            "challenge_id": challenged.json()["challenge_id"],
            "code": code,
        },
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    # The owner sees their own full phone (MePublic's contract); the
    # AUDIT snapshot is the masked surface this test pins next.
    assert confirmed.json()["phone_e164"] == "+8613800169999"

    audit_rows = (
        (
            await db_session.scalars(
                select(AuditLog).where(AuditLog.action == "PHONE_BOUND")
            )
        )
        .all()
    )
    assert len(audit_rows) == 1
    snapshot = audit_rows[0].after_snapshot or {}
    assert re.fullmatch(r"\+\d{2,3}\*{4}\d{4}", str(snapshot.get("phone")))


async def test_student_is_refused_the_bind_surface(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    _, student = await _seed_world(db_session)
    headers = {"Authorization": f"Bearer {await _mint(db_session, student)}"}

    challenged = await client.post(
        "/api/v1/me/phone/bind", json={"phone": _PHONE_RAW}, headers=headers
    )

    assert challenged.status_code == 403, challenged.text
    assert len(api_sms.messages) == 0


async def test_taken_phone_is_a_conflict_before_any_sms(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    teacher, student = await _seed_world(db_session)
    headers = {"Authorization": f"Bearer {await _mint(db_session, teacher)}"}

    challenged = await client.post(
        "/api/v1/me/phone/bind",
        json={"phone": student.phone_e164},
        headers=headers,
    )

    assert challenged.status_code == 409, challenged.text
    assert challenged.json()["error"]["code"] == "PHONE_ALREADY_BOUND"
    assert len(api_sms.messages) == 0


async def test_resend_inside_cooldown_is_429(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    teacher, _ = await _seed_world(db_session)
    headers = {"Authorization": f"Bearer {await _mint(db_session, teacher)}"}

    first = await client.post(
        "/api/v1/me/phone/bind", json={"phone": _PHONE_RAW}, headers=headers
    )
    again = await client.post(
        "/api/v1/me/phone/bind", json={"phone": _PHONE_RAW}, headers=headers
    )

    assert first.status_code == 200, first.text
    assert again.status_code == 429, again.text
