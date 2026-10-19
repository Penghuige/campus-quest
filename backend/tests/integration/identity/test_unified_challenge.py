# backend/tests/integration/identity/test_unified_challenge.py
"""The unified login challenge family (2026-10-19 spec §3) — PR-1.

A route FACADE over the existing challenge services: request routes by
identifier shape (email → staff email code; phone by the users row →
student mobile code, staff mobile code, or the unregistered student
entry; a student number is a format-layer 422), verify answers the
three-state discriminated union (judgment rides ``status`` alone), and
ONE totp endpoint serves BOTH pending-token domains (email-login and
staff-mobile — a GETDEL miss in the wrong domain burns nothing).

Wrong / expired / attempts-exhausted codes answer the single
CHALLENGE_CODE_INVALID everywhere in this family (the branch reason
stays in server logs — the anti-enumeration surface stays uniform).
``remember_device`` is honored by audience only: the STUDENT verify arm
and the ADMIN totp arm mint the cookie; staff code arms never exempt a
factor. The /auth/login identifier form carries the same union (phone
first then student number for students; email → the staff password
flow with the remembered-cookie honoring preserved).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pyotp
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
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

# Anchored to the real now: PyJWT validates ``exp``/``iat`` on the wall
# clock, and TOTP codes are generated from the business clock — both
# must be "just now".
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_REDIS_DB = 11
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
    email: str | None = None,
    phone: str | None = None,
    totp_secret: str | None = None,
) -> User:
    # ``email`` defaults to ``username``: the staff email-code service
    # resolves mailboxes BY USERNAME, so a seed with two independent
    # uuids there is a silent miss (the 202 no-op arm).
    resolved_email = email if email is not None else username
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=username.split("@")[0],
        email_normalized=resolved_email,
        email_verified_at=_T0 if resolved_email else None,
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

        # The APP's key (the services decrypt with Settings'): seeding
        # with a private key would break every totp verification.
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


def _device_cookie(response: httpx.Response) -> str:
    """The minted device cookie off a Set-Cookie header — the jar will
    not carry a Secure cookie over http://test, so the cookie rides the
    request explicitly (the remembered-tests' established pattern)."""
    from app.modules.identity.device_credentials import DEVICE_COOKIE_NAME

    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{DEVICE_COOKIE_NAME}="):
            return header.split("=", 1)[1].split(";", 1)[0]
    raise AssertionError("no device cookie on the response")


def _cookies(value: str) -> dict[str, str]:
    from app.modules.identity.device_credentials import DEVICE_COOKIE_NAME

    return {DEVICE_COOKIE_NAME: value}


async def test_request_routes_by_identifier_shape(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
    api_email: FakeEmailSender,
) -> None:
    """The four request arms + the format-layer 422, by what each arm
    observably does (the mail/SMS counts). The rows' cross-connection
    durability has its own leg below — the savepoint harness cannot
    prove it here."""
    teacher = await _seed_user(
        db_session,
        username=f"uni-teacher-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.TEACHER,
        phone="+8613800160001",
    )
    student = await _seed_user(
        db_session,
        username=f"2025{int(uuid4().hex, 16) % 10**7:07d}",
        role=Role.STUDENT,
        phone="+8613800160002",
    )
    await db_session.commit()

    # email → the staff email-code service really mails one code.
    emailed = await client.post(
        "/api/v1/auth/challenge/request",
        json={"identifier": teacher.email_normalized},
    )
    assert emailed.status_code == 202, emailed.text
    assert len(api_email.messages) == 1

    # a STUDENT-bound phone → the student mobile-code service.
    student_sms = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": student.phone_e164}
    )
    assert student_sms.status_code == 202, student_sms.text
    assert len(api_sms.messages) == 1

    # a STAFF-bound phone → the staff mobile-code service.
    staff_sms = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": teacher.phone_e164}
    )
    assert staff_sms.status_code == 202, staff_sms.text
    assert len(api_sms.messages) == 2

    # an UNREGISTERED phone still gets its student-entry code (the
    # phone-first registration batch): a real send, a real challenge.
    unregistered_sms = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": "+8613800160003"}
    )
    assert unregistered_sms.status_code == 202, unregistered_sms.text
    assert len(api_sms.messages) == 3

    # a student number is a FORMAT-layer refusal: no account query, no
    # enumeration surface.
    refused = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": "20250000001"}
    )
    assert refused.status_code == 422, refused.text


