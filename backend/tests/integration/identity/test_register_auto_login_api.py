# backend/tests/integration/identity/test_register_auto_login_api.py
"""Registration ends in a logged-in session (V1.1 login iteration, item 4).

The owner feedback: registering and then logging in immediately hits the
60s SMS cooldown — pointless, the student JUST proved the phone. The
register response grows a ``tokens`` envelope beside the user view, and
the cookie set matches the login path exactly (refresh HttpOnly-only,
csrf mirror, cq_shell). The minted session is usable on the spot: /me
answers with the PENDING_APPROVAL browsing state (#96 semantics —
register lands directly in the frozen-home browsing mode).
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.providers import (
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from app.modules.identity.routing_common import (
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    SHELL_COOKIE_NAME,
)
from tests.fakes.integrations import FakeRateLimiter, FakeSmsSender

pytestmark = pytest.mark.integration

_REG_REDIS_DB = 9
_T0 = datetime.now(UTC).replace(microsecond=0)
_STUDENT = "20250017777"
_PHONE_RAW = "13800167777"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_REG_REDIS_DB}"))


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


async def test_register_answers_envelope_with_usable_session(
    client: httpx.AsyncClient, api_sms: FakeSmsSender
) -> None:
    challenged = await client.post(
        "/api/v1/auth/phone/challenges", json={"phone": _PHONE_RAW}
    )
    assert challenged.status_code == 200, challenged.text
    code = api_sms.messages[0].variables["code"]
    verified = await client.post(
        f"/api/v1/auth/phone/challenges/{challenged.json()['challenge_id']}/verify",
        json={"code": code},
    )
    assert verified.status_code == 200, verified.text

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "student_number": _STUDENT,
            "phone_token": verified.json()["phone_token"],
            "nickname": "直进同学",
        },
    )
    assert registered.status_code == 201, registered.text

    body = registered.json()
    assert set(body) == {"user", "tokens"}
    assert body["user"]["username"] == _STUDENT
    assert body["user"]["status"] == "PENDING_APPROVAL"
    assert set(body["tokens"]) == {"access_token", "csrf_token", "token_type"}
    assert body["tokens"]["token_type"] == "bearer"

    # The login-path cookie set rides the same response.
    set_cookies = {
        cookie.split("=", 1)[0] for cookie in registered.headers.get_list("set-cookie")
    }
    assert {REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME, SHELL_COOKIE_NAME} <= set_cookies

    # The minted session works on the spot: /me answers the frozen-home
    # browsing state without a second login round-trip.
    me = await client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {body['tokens']['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["status"] == "PENDING_APPROVAL"
