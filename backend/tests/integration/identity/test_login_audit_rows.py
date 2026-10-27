# backend/tests/integration/identity/test_login_audit_rows.py
"""The §30 login audit rows through the real unified login flow.

The audit-log matrix's G-3/G-4: the code-login services write
EMAIL_LOGIN_SUCCEEDED / EMAIL_LOGIN_TOTP_REQUIRED (V1.1 2026-10-15)
and MOBILE_LOGIN_SUCCEEDED (V1.1 2026-10-16) via AuditLogWriter, but
no test anywhere asserted the rows — the write code survived the PR-3
route retirement because the unified challenge facade still calls the
services, so the gap is assertions, not behavior. These tests walk the
real HTTP flow (request → verify [/ totp]) and pin each row's ACTION
plus actor/target — not a row count — and G11: the identifier (email
or phone) never lands anywhere on the row.

Channel snapshot mapping (spec's «channel=email_code / +device /
+totp» / «channel=mobile_code» shorthand against the written shapes):

- teacher email-code completion → {"channel": "email_code"};
- ADMIN without a remembered cookie → EMAIL_LOGIN_TOTP_REQUIRED (no
  snapshot), then the totp leg → {"channel": "email_code+totp"};
- ADMIN completing AT verify with a valid remembered cookie →
  {"channel": "email_code", "remembered_device": true} (the «+device»
  variant — the only leg that records the device fact; the mobile
  student row is unconditionally {"channel": "mobile_code"});
- student mobile-code completion → {"channel": "mobile_code"};
- staff (teacher) mobile-code completion → {"channel":
  "staff_mobile_code"} (the second mobile write point, the shared
  _staff_login_succeeded helper).

The success INFO log renders the same channel shorthand
(channel=email_code / +device / +totp) one line per login — the
ops-triage counterpart of the snapshot; the email legs pin it via
caplog (ported with CQ-REFACTOR-PORT-01).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import pyotp
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
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from tests.fakes.integrations import (
    FakeEmailSender,
    FakeRateLimiter,
    FakeSmsSender,
)

pytestmark = pytest.mark.integration

# Anchored to the real now: PyJWT validates exp/iat on the wall clock
# while TOTP codes are cut from the business clock (the unified-suite
# discipline).
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_REDIS_DB = 4
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_REDIS_DB}"))


@pytest_asyncio.fixture
async def api_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(_redis_url(), decode_responses=True)
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
    api_sms: FakeSmsSender,
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
    app.dependency_overrides[get_sms_sender] = lambda: api_sms
    app.dependency_overrides[get_email_sender] = lambda: api_email
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_user(
    db: AsyncSession,
    *,
    username: str,
    role: Role,
    phone: str | None = None,
    totp_secret: str | None = None,
) -> User:
    """The unified-suite seed shape: the staff email service resolves
    mailboxes BY USERNAME, so username and email_normalized agree."""
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=username.split("@")[0],
        email_normalized=username,
        email_verified_at=_T0,
        phone_e164=phone,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    if totp_secret is not None:
        from cryptography.fernet import Fernet

        from app.modules.identity.models import TotpCredential
        from app.modules.identity.totp import encrypt_totp_secret

        fernet = Fernet(get_settings().totp_encryption_key.encode())
        db.add(
            TotpCredential(
                user_id=user.id,
                secret_encrypted=encrypt_totp_secret(fernet, totp_secret),
                confirmed_at=_T0,
            )
        )
        await db.flush()
    return user


def _totp_code(secret: str, clock: FrozenClock) -> str:
    return pyotp.TOTP(secret).at(clock.now())


def _device_cookie(response: httpx.Response) -> tuple[str, str]:
    """The minted slot cookie as (name, value) — Secure cookies do not
    ride the http://test jar, so the credential goes on the request
    explicitly (the unified-suite pattern)."""
    from app.modules.identity.device_credentials import DEVICE_COOKIE_PREFIX

    for header in response.headers.get_list("set-cookie"):
        if header.startswith(DEVICE_COOKIE_PREFIX):
            name, _, rest = header.partition("=")
            return name, rest.split(";", 1)[0]
    raise AssertionError("no device cookie on the response")


async def _login_audit_rows(db: AsyncSession, user_id: UUID) -> list[AuditLog]:
    """Every audit row the account's own logins wrote (§30: actor IS the
    logging-in account — target=self is part of the pin)."""
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.actor_user_id == user_id)))
        .scalars()
        .all()
    )
    # All rows share the harness transaction's now() — order by payload,
    # not timestamps.
    return sorted(
        rows, key=lambda row: (row.action, json.dumps(row.after_snapshot or {}))
    )


def _assert_identifier_never_lands_on_the_row(row: AuditLog, identifier: str) -> None:
    """G11: no payload column of the row carries the login identifier —
    the audit trail proves the login without restating the mailbox or
    phone number anywhere."""
    blob = json.dumps(
        {
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "reason": row.reason,
            "details": row.details,
            "before_snapshot": row.before_snapshot,
            "after_snapshot": row.after_snapshot,
            "request_id": row.request_id,
        },
        ensure_ascii=False,
    )
    assert identifier not in blob, f"login identifier leaked onto the audit row: {blob}"


def _assert_self_targeted_login_row(
    row: AuditLog, user: User, *, action: str, role: Role
) -> None:
    """The shared §30 login-row pin: the named action, actor=the
    logging-in account, target=the same account, no before-state (a
    login migrates nothing)."""
    assert row.action == action
    assert row.actor_user_id == user.id
    assert row.actor_role == role.value
    assert row.target_type == "user"
    assert row.target_id == str(user.id)
    assert row.before_snapshot is None


async def _email_request_and_code(
    client: httpx.AsyncClient,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
    email: str,
) -> str:
    """One email-arm request cycle; the wall-clock cooldown key is
    cleared first (its TTL runs on real time, the clock here is frozen)."""
    await api_redis.delete(f"emailcode:cooldown:{email}")
    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": email}
    )
    assert requested.status_code == 202, requested.text
    return str(api_email.messages[-1].variables["code"])


# --- G-3: the email-code login family ------------------------------------------------


async def test_teacher_email_code_login_writes_one_succeeded_audit_row(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TEACHER completes at verify: exactly one EMAIL_LOGIN_SUCCEEDED
    row, actor=target=the account itself, the email_code channel
    snapshot, and the HTTP correlation pair threaded from the request —
    with the mailbox nowhere on the row (G11) and the success INFO log
    rendering the same channel shorthand."""
    teacher = await _seed_user(
        db_session,
        username=f"audit-teacher-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.TEACHER,
    )
    await db_session.commit()
    caplog.set_level(logging.INFO, logger="app.modules.identity.email_login_service")

    code = await _email_request_and_code(client, api_email, api_redis, teacher.username)
    verified = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": teacher.username, "code": code},
        headers={"X-Request-ID": "login-aud-0001"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "authenticated"

    rows = await _login_audit_rows(db_session, teacher.id)
    assert len(rows) == 1, "the teacher's single email-code login wrote one row"
    row = rows[0]
    _assert_self_targeted_login_row(
        row, teacher, action="EMAIL_LOGIN_SUCCEEDED", role=Role.TEACHER
    )
    assert row.after_snapshot == {"channel": "email_code"}
    # The router threads AuditContext.from_request: the propagated id
    # and the directly connected peer land on the durable row (§30).
    assert row.request_id == "login-aud-0001"
    assert row.ip_address == "127.0.0.1"
    _assert_identifier_never_lands_on_the_row(row, teacher.username)
    # The log face of the same channel contract: the success INFO log
    # renders the snapshot's channel shorthand for ops triage.
    assert "channel=email_code" in caplog.text


async def test_admin_email_login_writes_totp_required_then_succeeded_rows(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ADMIN second-leg dispatch: the code leg answers totp_required
    with an EMAIL_LOGIN_TOTP_REQUIRED row (no snapshot — nothing
    migrated yet), the totp leg writes the email_code+totp success, and
    a later remembered-cookie completion writes the «+device» variant
    ({remembered_device: true}) AT verify — three rows, each pinned;
    the mailbox appears on none of them (G11) and each success INFO log
    renders its own channel shorthand."""
    secret = pyotp.random_base32()
    admin = await _seed_user(
        db_session,
        username=f"audit-admin-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        totp_secret=secret,
    )
    await db_session.commit()
    caplog.set_level(logging.INFO, logger="app.modules.identity.email_login_service")

    # Leg 1: the code is good, the second factor is owed.
    code = await _email_request_and_code(client, api_email, api_redis, admin.username)
    pending = (
        await client.post(
            "/api/v1/auth/challenge/verify",
            json={"identifier": admin.username, "code": code},
        )
    ).json()
    assert pending["status"] == "totp_required", pending

    totp_rows = await _login_audit_rows(db_session, admin.id)
    assert [row.action for row in totp_rows] == ["EMAIL_LOGIN_TOTP_REQUIRED"]
    _assert_self_targeted_login_row(
        totp_rows[0], admin, action="EMAIL_LOGIN_TOTP_REQUIRED", role=Role.ADMIN
    )
    assert totp_rows[0].after_snapshot is None
    _assert_identifier_never_lands_on_the_row(totp_rows[0], admin.username)

    # Leg 2: the totp completion (remember_device mints the cookie the
    # third leg consumes).
    finished = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": pending["pending_token"],
            "totp_code": _totp_code(secret, api_clock),
            "remember_device": True,
        },
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "authenticated"
    cookie = _device_cookie(finished)

    after_totp = await _login_audit_rows(db_session, admin.id)
    assert [row.action for row in after_totp] == [
        "EMAIL_LOGIN_SUCCEEDED",
        "EMAIL_LOGIN_TOTP_REQUIRED",
    ]
    succeeded = after_totp[0]
    _assert_self_targeted_login_row(
        succeeded, admin, action="EMAIL_LOGIN_SUCCEEDED", role=Role.ADMIN
    )
    assert succeeded.after_snapshot == {"channel": "email_code+totp"}
    _assert_identifier_never_lands_on_the_row(succeeded, admin.username)
    # Log face, leg 2: the totp completion renders its own shorthand.
    assert "channel=email_code+totp" in caplog.text
    caplog.clear()

    # Leg 3: a valid remembered cookie replaces the second factor — the
    # login completes AT verify with the device fact snapshotted.
    warm_code = await _email_request_and_code(
        client, api_email, api_redis, admin.username
    )
    warm = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": admin.username, "code": warm_code},
        cookies={cookie[0]: cookie[1]},
    )
    assert warm.status_code == 200, warm.text
    assert warm.json()["status"] == "authenticated"
    # Log face, leg 3: the remembered-device completion is the ONLY leg
    # whose log renders the «+device» shorthand.
    assert "channel=email_code+device" in caplog.text

    final_rows = await _login_audit_rows(db_session, admin.id)
    assert sorted(row.action for row in final_rows) == [
        "EMAIL_LOGIN_SUCCEEDED",
        "EMAIL_LOGIN_SUCCEEDED",
        "EMAIL_LOGIN_TOTP_REQUIRED",
    ]
    # Pick by snapshot, not list position: the two success rows differ
    # only in their channel payload.
    device_row = next(
        row
        for row in final_rows
        if row.after_snapshot == {"channel": "email_code", "remembered_device": True}
    )
    _assert_self_targeted_login_row(
        device_row, admin, action="EMAIL_LOGIN_SUCCEEDED", role=Role.ADMIN
    )
    _assert_identifier_never_lands_on_the_row(device_row, admin.username)
    totp_channel_row = next(
        row
        for row in final_rows
        if row.after_snapshot == {"channel": "email_code+totp"}
    )
    assert totp_channel_row is succeeded  # leg 2's row survived untouched


# --- G-4: the mobile-code login family ------------------------------------------------


async def test_student_mobile_code_login_writes_one_succeeded_audit_row(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    """A STUDENT's phone-code login writes exactly one
    MOBILE_LOGIN_SUCCEEDED row: actor=target=the account, the
    mobile_code channel snapshot, and the phone number nowhere on the
    row (G11)."""
    student = await _seed_user(
        db_session,
        username=f"2025{int(uuid4().hex, 16) % 10**7:07d}",
        role=Role.STUDENT,
        phone="+8613800180001",
    )
    await db_session.commit()

    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": student.phone_e164}
    )
    assert requested.status_code == 202, requested.text
    code = str(api_sms.messages[-1].variables["code"])
    verified = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": student.phone_e164, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "authenticated"

    rows = await _login_audit_rows(db_session, student.id)
    assert len(rows) == 1, "the student's single mobile-code login wrote one row"
    row = rows[0]
    _assert_self_targeted_login_row(
        row, student, action="MOBILE_LOGIN_SUCCEEDED", role=Role.STUDENT
    )
    assert row.after_snapshot == {"channel": "mobile_code"}
    _assert_identifier_never_lands_on_the_row(row, student.phone_e164 or "")


async def test_staff_mobile_code_login_writes_one_succeeded_audit_row(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    """The staff write point of the same action: a TEACHER-bound phone
    routes to the staff mobile service and completes at verify with the
    staff_mobile_code channel snapshot — actor=target=self again, and
    the phone number stays off the row (G11)."""
    teacher = await _seed_user(
        db_session,
        username=f"audit-staff-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.TEACHER,
        phone="+8613800180002",
    )
    await db_session.commit()

    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": teacher.phone_e164}
    )
    assert requested.status_code == 202, requested.text
    code = str(api_sms.messages[-1].variables["code"])
    verified = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": teacher.phone_e164, "code": code},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "authenticated"
    assert verified.json()["role"] == "TEACHER"

    rows = await _login_audit_rows(db_session, teacher.id)
    assert len(rows) == 1
    row = rows[0]
    _assert_self_targeted_login_row(
        row, teacher, action="MOBILE_LOGIN_SUCCEEDED", role=Role.TEACHER
    )
    assert row.after_snapshot == {"channel": "staff_mobile_code"}
    _assert_identifier_never_lands_on_the_row(row, teacher.phone_e164 or "")