async def test_request_commits_challenges_across_connections(
    db_engine: AsyncEngine,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
    api_email: FakeEmailSender,
) -> None:
    """The facade's 202 arms commit their challenge rows (#85/#101
    lesson): the savepoint suite sessions CANNOT see a missing commit,
    so this leg drives the REAL facade on REAL sessions — a fresh
    connection must find every row after each request returns."""
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.modules.identity.email_login_service import EmailLoginService
    from app.modules.identity.mobile_login_service import MobileLoginService
    from app.modules.identity.models import LoginEmailChallenge, LoginMobileChallenge
    from app.modules.identity.unified_login_service import UnifiedLoginService

    run = uuid4().hex[:8]
    teacher_mail = f"durable-uni-{run}@campus.example.edu"
    teacher_phone = "+8613800160091"
    student_phone = "+8613800160092"
    student_number = f"2025{int(uuid4().hex, 16) % 10**7:07d}"
    async with AsyncSession(db_engine) as session:
        session.add(
            User(
                username=teacher_mail,
                password_hash=None,
                nickname="durable",
                email_normalized=teacher_mail,
                email_verified_at=_T0,
                phone_e164=teacher_phone,
                role=Role.TEACHER,
                status=UserStatus.ACTIVE,
            )
        )
        session.add(
            User(
                username=student_number,
                password_hash=None,
                nickname="durable-student",
                phone_e164=student_phone,
                role=Role.STUDENT,
                status=UserStatus.ACTIVE,
            )
        )
        await session.commit()

    facade = UnifiedLoginService(
        clock=api_clock,
        email_login=EmailLoginService(
            sender=api_email,
            redis=api_redis,
            clock=api_clock,
            hmac_secret=get_settings().otp_hmac_secret,
        ),
        mobile_login=MobileLoginService(
            sms_sender=api_sms,
            redis=api_redis,
            clock=api_clock,
            hmac_secret=get_settings().otp_hmac_secret,
        ),
        staff=None,  # type: ignore[arg-type]  # request arms never touch it
        sessions=None,  # type: ignore[arg-type]
    )
    async with AsyncSession(db_engine) as session:
        await facade.request(session, teacher_mail)
        await facade.request(session, teacher_phone)
        await facade.request(session, student_phone)

    async with AsyncSession(db_engine) as verify:
        email_rows = (
            await verify.scalars(
                select(LoginEmailChallenge).where(
                    LoginEmailChallenge.email_normalized == teacher_mail
                )
            )
        ).all()
        mobile_rows = (
            await verify.scalars(
                select(LoginMobileChallenge).where(
                    LoginMobileChallenge.phone_e164.in_((teacher_phone, student_phone))
                )
            )
        ).all()
    assert len(email_rows) == 1, "the email challenge died with the transaction"
    assert len(mobile_rows) == 2, "a mobile challenge died with the transaction"

    async with AsyncSession(db_engine) as cleanup:
        await cleanup.execute(
            delete(LoginEmailChallenge).where(
                LoginEmailChallenge.email_normalized == teacher_mail
            )
        )
        await cleanup.execute(
            delete(LoginMobileChallenge).where(
                LoginMobileChallenge.phone_e164.in_((teacher_phone, student_phone))
            )
        )
        await cleanup.execute(
            delete(User).where(User.username.in_((teacher_mail, student_number)))
        )
        await cleanup.commit()
    await api_redis.flushdb()


async def test_verify_email_arm_admin_answers_totp_required(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_email: FakeEmailSender,
) -> None:
    """The email arm: a TEACHER completes at verify with the nested
    token envelope; an ADMIN answers totp_required, and the ONE totp
    endpoint finishes the email-domain pending credential."""
    secret = pyotp.random_base32()
    admin = await _seed_user(
        db_session,
        username=f"uni-admin-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        totp_secret=secret,
    )
    await db_session.commit()

    requested = await client.post(
        "/api/v1/auth/challenge/request",
        json={"identifier": admin.email_normalized},
    )
    assert requested.status_code == 202, requested.text
    code = str(api_email.messages[-1].variables["code"])

    verified = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": admin.email_normalized, "code": code},
    )
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["status"] == "totp_required"
    pending_token = body["pending_token"]

    finished = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": pending_token,
            "totp_code": _totp_code(secret, api_clock),
        },
    )
    assert finished.status_code == 200, finished.text
    done = finished.json()
    assert done["status"] == "authenticated"
    assert done["role"] == "ADMIN"
    assert done["tokens"]["access_token"]
    assert done["tokens"]["token_type"] == "bearer"


