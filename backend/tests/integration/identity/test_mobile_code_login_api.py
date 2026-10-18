# backend/tests/integration/identity/test_mobile_code_login_api.py
"""Student mobile-code login over the real app (V1.1 §3).

The email-code discipline mirrored onto SMS: constant 202 with the
cooldown armed for ANY phone, 5-attempt challenges with the keyed
digest, the shared account lockout, and the remembered-device cookie
(#87 craft — with the cookie, verify answers tokens with NO code at
all: the cookie is the credential).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http import cookies as http_cookies
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.providers import (
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from app.modules.identity.routing_common import SHELL_COOKIE_NAME
from tests.fakes.integrations import FakeRateLimiter, FakeSmsSender

pytestmark = pytest.mark.integration

_MOBILE_REDIS_DB = 7
_T0 = datetime.now(UTC).replace(microsecond=0)
_STUDENT = "20250012345"
_PASSWORD = "correct-horse-battery"
_PHONE = "+8613800123456"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_MOBILE_REDIS_DB}"))


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


async def _seed_student(
    db: AsyncSession, *, status: UserStatus = UserStatus.PENDING_APPROVAL
) -> User:
    user = User(
        username=_STUDENT,
        password_hash=hash_password(_PASSWORD),
        nickname="手机码同学",
        phone_e164=_PHONE,
        role=Role.STUDENT,
        status=status,
    )
    db.add(user)
    await db.flush()
    return user


def _last_code(api_sms: FakeSmsSender) -> str:
    assert api_sms.messages, "no sms was sent"
    return str(api_sms.messages[-1].variables["code"])


async def _request(client: httpx.AsyncClient, phone: str) -> httpx.Response:
    return await client.post("/api/v1/auth/mobile-code/request", json={"phone": phone})


async def _verify(
    client: httpx.AsyncClient, phone: str, code: str | None, *, remember: bool = False
) -> httpx.Response:
    body: dict[str, object] = {"phone": phone}
    if code is not None:
        body["code"] = code
    if remember:
        body["remember_device"] = True
    return await client.post("/api/v1/auth/mobile-code/verify", json=body)


def _envelope(response: httpx.Response) -> dict:
    return response.json()["error"]


def _shell_header(response: httpx.Response) -> str:
    matches = [
        h
        for h in response.headers.get_list("set-cookie")
        if h.startswith(f"{SHELL_COOKIE_NAME}=")
    ]
    assert len(matches) == 1
    return matches[0]


async def test_pending_approval_student_logs_in_end_to_end(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    """The headline flow: a PENDING_APPROVAL student registers (no
    password needed for this path), requests a code, and logs in —
    browsing-grade session with the student shell cookie."""
    await _seed_student(db_session)

    requested = await _request(client, _PHONE)
    assert requested.status_code == 202, requested.text
    assert requested.json() == {}

    verified = await _verify(client, _PHONE, _last_code(api_sms))
    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["access_token"]
    header = _shell_header(verified)
    assert re.search(r"cq_shell=student", header)
    assert "HttpOnly" in _shell_header(verified)

    me = await client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["status"] == "PENDING_APPROVAL"


async def test_request_is_constant_202_and_uniform_429(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    await _seed_student(db_session)

    requested = await _request(client, "+8613800099999")  # unknown phone
    assert requested.status_code == 202, requested.text
    # The entry channel: a real code for the unknown phone (count only
    # THIS phone's sends — the shared fake carries the file's history).
    assert [m for m in api_sms.messages if m.to == "+8613800099999"]
    repeat_unknown = await _request(client, "+8613800099999")
    assert repeat_unknown.status_code == 429
    assert _envelope(repeat_unknown)["code"] == "RATE_LIMITED"

    assert (await _request(client, _PHONE)).status_code == 202
    repeat = await _request(client, _PHONE)
    assert repeat.status_code == 429
    assert _envelope(repeat)["code"] == "RATE_LIMITED"
    # (Both phones were SMSed — the entry contract: every well-formed
    # phone gets a real code.)


async def test_wrong_code_five_times_then_lockout_envelope(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    await _seed_student(db_session)
    await _request(client, _PHONE)

    for _ in range(5):
        wrong = await _verify(client, _PHONE, "000000")
        assert wrong.status_code == 400, wrong.text
        assert _envelope(wrong)["code"] == "MOBILE_CODE_INVALID"

    sixth = await _verify(client, _PHONE, _last_code(api_sms))
    assert sixth.status_code == 429, sixth.text
    error = _envelope(sixth)
    assert error["code"] == "ACCOUNT_LOCKED"
    assert 0 < error["details"]["retry_after_seconds"] <= 900


async def test_remembered_device_cookie_logs_in_without_any_code(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    """The #87 craft, student edition: with the remembered-device cookie
    the verify call carries NO code at all — the cookie is the
    credential (equivalent security to a password session lifetime)."""
    await _seed_student(db_session)
    await _request(client, _PHONE)
    first = await _verify(client, _PHONE, _last_code(api_sms), remember=True)
    assert first.status_code == 200, first.text
    jar = http_cookies.SimpleCookie()
    jar.load(
        next(
            h
            for h in first.headers.get_list("set-cookie")
            if h.startswith("cq_staff_device=")
        )
    )
    cookie_value = jar["cq_staff_device"].value
    assert cookie_value

    # The remembered path: NO code in the body, only the cookie.
    remembered = await client.post(
        "/api/v1/auth/mobile-code/verify",
        json={"phone": _PHONE},
        cookies={"cq_staff_device": cookie_value},
    )
    assert remembered.status_code == 200, remembered.text
    assert remembered.json()["access_token"]
    # And it SMSed nothing new.
    assert len(api_sms.messages) == 1


async def test_non_student_or_inactive_phone_is_noop(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    teacher = User(
        username="mobile-staff@campus.example.edu",
        password_hash=None,
        nickname="员工",
        phone_e164="+8613800177777",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    banned = User(
        username="20250017776",
        password_hash="x",
        nickname="被封",
        phone_e164="+8613800177776",
        role=Role.STUDENT,
        status=UserStatus.BANNED,
    )
    db_session.add_all([teacher, banned])
    await db_session.flush()

    # The entry-channel change (the queue-jump batch): EVERY
    # well-formed phone now receives a real code — a staff-bound or
    # banned phone is no longer silently skipped on REQUEST (the
    # account check moved to VERIFY: such phones get an entry token,
    # and the register door refuses a taken phone). The 202s hold.
    for phone in ("+8613800177777", "+8613800177776"):
        response = await _request(client, phone)
        assert response.status_code == 202, response.text

    assert len(api_sms.messages) == 2


async def test_sms_failure_cleans_up_and_answers_502(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    api_sms: FakeSmsSender,
) -> None:
    """The email-service hotfix discipline, mobile side: a provider
    failure answers 502 with this request's traces gone — no challenge
    row, cooldown released."""
    from sqlalchemy import select as _select

    from app.core.errors import BusinessError
    from app.integrations.errors import PermanentProviderError
    from app.modules.identity.models import LoginMobileChallenge

    await _seed_student(db_session)
    api_sms.failures.append(PermanentProviderError("boom"))
    import pytest as _pytest

    with _pytest.raises(BusinessError):
        # Service-level: the route would render the 502 envelope; the
        # cleanup contract is what we pin here.
        from urllib.parse import urlsplit as _us
        from urllib.parse import urlunsplit as _uu

        from redis.asyncio import from_url as _from_url

        from app.core.clock import FrozenClock
        from app.core.config import get_settings
        from app.modules.identity.mobile_login_service import MobileLoginService

        base = get_settings().redis_url
        parts = _us(base)
        url = _uu(parts._replace(path="/7"))
        redis = _from_url(url, decode_responses=True)
        try:
            service = MobileLoginService(
                sms_sender=api_sms,
                redis=redis,
                clock=FrozenClock(datetime.now(UTC).replace(microsecond=0)),
                hmac_secret=get_settings().otp_hmac_secret,
            )
            await service.request_code(db_session, _PHONE)
        finally:
            await redis.aclose()

    assert (
        await db_session.execute(
            _select(LoginMobileChallenge).where(
                LoginMobileChallenge.phone_e164 == _PHONE
            )
        )
    ).scalar_one_or_none() is None


async def test_mobile_code_probe_recovers_the_smsed_code(
    db_session: AsyncSession,
) -> None:
    """The e2e recovery channel (tests/e2e/mobile_code_probe.py): the
    probe rediscovers the code from the stored keyed digest alone."""
    from tests.e2e.mobile_code_probe import recover_mobile_code

    await _seed_student(db_session)

    from app.core.clock import FrozenClock
    from app.core.config import get_settings
    from app.modules.identity.mobile_login_service import MobileLoginService

    class _Redis:
        def __init__(self) -> None:
            self.strings: dict[str, str] = {}

        async def set(self, key, value, ex=None, nx=False):
            if nx and key in self.strings:
                return None
            self.strings[key] = value
            return True

        async def get(self, key):
            return self.strings.get(key)

        async def delete(self, *keys):
            for key in keys:
                self.strings.pop(key, None)
            return len(keys)

    sms = FakeSmsSender()
    service = MobileLoginService(
        sms_sender=sms,
        redis=_Redis(),
        clock=FrozenClock(_T0),
        hmac_secret=get_settings().otp_hmac_secret,
    )
    await service.request_code(db_session, _PHONE)
    mailed = sms.messages[-1].variables["code"]

    challenge_id, recovered = await recover_mobile_code(db_session, _PHONE)
    assert recovered == str(mailed)

    with pytest.raises(LookupError):
        await recover_mobile_code(db_session, "+8613800099999")


async def test_pending_student_survives_refresh_rotation(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    """The auth-II e2e catch: a hard refresh is the browse path's
    foundation — the pending student's session must ROTATE, not die with
    ACCOUNT_NOT_ACTIVE (spec 2026-10-16 §2)."""
    import http.cookies as http_cookies

    from app.modules.identity.routing_common import (
        CSRF_COOKIE_NAME as _CSRF_C,
    )
    from app.modules.identity.routing_common import (
        REFRESH_COOKIE_NAME as _RC,
    )

    await _seed_student(db_session)  # PENDING_APPROVAL
    await _request(client, _PHONE)
    login = await _verify(client, _PHONE, _last_code(api_sms))
    assert login.status_code == 200, login.text
    csrf = login.json()["csrf_token"]

    jar = http_cookies.SimpleCookie()

    def _from(response, name):
        jar.load(
            next(
                h
                for h in response.headers.get_list("set-cookie")
                if h.startswith(f"{name}=")
            )
        )
        return jar[name].value

    cookies = {_RC: _from(login, _RC), _CSRF_C: csrf}
    rotated = await client.post(
        "/api/v1/auth/refresh",
        cookies=cookies,
        headers={"X-CSRF-Token": csrf},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["access_token"]

    me = await client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {rotated.json()['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["status"] == "PENDING_APPROVAL"
