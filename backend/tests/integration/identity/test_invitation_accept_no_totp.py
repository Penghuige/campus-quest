# backend/tests/integration/identity/test_invitation_accept_no_totp.py
"""TEACHER invitation acceptance drops mandatory TOTP (V1.1 §2.3, Task 7).

TEACHER accept: optional password (blank = email-code-only account),
straight to ACTIVE, a COMPLETE session, no TOTP credential row, no
recovery codes — and `totp_required: false` in the response (the
reviewer's contract addendum for the frontend's branch decision).
ADMIN accept: byte-identical to today (mandatory TOTP pending state,
`totp_required: true`).

Review Focus 5 lives here: the passwordless TEACHER account logs in
end to end through the email-code channel (no password ever set).
"""

from __future__ import annotations

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
from app.core.security import hash_refresh_token
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import RecoveryCode, StaffInvitation, TotpCredential
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

pytestmark = pytest.mark.integration

_INVITE_REDIS_DB = 11
_T0 = datetime.now(UTC).replace(microsecond=0)
_OWNER_EMAIL = "invite-owner@campus.example.edu"
_TEACHER_EMAIL = "invite-teacher@campus.example.edu"
_ADMIN_EMAIL = "invite-admin@campus.example.edu"
_PASSWORD = "teacher-correct-horse"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_INVITE_REDIS_DB}"))


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


async def _seed_owner(db: AsyncSession):
    from app.modules.identity.models import User

    owner = User(
        username=_OWNER_EMAIL,
        password_hash="x",
        nickname="邀请管理员",
        email_normalized=_OWNER_EMAIL,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(owner)
    await db.flush()
    return owner


async def _seed_invitation(
    db: AsyncSession, email: str, role: Role, *, created_by
) -> tuple[str, str]:
    """One live invitation row; returns (token, invitation_id)."""
    import secrets

    token = secrets.token_urlsafe(32)
    invitation = StaffInvitation(
        email_normalized=email,
        role=role.value,
        token_hash=hash_refresh_token(token),
        expires_at=_T0.replace(year=_T0.year + 1),
        created_by=created_by,
    )
    db.add(invitation)
    await db.flush()
    return token, str(invitation.id)


async def _accept(
    client: httpx.AsyncClient, token: str, password: str | None
) -> httpx.Response:
    body: dict[str, object] = {"token": token}
    if password is not None:
        body["password"] = password
    return await client.post("/api/v1/auth/staff/invitations/accept", json=body)


async def test_teacher_accept_with_password_is_complete(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Password set: full session, ACTIVE, but NO TOTP credential and NO
    recovery codes — the V1 mandatory onboarding steps are gone."""
    from app.modules.identity.models import User

    owner = await _seed_owner(db_session)
    token, _ = await _seed_invitation(
        db_session, _TEACHER_EMAIL, Role.TEACHER, created_by=owner.id
    )

    accepted = await _accept(client, token, _PASSWORD)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["access_token"]
    assert body["totp_required"] is False

    user = (
        await db_session.execute(select(User).where(User.username == _TEACHER_EMAIL))
    ).scalar_one()
    assert user.status == UserStatus.ACTIVE
    assert user.password_hash is not None
    assert await db_session.get(TotpCredential, user.id) is None
    assert (
        await db_session.scalars(
            select(RecoveryCode).where(RecoveryCode.user_id == user.id)
        )
    ).all() == []

    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200, me.text


async def test_teacher_accept_without_password_then_email_code_login(
    client: httpx.AsyncClient, db_session: AsyncSession, api_email: FakeEmailSender
) -> None:
    """Review Focus 5: blank password yields a usable email-code-ONLY
    account — password_hash stays NULL and the emailed code logs in."""
    from app.modules.identity.models import User
    from tests.e2e.email_code_probe import recover_email_code

    owner = await _seed_owner(db_session)
    token, _ = await _seed_invitation(
        db_session, _TEACHER_EMAIL, Role.TEACHER, created_by=owner.id
    )

    accepted = await _accept(client, token, None)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["totp_required"] is False

    user = (
        await db_session.execute(select(User).where(User.username == _TEACHER_EMAIL))
    ).scalar_one()
    assert user.password_hash is None

    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": _TEACHER_EMAIL}
    )
    assert requested.status_code == 202, requested.text
    _, code = await recover_email_code(db_session, _TEACHER_EMAIL)
    verified = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": _TEACHER_EMAIL, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["tokens"]["access_token"]


async def test_teacher_accept_weak_password_is_rejected_before_the_token_burns(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The validation-order principle survives the optional password: a
    SUPPLIED password must pass the band check before the one-time
    invitation lookup, so a malformed request never consumes the token."""
    from app.core.error_codes import ErrorCode

    owner = await _seed_owner(db_session)
    token, invitation_id = await _seed_invitation(
        db_session, _TEACHER_EMAIL, Role.TEACHER, created_by=owner.id
    )

    rejected = await _accept(client, token, "short")
    assert rejected.status_code in (400, 422), rejected.text
    error = rejected.json()["error"]
    assert error["code"] in (ErrorCode.VALIDATION_ERROR, "VALIDATION_ERROR")

    from uuid import UUID

    invitation = await db_session.get(StaffInvitation, UUID(invitation_id))
    assert invitation is not None and invitation.accepted_at is None

    # The untouched token still accepts.
    ok = await _accept(client, token, _PASSWORD)
    assert ok.status_code == 200, ok.text


async def test_admin_accept_unchanged_pending_totp(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """ADMIN byte-identical to V1: mandatory TOTP onboarding, signaled by
    totp_required: true (the frontend's TotpSetup branch)."""
    from app.modules.identity.models import User

    owner = await _seed_owner(db_session)
    token, _ = await _seed_invitation(
        db_session, _ADMIN_EMAIL, Role.ADMIN, created_by=owner.id
    )

    accepted = await _accept(client, token, _PASSWORD)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["access_token"]
    assert body["totp_required"] is True

    user = (
        await db_session.execute(select(User).where(User.username == _ADMIN_EMAIL))
    ).scalar_one()
    # No credential YET: onboarding is the pending next step, exactly V1.
    assert await db_session.get(TotpCredential, user.id) is None
    # The management surface stays closed until the TOTP confirm.
    manage = await client.get(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert manage.status_code == 403, manage.text
    assert manage.json()["error"]["code"] == "TOTP_SETUP_REQUIRED"


async def test_admin_accept_requires_password(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """ADMIN keeps the mandatory password — a blank one is a validation
    error, not an email-code-only ADMIN."""
    owner = await _seed_owner(db_session)
    token, _ = await _seed_invitation(
        db_session, _ADMIN_EMAIL, Role.ADMIN, created_by=owner.id
    )

    rejected = await _accept(client, token, None)
    assert rejected.status_code in (400, 422), rejected.text


async def test_passwordless_teacher_on_password_channel_is_uniform_401(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Final-review F2 pin: a passwordless TEACHER hitting the retained
    password channel (spec §2.4 keeps it) gets the SAME uniform 401 as a
    wrong password — never a 500 (verify_password would raise
    AttributeError on a NULL hash) and never a probe signal that the
    account has no password."""
    owner = await _seed_owner(db_session)
    token, _ = await _seed_invitation(
        db_session, _TEACHER_EMAIL, Role.TEACHER, created_by=owner.id
    )
    accepted = await _accept(client, token, None)
    assert accepted.status_code == 200, accepted.text

    staff_login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": _TEACHER_EMAIL, "password": "whatever-password"},
    )
    assert staff_login.status_code == 401, staff_login.text
    assert staff_login.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