async def test_verify_phone_arms_answer_all_three_states(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    """The phone arms: a STUDENT completes, a TEACHER completes, and an
    UNREGISTERED phone answers the entry shape with a register_token."""
    student = await _seed_user(
        db_session,
        username=f"2025{int(uuid4().hex, 16) % 10**7:07d}",
        role=Role.STUDENT,
        phone="+8613800160011",
    )
    teacher = await _seed_user(
        db_session,
        username=f"uni-teacher2-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.TEACHER,
        phone="+8613800160012",
    )
    await db_session.commit()

    for phone, role in (
        (student.phone_e164, "STUDENT"),
        (teacher.phone_e164, "TEACHER"),
    ):
        requested = await client.post(
            "/api/v1/auth/challenge/request", json={"identifier": phone}
        )
        assert requested.status_code == 202, requested.text
        code = str(api_sms.messages[-1].variables["code"])
        verified = await client.post(
            "/api/v1/auth/challenge/verify",
            json={"identifier": phone, "code": code},
        )
        assert verified.status_code == 200, verified.text
        body = verified.json()
        assert body["status"] == "authenticated", body
        assert body["role"] == role
        assert body["tokens"]["access_token"]

    fresh_phone = "+8613800160013"
    entry = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": fresh_phone}
    )
    assert entry.status_code == 202, entry.text
    code = str(api_sms.messages[-1].variables["code"])
    unregistered = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": fresh_phone, "code": code},
    )
    assert unregistered.status_code == 200, unregistered.text
    body = unregistered.json()
    assert body["status"] == "unregistered"
    assert body["register_token"]


async def test_totp_endpoint_serves_both_pending_domains(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
    api_email: FakeEmailSender,
) -> None:
    """ONE totp endpoint, TWO pending domains: the email-login domain
    and the staff-mobile domain both finish here (a wrong-domain GETDEL
    misses and burns nothing), and a bogus pending credential answers
    the single invalid envelope."""
    email_secret = pyotp.random_base32()
    admin_email = await _seed_user(
        db_session,
        username=f"uni-admin2-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        totp_secret=email_secret,
    )
    phone_secret = pyotp.random_base32()
    admin_phone = await _seed_user(
        db_session,
        username=f"uni-admin3-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        phone="+8613800160021",
        totp_secret=phone_secret,
    )
    await db_session.commit()

    # email-domain pending credential
    await client.post(
        "/api/v1/auth/challenge/request",
        json={"identifier": admin_email.email_normalized},
    )
    email_code = str(api_email.messages[-1].variables["code"])
    email_pending = (
        await client.post(
            "/api/v1/auth/challenge/verify",
            json={"identifier": admin_email.email_normalized, "code": email_code},
        )
    ).json()["pending_token"]

    via_email = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": email_pending,
            "totp_code": _totp_code(email_secret, api_clock),
        },
    )
    assert via_email.status_code == 200, via_email.text
    assert via_email.json()["status"] == "authenticated"

    # staff-mobile-domain pending credential
    await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": admin_phone.phone_e164}
    )
    phone_code = str(api_sms.messages[-1].variables["code"])
    phone_pending = (
        await client.post(
            "/api/v1/auth/challenge/verify",
            json={"identifier": admin_phone.phone_e164, "code": phone_code},
        )
    ).json()["pending_token"]

    via_phone = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": phone_pending,
            "totp_code": _totp_code(phone_secret, api_clock),
        },
    )
    assert via_phone.status_code == 200, via_phone.text
    assert via_phone.json()["status"] == "authenticated"

    bogus = await client.post(
        "/api/v1/auth/challenge/totp",
        json={"pending_token": "not-a-pending-token", "totp_code": "123456"},
    )
    assert bogus.status_code == 400, bogus.text
    assert bogus.json()["error"]["code"] == "CHALLENGE_CODE_INVALID"


async def test_wrong_codes_answer_the_single_challenge_envelope(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
) -> None:
    """Wrong code → CHALLENGE_CODE_INVALID on the unified verify arms —
    one code for wrong/expired/exhausted (the branch reason stays in
    server logs), the uniform anti-enumeration face of §5.1."""
    teacher = await _seed_user(
        db_session,
        username=f"uni-teacher3-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.TEACHER,
    )
    await db_session.commit()
    await client.post(
        "/api/v1/auth/challenge/request",
        json={"identifier": teacher.email_normalized},
    )
    assert len(api_email.messages) == 1

    wrong = await client.post(
        "/api/v1/auth/challenge/verify",
        json={"identifier": teacher.email_normalized, "code": "000000"},
    )
    assert wrong.status_code == 400, wrong.text
    assert wrong.json()["error"]["code"] == "CHALLENGE_CODE_INVALID"


