# backend/tests/integration/identity/test_identity_api.py
"""The identity HTTP API end-to-end over real PostgreSQL + Redis (Task 9).

Drives the real app (`create_app()` — real routes, real envelope handlers,
real composition-root wiring incl. the rbac actor seam) through the full
student journey the task brief freezes:

    seed whitelist -> request OTP -> verify OTP -> register -> login
    -> GET /api/v1/me

plus the security surface the plan pins: refresh-cookie flags, CSRF
double-submit on cookie-authenticated mutations, the §29 envelope on
business/typed/framework errors, the separate staff login + mandatory TOTP
gate, and endpoint rate limiting with normalized identifiers.

Seams are dependency overrides, not fakes of the routes: the rollback-
harness session (nothing leaks between tests), the flushed Redis test
database 15, the FrozenClock (anchored to the real now because PyJWT
validates ``exp`` against wall-clock time), the deterministic SMS/Email
fakes, and the FakeRateLimiter recording every check. The management probe
route below is a throwaway stand-in for Plan 03's teacher endpoints — the
guard under test is the real one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http import cookies as http_cookies
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

import httpx
import pyotp
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password, hash_refresh_token
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
    require_staff_management_actor,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import StudentWhitelist, User
from app.modules.identity.router import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    REFRESH_COOKIE_NAME,
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from app.modules.identity.session_service import SessionService
from app.modules.identity.staff_service import StaffService
from tests.fakes.integrations import (
    FakeEmailSender,
    FakeRateLimiter,
    FakeSmsSender,
)

_OTP_TEST_REDIS_DB = 15
# Anchored to the real now: PyJWT validates `exp` at decode time against
# wall-clock time, so tokens minted by the app must be "just now" (the
# account-status suite uses the same anchoring).
_T0 = datetime.now(UTC).replace(microsecond=0)
_STUDENT = "20250010001"
_PASSWORD = "correct-horse-battery"
_PHONE_RAW = "+86 137 0013 7001"
_PHONE_E164 = "+8613700137001"
_NICKNAME = "接口测试同学"
_STAFF_EMAIL = "teacher@pku.edu.cn"
_STAFF_PASSWORD = "staff-correct-horse"
_ADMIN_EMAIL = "admin@pku.edu.cn"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_RECOVERY_CODE_LENGTH = 11


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_OTP_TEST_REDIS_DB}"))


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
def api_email() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def fake_limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock,
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

    @app.post("/api/v1/test/manage-probe")
    async def manage_probe(
        actor: Annotated[Actor, Depends(require_staff_management_actor)],
    ) -> dict[str, str]:
        # Throwaway stand-in for Plan 03's teacher/management endpoints: the
        # point under test is the guard, which is the real dependency.
        return {"user_id": str(actor.user_id), "role": actor.role.value}

    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


def _cookie_header(response: httpx.Response, name: str) -> str:
    matches = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    ]
    assert len(matches) == 1, f"expected exactly one {name} Set-Cookie"
    return matches[0]


def _cookie_value(response: httpx.Response, name: str) -> str:
    jar = http_cookies.SimpleCookie()
    jar.load(_cookie_header(response, name))
    return jar[name].value


async def _seed_whitelist(db: AsyncSession, *numbers: str) -> None:
    for number in numbers:
        db.add(StudentWhitelist(student_number=number))
    await db.flush()


async def _seed_student(db: AsyncSession, username: str = _STUDENT) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=_NICKNAME,
        phone_e164=_PHONE_E164,
        role=Role.STUDENT.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _login(client: httpx.AsyncClient, username: str = _STUDENT) -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


# --- the frozen student flow (task brief step 1) -------------------------------


@pytest.mark.integration
async def test_student_flow_whitelist_otp_register_login_me(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    api_sms: FakeSmsSender,
) -> None:
    await _seed_whitelist(db_session, _STUDENT)

    challenged = await client.post(
        "/api/v1/auth/phone/challenges", json={"phone": _PHONE_RAW}
    )
    assert challenged.status_code == 200, challenged.text
    assert set(challenged.json()) == {"challenge_id", "expires_at"}

    assert len(api_sms.messages) == 1
    code = api_sms.messages[0].variables["code"]
    assert isinstance(code, str) and len(code) == 6

    verified = await client.post(
        f"/api/v1/auth/phone/challenges/{challenged.json()['challenge_id']}/verify",
        json={"code": code},
    )
    assert verified.status_code == 200, verified.text
    phone_token = verified.json()["phone_token"]
    assert verified.json()["expires_at"]

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "student_number": _STUDENT,
            "nickname": f"  {_NICKNAME}  ",
            "phone_token": phone_token,
            "password": _PASSWORD,
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json() == {
        "id": registered.json()["id"],
        "username": _STUDENT,
        "nickname": _NICKNAME,  # normalized by the service
        "role": "STUDENT",
        "status": "ACTIVE",
    }

    logged_in = await client.post(
        "/api/v1/auth/login",
        # §5.2: outer whitespace is stripped at the login boundary.
        json={"username": f"  {_STUDENT}  ", "password": _PASSWORD},
    )
    assert logged_in.status_code == 200, logged_in.text
    session = logged_in.json()
    assert set(session) == {"access_token", "refresh_token", "csrf_token", "token_type"}

    # Cookie flags (spec §5.6/§33.1): HttpOnly refresh, readable CSRF, both
    # Secure + SameSite=Lax; the refresh cookie is scoped to the auth paths.
    refresh_cookie = _cookie_header(logged_in, REFRESH_COOKIE_NAME).lower()
    assert "httponly" in refresh_cookie
    assert "secure" in refresh_cookie
    assert "samesite=lax" in refresh_cookie
    assert "path=/api/v1/auth" in refresh_cookie
    csrf_cookie = _cookie_header(logged_in, CSRF_COOKIE_NAME).lower()
    assert "httponly" not in csrf_cookie
    assert "secure" in csrf_cookie
    assert "samesite=lax" in csrf_cookie
    assert _cookie_value(logged_in, CSRF_COOKIE_NAME) == session["csrf_token"]

    me = await client.get("/api/v1/me", headers=_bearer(session))
    assert me.status_code == 200, me.text
    assert me.json()["username"] == _STUDENT
    assert me.json()["phone_e164"] == _PHONE_E164
    assert me.json()["nickname"] == _NICKNAME

    # No response anywhere in the flow carries secret material: the Argon2id
    # verifier, the OTP code, the refresh-token digest, or a TOTP secret.
    forbidden = (
        "password_hash",
        "$argon2",
        hash_refresh_token(session["refresh_token"]),
        f'"{code}"',
        "secret_encrypted",
    )
    for response in (challenged, verified, registered, logged_in, me):
        for material in forbidden:
            assert material not in response.text


# --- envelope discipline (spec §29) --------------------------------------------


@pytest.mark.integration
async def test_business_and_validation_errors_use_the_envelope(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    await _seed_student(db_session)

    wrong_password = await client.post(
        "/api/v1/auth/login", json={"username": _STUDENT, "password": "wrong-horse!"}
    )
    assert wrong_password.status_code == 401
    error = _envelope(wrong_password)
    assert error["code"] == "AUTHENTICATION_REQUIRED"
    assert error["message"]
    assert error["details"] is None

    missing_fields = await client.post("/api/v1/auth/login", json={})
    assert missing_fields.status_code == 422
    validation = _envelope(missing_fields)
    assert validation["code"] == "VALIDATION_ERROR"
    assert set(validation["details"]) == {"username", "password"}


@pytest.mark.integration
async def test_wrong_otp_code_renders_the_typed_envelope(
    client: httpx.AsyncClient,
    api_sms: FakeSmsSender,
) -> None:
    challenged = await client.post(
        "/api/v1/auth/phone/challenges", json={"phone": _PHONE_RAW}
    )
    assert challenged.status_code == 200

    wrong = await client.post(
        f"/api/v1/auth/phone/challenges/{challenged.json()['challenge_id']}/verify",
        json={"code": "000000"},
    )
    assert wrong.status_code == 400
    assert _envelope(wrong)["code"] == "OTP_CODE_INVALID"


@pytest.mark.integration
async def test_invalid_phone_is_a_validation_error(
    client: httpx.AsyncClient,
) -> None:
    rejected = await client.post(
        "/api/v1/auth/phone/challenges", json={"phone": "not-a-phone"}
    )
    assert rejected.status_code == 400
    assert _envelope(rejected)["code"] == "VALIDATION_ERROR"


# --- CSRF on cookie-authenticated mutations (spec §5.6, §33.1) -----------------


@pytest.mark.integration
async def test_cookie_refresh_requires_csrf_and_accepts_a_valid_token(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    await _seed_student(db_session)
    login_response = await client.post(
        "/api/v1/auth/login", json={"username": _STUDENT, "password": _PASSWORD}
    )
    assert login_response.status_code == 200
    session = login_response.json()
    auth_cookies = {
        REFRESH_COOKIE_NAME: _cookie_value(login_response, REFRESH_COOKIE_NAME),
        CSRF_COOKIE_NAME: session["csrf_token"],
    }

    # Mutation without the echoed header: rejected, envelope-shaped.
    missing_header = await client.post("/api/v1/auth/refresh", cookies=auth_cookies)
    assert missing_header.status_code == 403
    assert _envelope(missing_header)["code"] == "PERMISSION_DENIED"

    # The same request with the header succeeds and rotates both cookies.
    accepted = await client.post(
        "/api/v1/auth/refresh",
        cookies=auth_cookies,
        headers={CSRF_HEADER_NAME: session["csrf_token"]},
    )
    assert accepted.status_code == 200, accepted.text
    rotated = accepted.json()
    assert rotated["access_token"] != session["access_token"]
    assert rotated["csrf_token"] != session["csrf_token"]
    me = await client.get("/api/v1/me", headers=_bearer(rotated))
    assert me.status_code == 200


@pytest.mark.integration
async def test_body_refresh_token_needs_no_csrf(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    # Non-cookie clients carry no ambient browser credential, so the CSRF
    # dependency must stay disengaged (no cookies on the request at all).
    await _seed_student(db_session)
    session = await _login(client)

    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"] != session["access_token"]


@pytest.mark.integration
async def test_logout_revokes_the_presented_session(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    await _seed_student(db_session)
    login_response = await client.post(
        "/api/v1/auth/login", json={"username": _STUDENT, "password": _PASSWORD}
    )
    session = login_response.json()
    refresh_token = _cookie_value(login_response, REFRESH_COOKIE_NAME)

    logged_out = await client.post(
        "/api/v1/auth/logout",
        cookies={
            REFRESH_COOKIE_NAME: refresh_token,
            CSRF_COOKIE_NAME: session["csrf_token"],
        },
        headers={CSRF_HEADER_NAME: session["csrf_token"]},
    )
    assert logged_out.status_code == 204

    dead = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert dead.status_code == 401
    assert _envelope(dead)["code"] == "AUTHENTICATION_REQUIRED"


# --- staff: separate login, mandatory TOTP, management gate (§5.8, §33.4) ------


def _staff_service(api_clock, api_redis: aioredis.Redis) -> StaffService:
    settings = get_settings()
    sessions = SessionService(
        clock=api_clock,
        access_codec=get_access_token_codec(),
        refresh_token_ttl_days=settings.refresh_token_ttl_days,
    )
    return StaffService(
        clock=api_clock,
        sessions=sessions,
        fernet=Fernet(settings.totp_encryption_key),
        events=InMemoryEventCollector(),
        invitation_ttl_hours=settings.staff_invitation_ttl_hours,
    )


async def _seed_staff_invitation(db: AsyncSession, api_clock, api_redis) -> str:
    admin = User(
        username=_ADMIN_EMAIL,
        password_hash=hash_password(_PASSWORD),
        nickname="管理员",
        email_normalized=_ADMIN_EMAIL,
        email_verified_at=_T0,
        role=Role.ADMIN.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(admin)
    await db.flush()
    issued = await _staff_service(api_clock, api_redis).create_staff_invitation(
        db, Actor(user_id=admin.id, role=Role.ADMIN), _STAFF_EMAIL, Role.TEACHER
    )
    return issued.token


@pytest.mark.integration
async def test_staff_onboarding_until_management_access(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    api_clock,
    api_redis: aioredis.Redis,
) -> None:
    invitation_token = await _seed_staff_invitation(db_session, api_clock, api_redis)

    accepted = await client.post(
        "/api/v1/auth/staff/invitations/accept",
        json={"token": invitation_token, "password": _STAFF_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    pending = accepted.json()

    # Pending staff is authenticated but NOT a management actor: the real
    # guard answers with the distinct setup-forcing code (§5.8 step 3).
    gated = await client.post("/api/v1/test/manage-probe", headers=_bearer(pending))
    assert gated.status_code == 403
    assert _envelope(gated)["code"] == "TOTP_SETUP_REQUIRED"

    # The separate staff endpoint enforces the same gate at login — even
    # with the CORRECT password, before 2FA is confirmed.
    premature = await client.post(
        "/api/v1/auth/staff/login",
        json={
            "email": _STAFF_EMAIL,
            "password": _STAFF_PASSWORD,
            "totp_code": "123456",
        },
    )
    assert premature.status_code == 403
    assert _envelope(premature)["code"] == "TOTP_SETUP_REQUIRED"

    begun = await client.post("/api/v1/staff/totp/begin", headers=_bearer(pending))
    assert begun.status_code == 200, begun.text
    secret = begun.json()["secret"]
    assert begun.json()["otpauth_uri"].startswith("otpauth://totp/")

    confirmed = await client.post(
        "/api/v1/staff/totp/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers=_bearer(pending),
    )
    assert confirmed.status_code == 200, confirmed.text
    recovery_codes = confirmed.json()["recovery_codes"]
    assert len(recovery_codes) == 8
    assert all(len(code) == _RECOVERY_CODE_LENGTH for code in recovery_codes)

    staff_session = await client.post(
        "/api/v1/auth/staff/login",
        json={
            "email": _STAFF_EMAIL,
            "password": _STAFF_PASSWORD,
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert staff_session.status_code == 200, staff_session.text
    staff = staff_session.json()
    # Nothing about the credential ever rides along on a login response.
    for material in ("password_hash", "$argon2", secret):
        assert material not in staff_session.text

    allowed = await client.post("/api/v1/test/manage-probe", headers=_bearer(staff))
    assert allowed.status_code == 200
    assert allowed.json()["role"] == "TEACHER"


@pytest.mark.integration
async def test_student_cannot_reach_staff_totp_setup(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    # The staff-role guard on the setup endpoints runs through the REAL
    # composition-root wiring (rbac.get_role_bearer -> identity get_actor).
    await _seed_student(db_session)
    session = await _login(client)

    denied = await client.post("/api/v1/staff/totp/begin", headers=_bearer(session))
    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "PERMISSION_DENIED"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/staff/totp/begin", None),
        ("/api/v1/staff/totp/confirm", {"code": "123456"}),
    ],
)
async def test_suspended_staff_denied_totp_setup(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    api_clock: FrozenClock,
    path: str,
    payload: dict | None,
) -> None:
    # Fix-round-1: the setup routes must also carry the account-status gate
    # (§5.7) — a SUSPENDED staff member holding a still-live access token
    # (revocation lags suspension) may not rotate a pending TOTP credential.
    # No privilege is gained (the management guard re-checks), but the gate
    # belongs here too. Login refuses suspended accounts, so the session is
    # minted directly through the real SessionService.
    suspended = User(
        username="suspended@pku.edu.cn",
        password_hash=hash_password(_PASSWORD),
        nickname="停用教师",
        email_normalized="suspended@pku.edu.cn",
        email_verified_at=_T0,
        role=Role.TEACHER.value,
        status=UserStatus.SUSPENDED.value,
    )
    db_session.add(suspended)
    await db_session.flush()
    sessions = SessionService(clock=api_clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(
        db_session, user=suspended, now=api_clock.now()
    )

    denied = await client.post(
        path, json=payload, headers=_bearer({"access_token": tokens.access_token})
    )

    assert denied.status_code == 403
    assert _envelope(denied)["code"] == "ACCOUNT_NOT_ACTIVE"


# --- rate limiting: normalized identifiers + 429 envelope (spec §33.1) ---------


@pytest.mark.integration
async def test_rate_limiter_receives_normalized_identifiers(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    fake_limiter: FakeRateLimiter,
) -> None:
    await _seed_student(db_session)

    await client.post(
        "/api/v1/auth/login",
        json={"username": f"  {_STUDENT}  ", "password": _PASSWORD},
    )
    assert fake_limiter.checks_for("auth:login")[-1].identifier == _STUDENT

    await client.post(
        "/api/v1/auth/staff/login",
        json={
            "email": f"  {_STAFF_EMAIL.upper()}  ",
            "password": "irrelevant",
            "totp_code": "123456",
        },
    )
    assert fake_limiter.checks_for("auth:staff-login")[-1].identifier == _STAFF_EMAIL

    # A DIFFERENT number than the student's bound phone: the real OTP
    # cooldown (60s, FrozenClock-frozen) would 429 a second send to the
    # same phone within this test.
    otp = await client.post(
        "/api/v1/auth/phone/challenges", json={"phone": "138 0013 8000"}
    )
    assert otp.status_code == 200
    assert fake_limiter.checks_for("auth:otp-send")[-1].identifier == "+8613800138000"

    forgot = await client.post(
        "/api/v1/auth/password/forgot", json={"username": f" {_STUDENT} "}
    )
    assert forgot.status_code == 200
    assert fake_limiter.checks_for("auth:password-reset")[-1].identifier == _STUDENT

    session = await _login(client)
    email_bind = await client.post(
        "/api/v1/me/email",
        json={"email": "  User@PKU.edu.CN  "},
        headers=_bearer(session),
    )
    assert email_bind.status_code == 200
    assert fake_limiter.checks_for("me:email-verify")[-1].identifier == (
        "user@pku.edu.cn"
    )

    phone_change = await client.post(
        "/api/v1/me/phone/change",
        json={"password": _PASSWORD, "new_phone": "139 0013 9000"},
        headers=_bearer(session),
    )
    assert phone_change.status_code == 200
    assert fake_limiter.checks_for("me:phone-change")[-1].identifier == "+8613900139000"

    register = await client.post(
        "/api/v1/auth/register",
        json={
            "student_number": f"  {_STUDENT}  ",
            "nickname": "限流同学",
            "phone_token": "irrelevant",
            "password": _PASSWORD,
        },
    )
    assert register.status_code in (400, 403, 409)  # rejected downstream
    assert fake_limiter.checks_for("auth:register")[-1].identifier == _STUDENT


@pytest.mark.integration
async def test_exhausted_rate_limit_renders_429_envelope(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    fake_limiter: FakeRateLimiter,
) -> None:
    await _seed_student(db_session)
    fake_limiter.fail_on("auth:login")

    throttled = await client.post(
        "/api/v1/auth/login", json={"username": _STUDENT, "password": _PASSWORD}
    )
    assert throttled.status_code == 429
    assert _envelope(throttled)["code"] == "RATE_LIMITED"


# --- authenticated profile surface (spec §5.3-5.5) ------------------------------


@pytest.mark.integration
async def test_nickname_patch_updates_and_returns_the_owner_view(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    await _seed_student(db_session)
    session = await _login(client)

    patched = await client.patch(
        "/api/v1/me/nickname",
        json={"nickname": "  新昵称😀  "},
        headers=_bearer(session),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["nickname"] == "新昵称😀"
    assert set(patched.json()) == {
        "id",
        "username",
        "nickname",
        "role",
        "status",
        "phone_e164",
        "email_normalized",
        "email_verified_at",
    }


@pytest.mark.integration
async def test_unauthenticated_me_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    rejected = await client.get("/api/v1/me")
    assert rejected.status_code == 401
    assert _envelope(rejected)["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.integration
async def test_password_forgot_is_uniform_and_rejects_staff(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
) -> None:
    await _seed_student(db_session)
    teacher = User(
        username="t-teacher@pku.edu.cn",
        password_hash=hash_password(_PASSWORD),
        nickname="教师",
        role=Role.TEACHER.value,
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(teacher)
    await db_session.flush()

    known = await client.post(
        "/api/v1/auth/password/forgot", json={"username": _STUDENT}
    )
    unknown = await client.post(
        "/api/v1/auth/password/forgot", json={"username": "20990099999"}
    )
    assert known.status_code == unknown.status_code == 200
    assert (
        set(known.json())
        == set(unknown.json())
        == {
            "challenge_id",
            "expires_at",
        }
    )
    assert known.json()["expires_at"] == unknown.json()["expires_at"]

    staff_reset = await client.post(
        "/api/v1/auth/password/forgot", json={"username": teacher.username}
    )
    assert staff_reset.status_code == 403
    assert _envelope(staff_reset)["code"] == "PASSWORD_RESET_NOT_ALLOWED"
