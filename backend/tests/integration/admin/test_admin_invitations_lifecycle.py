# backend/tests/integration/admin/test_admin_invitations_lifecycle.py
"""Wave 2a §4.2: the staff invitation lifecycle on the admin surface.

The list derives each row's state (PENDING/ACCEPTED/EXPIRED/REVOKED)
against the business clock and shows the email masked only — the
plaintext never leaves the service boundary on this read path. DELETE
revokes an un-accepted invitation and its token dies immediately (the
accept lookup can no longer resolve it). resend mints a fresh one-time
token — the old one dies, the expiry window restarts, and a revoked row
comes back to life. create refuses an email already held by an account
(409 EMAIL_ALREADY_BOUND, normalized before the check).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import AccessTokenCodec, hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import StaffInvitation, TotpCredential, User
from app.modules.identity.providers import get_rate_limiter
from app.modules.identity.session_service import SessionService
from app.modules.identity.staff_service import StaffService
from tests.fakes.integrations import FakeRateLimiter

pytestmark = pytest.mark.integration

# Anchored to the real now: PyJWT validates ``exp`` against wall-clock
# time, so tokens decoded on the request path must be minted "just now".
# Invitation TTL and session liveness are FrozenClock-driven.
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_INVITATION_TTL_HOURS = 48
_ACCESS_SECRET = "integration-test-access-token-secret-0123456789"
_FERNET_KEY = Fernet.generate_key()


def _email() -> str:
    return f"inv-{uuid4().hex[:10]}@campus.example.edu"


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession, api_clock: FrozenClock, fake_limiter: FakeRateLimiter
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


def _advance(clock: FrozenClock, **kwargs: int) -> None:
    object.__setattr__(clock, "current", clock.current + timedelta(**kwargs))


def _make_staff_service(clock: FrozenClock) -> StaffService:
    return StaffService(
        clock=clock,
        sessions=SessionService(
            clock=clock,
            access_codec=AccessTokenCodec(secret=_ACCESS_SECRET, ttl_minutes=15),
            refresh_token_ttl_days=30,
        ),
        fernet=Fernet(_FERNET_KEY),
        events=InMemoryEventCollector(),
        invitation_ttl_hours=_INVITATION_TTL_HOURS,
    )


async def _seed_staff(
    db: AsyncSession, clock: FrozenClock, *, role: Role, status: UserStatus
) -> User:
    username = f"lifecycle-{uuid4().hex[:8]}@campus.example.edu"
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=username.split("@")[0],
        email_normalized=username,
        email_verified_at=clock.now(),
        role=role,
        status=status,
    )
    db.add(user)
    await db.flush()
    if role is not Role.STUDENT:
        db.add(
            TotpCredential(
                user_id=user.id,
                secret_encrypted=b"test-stand-in-secret",
                confirmed_at=clock.now(),
            )
        )
    await db.flush()
    return user


async def _admin_headers(
    db: AsyncSession, clock: FrozenClock
) -> tuple[User, dict[str, str]]:
    admin = await _seed_staff(db, clock, role=Role.ADMIN, status=UserStatus.ACTIVE)
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=admin, now=clock.now())
    await db.commit()
    return admin, {"Authorization": f"Bearer {tokens.access_token}"}


async def test_list_derives_the_four_states_with_masked_email(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)
    service = _make_staff_service(api_clock)
    actor = Actor(user_id=admin.id, role=Role.ADMIN)

    # An invitation minted at T0 whose 48h window the clock has left.
    gone = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    _advance(api_clock, hours=_INVITATION_TTL_HOURS + 1)

    pending = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    accepted = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    await service.accept_staff_invitation(db_session, accepted.token, None)
    revoked = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    revoked.invitation.revoked_at = api_clock.now()
    await db_session.commit()

    response = await client.get("/api/v1/admin/staff/invitations", headers=headers)

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["items"]}
    assert rows[str(gone.invitation.id)]["status"] == "EXPIRED"
    assert rows[str(pending.invitation.id)]["status"] == "PENDING"
    assert rows[str(accepted.invitation.id)]["status"] == "ACCEPTED"
    assert rows[str(accepted.invitation.id)]["accepted_at"] is not None
    assert rows[str(revoked.invitation.id)]["status"] == "REVOKED"

    # The invited address shows masked (first local character + domain);
    # the plaintext never rides the response.
    for issued in (gone, pending, accepted, revoked):
        row = rows[str(issued.invitation.id)]
        local = issued.invitation.email_normalized.split("@")[0]
        assert (
            row["email"]
            == f"{local[:1]}***@{issued.invitation.email_normalized.split('@')[1]}"
        )
        assert issued.invitation.email_normalized not in response.text
        assert row["role"] == "TEACHER"
        assert row["created_by"] == str(admin.id)
        assert row["issuer_nickname"] == admin.nickname


async def test_revoke_kills_the_token_and_refuses_terminal_states(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)
    service = _make_staff_service(api_clock)
    actor = Actor(user_id=admin.id, role=Role.ADMIN)

    issued = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    await db_session.commit()

    revoked = await client.delete(
        f"/api/v1/admin/staff/invitations/{issued.invitation.id}", headers=headers
    )
    assert revoked.status_code == 204, revoked.text

    # The token died with the row: accept can no longer resolve it, with
    # the same uniform rejection as an unknown token.
    with pytest.raises(BusinessError) as exc:
        await service.accept_staff_invitation(db_session, issued.token, None)
    assert exc.value.code == ErrorCode.AUTHENTICATION_REQUIRED

    # Already revoked — the state machine refuses to loop.
    again = await client.delete(
        f"/api/v1/admin/staff/invitations/{issued.invitation.id}", headers=headers
    )
    assert again.status_code == 409, again.text

    # Accepted invitations are terminal too.
    consumed = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    await service.accept_staff_invitation(db_session, consumed.token, None)
    await db_session.commit()
    refused = await client.delete(
        f"/api/v1/admin/staff/invitations/{consumed.invitation.id}", headers=headers
    )
    assert refused.status_code == 409, refused.text

    # Durable trace: one REVOKED audit row naming the invitation.
    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "STAFF_INVITATION_REVOKED",
                    AuditLog.target_id == str(issued.invitation.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit_rows) == 1

    # Unknown id answers 404, not 500.
    unknown = await client.delete(
        f"/api/v1/admin/staff/invitations/{uuid4()}", headers=headers
    )
    assert unknown.status_code == 404, unknown.text


async def test_resend_rotates_the_one_time_token(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)
    service = _make_staff_service(api_clock)
    actor = Actor(user_id=admin.id, role=Role.ADMIN)

    issued = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    old_expires_at = issued.invitation.expires_at
    _advance(api_clock, hours=1)
    await db_session.commit()

    resent = await client.post(
        f"/api/v1/admin/staff/invitations/{issued.invitation.id}/resend",
        headers=headers,
    )
    assert resent.status_code == 200, resent.text
    body = resent.json()
    assert body["token"] != issued.token
    assert datetime.fromisoformat(body["expires_at"]) > old_expires_at

    # The old token died; the fresh one accepts.
    with pytest.raises(BusinessError) as exc:
        await service.accept_staff_invitation(db_session, issued.token, None)
    assert exc.value.code == ErrorCode.AUTHENTICATION_REQUIRED
    session: Any = await service.accept_staff_invitation(
        db_session, body["token"], None
    )
    assert session.user_id is not None

    # A revoked row comes back to life through resend.
    revived = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    await db_session.commit()
    killed = await client.delete(
        f"/api/v1/admin/staff/invitations/{revived.invitation.id}", headers=headers
    )
    assert killed.status_code == 204, killed.text
    renewed = await client.post(
        f"/api/v1/admin/staff/invitations/{revived.invitation.id}/resend",
        headers=headers,
    )
    assert renewed.status_code == 200, renewed.text
    revived_session = await service.accept_staff_invitation(
        db_session, renewed.json()["token"], None
    )
    assert revived_session.user_id is not None

    # An accepted invitation is terminal for resend as well.
    terminal = await service.create_staff_invitation(
        db_session, actor, _email(), Role.TEACHER
    )
    await service.accept_staff_invitation(db_session, terminal.token, None)
    await db_session.commit()
    refused = await client.post(
        f"/api/v1/admin/staff/invitations/{terminal.invitation.id}/resend",
        headers=headers,
    )
    assert refused.status_code == 409, refused.text


async def test_create_refuses_an_email_already_held_by_an_account(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    admin, headers = await _admin_headers(db_session, api_clock)
    occupied = _email()
    holder = User(
        username=occupied,
        password_hash=hash_password(_PASSWORD),
        nickname="占坑学生",
        email_normalized=occupied,
        email_verified_at=api_clock.now(),
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(holder)
    await db_session.commit()

    # A differently-cased spelling still resolves to the held address:
    # normalization runs before the occupancy check.
    response = await client.post(
        "/api/v1/staff/invitations",
        json={"email": occupied.upper(), "role": "TEACHER"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EMAIL_ALREADY_BOUND"

    created = await db_session.scalar(
        select(func.count())
        .select_from(StaffInvitation)
        .where(StaffInvitation.email_normalized == occupied)
    )
    assert created == 0

    # A free address still invites as before.
    fresh = await client.post(
        "/api/v1/staff/invitations",
        json={"email": _email(), "role": "TEACHER"},
        headers=headers,
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["token"]