async def test_remembered_probe_and_empty_body_verify_leg(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    """The remembered face: the probe answers a masked phone only for
    the banner's audience (a cookie-bound STUDENT), and the EMPTY-body
    verify opens the session on the cookie alone — but an ADMIN cookie
    still owes the second factor (totp_required, never exempted)."""
    student = await _seed_user(
        db_session,
        username=f"2025{int(uuid4().hex, 16) % 10**7:07d}",
        role=Role.STUDENT,
        phone="+8613800160031",
    )
    await db_session.commit()

    no_cookie = await client.get("/api/v1/auth/challenge/remembered")
    assert no_cookie.status_code == 204, no_cookie.text

    # Mint the student device cookie through a remembered verify.
    await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": student.phone_e164}
    )
    code = str(api_sms.messages[-1].variables["code"])
    login = await client.post(
        "/api/v1/auth/challenge/verify",
        json={
            "identifier": student.phone_e164,
            "code": code,
            "remember_device": True,
        },
    )
    assert login.status_code == 200, login.text
    assert login.json()["status"] == "authenticated"
    cookie = _device_cookie(login)

    probed = await client.get(
        "/api/v1/auth/challenge/remembered",
        cookies=_cookies(cookie),
    )
    assert probed.status_code == 200, probed.text
    masked = probed.json()["phone_masked"]
    assert masked.startswith("+86")
    assert student.phone_e164 not in masked

    # The cookie alone re-opens the student session: empty-body verify.
    continued = await client.post(
        "/api/v1/auth/challenge/verify", json={}, cookies=_cookies(cookie)
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] == "authenticated"
    assert continued.json()["role"] == "STUDENT"


async def test_empty_body_verify_still_owes_admin_the_second_factor(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
) -> None:
    """A cookie-bound ADMIN is never exempted on the code legs: the
    empty-body verify answers totp_required (the free-TOTP semantics
    live ONLY on the /auth/login password leg's cookie honoring)."""
    secret = pyotp.random_base32()
    admin = await _seed_user(
        db_session,
        username=f"uni-admin4-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        phone="+8613800160041",
        totp_secret=secret,
    )
    await db_session.commit()

    await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": admin.phone_e164}
    )
    code = str(api_sms.messages[-1].variables["code"])
    pending = (
        await client.post(
            "/api/v1/auth/challenge/verify",
            json={
                "identifier": admin.phone_e164,
                "code": code,
                "remember_device": True,
            },
        )
    ).json()
    assert pending["status"] == "totp_required"

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

    # A STAFF binding is not the banner's audience: the probe stays 204.
    probed = await client.get(
        "/api/v1/auth/challenge/remembered", cookies=_cookies(cookie)
    )
    assert probed.status_code == 204, probed.text

    continued = await client.post(
        "/api/v1/auth/challenge/verify", json={}, cookies=_cookies(cookie)
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] == "totp_required"


async def test_login_identifier_form_covers_three_identifiers(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
) -> None:
    """The /auth/login identifier form: student number and student
    phone both reach the student password flow (phone first — the #73
    semantics), a staff email reaches the staff password flow, and the
    RETIRED {username, password} shape answers the schema 422 (PR-3)."""
    student = await _seed_user(
        db_session,
        username=f"2025{int(uuid4().hex, 16) % 10**7:07d}",
        role=Role.STUDENT,
        phone="+8613800160051",
    )
    teacher = await _seed_user(
        db_session,
        username=f"uni-teacher4-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.TEACHER,
    )
    await db_session.commit()

    by_number = await client.post(
        "/api/v1/auth/login",
        json={"identifier": student.username, "password": _PASSWORD},
    )
    assert by_number.status_code == 200, by_number.text
    body = by_number.json()
    assert body["status"] == "authenticated"
    assert body["role"] == "STUDENT"
    assert body["tokens"]["access_token"]

    by_phone = await client.post(
        "/api/v1/auth/login",
        json={"identifier": student.phone_e164, "password": _PASSWORD},
    )
    assert by_phone.status_code == 200, by_phone.text
    assert by_phone.json()["status"] == "authenticated"

    by_email = await client.post(
        "/api/v1/auth/login",
        json={"identifier": teacher.email_normalized, "password": _PASSWORD},
    )
    assert by_email.status_code == 200, by_email.text
    staff_body = by_email.json()
    assert staff_body["status"] == "authenticated"
    assert staff_body["role"] == "TEACHER"

    # PR-3: the legacy {username, password} form is RETIRED — a 422
    # naming the unknown member, the unified form the only shape.
    legacy = await client.post(
        "/api/v1/auth/login",
        json={"username": student.username, "password": _PASSWORD},
    )
    assert legacy.status_code == 422, legacy.text


async def test_login_admin_leg_honors_the_remembered_cookie(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
) -> None:
    """The ADMIN password leg owes TOTP — unless a valid remembered
    cookie rides the request (the staff/login 30-day re-login
    semantics, preserved on the unified form): password alone answers
    totp_required; password + cookie answers authenticated directly."""
    secret = pyotp.random_base32()
    admin = await _seed_user(
        db_session,
        username=f"uni-admin5-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        phone="+8613800160061",
        totp_secret=secret,
    )
    await db_session.commit()

    cold = await client.post(
        "/api/v1/auth/login",
        json={"identifier": admin.email_normalized, "password": _PASSWORD},
    )
    assert cold.status_code == 200, cold.text
    assert cold.json()["status"] == "totp_required"

    # Mint the remembered cookie through the code flow's totp leg.
    await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": admin.phone_e164}
    )
    code = str(api_sms.messages[-1].variables["code"])
    pending = (
        await client.post(
            "/api/v1/auth/challenge/verify",
            json={"identifier": admin.phone_e164, "code": code},
        )
    ).json()
    finished = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": pending["pending_token"],
            "totp_code": _totp_code(secret, api_clock),
            "remember_device": True,
        },
    )
    assert finished.status_code == 200, finished.text
    cookie = _device_cookie(finished)

    warm = await client.post(
        "/api/v1/auth/login",
        json={
            "identifier": admin.email_normalized,
            "password": _PASSWORD,
            "remember_device": True,
        },
        cookies=_cookies(cookie),
    )
    assert warm.status_code == 200, warm.text
    warm_body = warm.json()
    assert warm_body["status"] == "authenticated"
    assert warm_body["role"] == "ADMIN"
    assert warm_body["tokens"]["access_token"]


async def test_empty_body_verify_serves_pending_approval_students(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    """The banner's promise holds for a PENDING_APPROVAL student too
    (the #115 runtime finding): the probe answers the masked phone for
    ACTIVE and PENDING_APPROVAL alike (the #87 audience), and the
    one-click continue must honor the same audience — a PENDING student
    logs in and browses the frozen home (RA3); a banner that invites
    and then refuses is a dead-end UX."""
    student = await _seed_user(
        db_session,
        username=f"2025{int(uuid4().hex, 16) % 10**7:07d}",
        role=Role.STUDENT,
        phone="+8613800160071",
    )
    student.status = UserStatus.PENDING_APPROVAL
    await db_session.commit()

    await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": student.phone_e164}
    )
    code = str(api_sms.messages[-1].variables["code"])
    login = await client.post(
        "/api/v1/auth/challenge/verify",
        json={
            "identifier": student.phone_e164,
            "code": code,
            "remember_device": True,
        },
    )
    assert login.status_code == 200, login.text
    cookie = _device_cookie(login)

    probed = await client.get(
        "/api/v1/auth/challenge/remembered", cookies=_cookies(cookie)
    )
    assert probed.status_code == 200, probed.text

    continued = await client.post(
        "/api/v1/auth/challenge/verify", json={}, cookies=_cookies(cookie)
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] == "authenticated"
    assert continued.json()["role"] == "STUDENT"


async def _email_arm_pending(
    client: httpx.AsyncClient,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
    email: str,
) -> str:
    """One email-arm cycle with the wall-clock cooldown key cleared
    (the TTL runs on real time; the cooldown CRAFT has its own suite):
    request, verify the mailed code, return the pending credential."""
    await api_redis.delete(f"emailcode:cooldown:{email}")
    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": email}
    )
    assert requested.status_code == 202, requested.text
    code = str(api_email.messages[-1].variables["code"])
    pending = (
        await client.post(
            "/api/v1/auth/challenge/verify",
            json={"identifier": email, "code": code},
        )
    ).json()
    assert pending["status"] == "totp_required", pending
    return pending["pending_token"]


async def test_admin_cookie_completes_at_email_verify(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
) -> None:
    """An ADMIN's valid remembered cookie replaces the SECOND factor,
    not the code (the #87 craft on the email arm): a valid mailed code
    + cookie completes AT verify — no totp_required mid-state — while
    a wrong code stays the uniform single envelope even with one."""
    secret = pyotp.random_base32()
    admin = await _seed_user(
        db_session,
        username=f"uni-admin6-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        totp_secret=secret,
    )
    await db_session.commit()

    first = await _email_arm_pending(
        client, api_email, api_redis, admin.email_normalized
    )
    warm_cycle = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": first,
            "totp_code": _totp_code(secret, api_clock),
            "remember_device": True,
        },
    )
    assert warm_cycle.status_code == 200, warm_cycle.text
    cookie = _device_cookie(warm_cycle)

    # Wrong code: the cookie saves no one from a bad code.
    wrong = await client.post(
        "/api/v1/auth/challenge/verify",
        json={
            "identifier": admin.email_normalized,
            "code": "000000",
            "remember_device": True,
        },
        cookies=_cookies(cookie),
    )
    assert wrong.status_code == 400, wrong.text
    assert wrong.json()["error"]["code"] == "CHALLENGE_CODE_INVALID"

    # Valid code + cookie: the login completes HERE, no totp leg.
    await api_redis.delete(f"emailcode:cooldown:{admin.email_normalized}")
    await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": admin.email_normalized}
    )
    good_code = str(api_email.messages[-1].variables["code"])
    warm = await client.post(
        "/api/v1/auth/challenge/verify",
        json={
            "identifier": admin.email_normalized,
            "code": good_code,
            "remember_device": True,
        },
        cookies=_cookies(cookie),
    )
    assert warm.status_code == 200, warm.text
    assert warm.json()["status"] == "authenticated"
    assert warm.json()["role"] == "ADMIN"
    assert warm.json()["tokens"]["access_token"]


async def test_email_totp_wrong_code_counts_once_but_cycles_reset(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_clock: FrozenClock,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
) -> None:
    """The email-arm totp leg's wrong-code branch counts (server-side)
    but the FULL cycle's verify success RESETS the streak — the
    accumulation-to-lock lives on the mobile arm (the lockout suite);
    here one failure leaves the account open for the next valid cycle."""
    secret = pyotp.random_base32()
    admin = await _seed_user(
        db_session,
        username=f"uni-admin7-{uuid4().hex[:8]}@campus.example.edu",
        role=Role.ADMIN,
        totp_secret=secret,
    )
    await db_session.commit()

    pending = await _email_arm_pending(
        client, api_email, api_redis, admin.email_normalized
    )
    wrong = await client.post(
        "/api/v1/auth/challenge/totp",
        json={"pending_token": pending, "totp_code": "000000"},
    )
    assert wrong.status_code == 400, wrong.text
    assert wrong.json()["error"]["code"] == "CHALLENGE_CODE_INVALID"

    pending2 = await _email_arm_pending(
        client, api_email, api_redis, admin.email_normalized
    )
    recovered = await client.post(
        "/api/v1/auth/challenge/totp",
        json={
            "pending_token": pending2,
            "totp_code": _totp_code(secret, api_clock),
        },
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "authenticated"


async def test_email_totp_leg_forces_setup_for_unconfirmed_admin(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_email: FakeEmailSender,
    api_redis: aioredis.Redis,
) -> None:
    """The EMAIL pending domain keeps its setup-forcing signal (unlike
    the mobile domain's uniform fold): an accepted-but-unconfirmed
    ADMIN answers TOTP_SETUP_REQUIRED at the totp leg."""
    from app.core.security import hash_password as _hash_pw
    from app.modules.identity.models import User as _User

    mail = f"uni-admin8-{uuid4().hex[:8]}@campus.example.edu"
    db_session.add(
        _User(
            username=mail,
            password_hash=_hash_pw(_PASSWORD),
            nickname="未确认管理员",
            email_normalized=mail,
            email_verified_at=_T0,
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
    )
    await db_session.commit()

    pending = await _email_arm_pending(client, api_email, api_redis, mail)
    forced = await client.post(
        "/api/v1/auth/challenge/totp",
        json={"pending_token": pending, "totp_code": "123456"},
    )
    assert forced.status_code == 403, forced.text
    assert forced.json()["error"]["code"] == "TOTP_SETUP_REQUIRED"
