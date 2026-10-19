# backend/tests/integration/identity/test_mobile_code_entry_registration.py
"""Phone-first registration (the queue-jump batch, owner pain point).

The mobile-code channel becomes the ENTRY: request SMSes a code for
EVERY well-formed phone (registered or not — the challenge table keys
by phone, nothing is account-gated); verify with a valid code and NO
account answers {status:"unregistered", register_token} — a 5-minute
single-use proof that THIS phone just passed a code check; the register
endpoint trades that proof for a PENDING_APPROVAL account with the
phone bound and a session minted on the spot (the register-ends-logged-
in semantics). The classic /auth/register family stays untouched as
the second door; the staff channel is unaffected.
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
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.providers import (
    get_identity_redis,
    get_rate_limiter,
    get_sms_sender,
)
from tests.fakes.integrations import FakeRateLimiter, FakeSmsSender

pytestmark = pytest.mark.integration

_ENTRY_REDIS_DB = 14
# Anchored to the real now: the codec validates iat against the wall
# clock (the FrozenClock lesson).
_T0 = datetime.now(UTC).replace(microsecond=0)
_PHONE = "+8613801355551"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_ENTRY_REDIS_DB}"))


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


async def _request_code(client: httpx.AsyncClient) -> str:
    requested = await client.post(
        "/api/v1/auth/challenge/request", json={"identifier": _PHONE}
    )
    assert requested.status_code == 202, requested.text
    return requested


async def _code_and_verify(api_sms: FakeSmsSender, client: httpx.AsyncClient):
    code = api_sms.messages[-1].variables["code"]
    return await client.post(
        "/api/v1/auth/challenge/verify", json={"identifier": _PHONE, "code": code}
    )


async def test_unregistered_phone_receives_a_code_and_an_entry_token(
    client: httpx.AsyncClient, api_sms: FakeSmsSender
) -> None:
    await _request_code(client)
    assert len(api_sms.messages) == 1  # no account, still a real code

    verified = await _code_and_verify(api_sms, client)

    assert verified.status_code == 200, verified.text
    body = verified.json()
    assert body["status"] == "unregistered"
    assert body["register_token"]


async def test_register_token_mints_a_logged_in_pending_account(
    client: httpx.AsyncClient, api_sms: FakeSmsSender
) -> None:
    await _request_code(client)
    verified = await _code_and_verify(api_sms, client)
    register_token = verified.json()["register_token"]

    registered = await client.post(
        "/api/v1/auth/mobile-code/register",
        json={
            "register_token": register_token,
            "student_number": "2025001355551",
            "password": "correct-horse-battery",
        },
    )
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["user"]["username"] == "2025001355551"
    assert body["user"]["role"] == "STUDENT"
    assert body["user"]["status"] == "PENDING_APPROVAL"
    assert set(body) == {"user", "tokens"}  # the register-ends-logged-in shape

    me = await client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {body['tokens']['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["status"] == "PENDING_APPROVAL"


async def test_register_token_is_single_use(
    client: httpx.AsyncClient, api_sms: FakeSmsSender
) -> None:
    await _request_code(client)
    verified = await _code_and_verify(api_sms, client)
    token = verified.json()["register_token"]
    first = await client.post(
        "/api/v1/auth/mobile-code/register",
        json={"register_token": token, "student_number": "2025001355556"},
    )
    assert first.status_code == 201, first.text

    replay = await client.post(
        "/api/v1/auth/mobile-code/register",
        json={"register_token": token, "student_number": "2025001355557"},
    )
    assert replay.status_code == 400, replay.text


async def test_taken_student_number_is_a_conflict(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    import uuid

    from app.core.security import hash_password
    from app.modules.identity.models import User

    taken = f"2025{int(uuid.uuid4()) % 10**7:07d}"
    db_session.add(
        User(
            username=taken,
            password_hash=hash_password("placeholder-long-password"),
            nickname="占号",
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
    )
    await db_session.commit()

    await _request_code(client)
    verified = await _code_and_verify(api_sms, client)

    registered = await client.post(
        "/api/v1/auth/mobile-code/register",
        json={
            "register_token": verified.json()["register_token"],
            "student_number": taken,
        },
    )
    assert registered.status_code == 409, registered.text
    assert registered.json()["error"]["code"] == "USERNAME_ALREADY_EXISTS"


async def test_registered_phone_still_logs_in_directly(
    client: httpx.AsyncClient, db_session: AsyncSession, api_sms: FakeSmsSender
) -> None:
    """The existing student path is untouched: a REGISTERED phone's
    verify still answers the token pair, not the entry token."""
    from app.core.security import hash_password
    from app.modules.identity.models import User

    db_session.add(
        User(
            username="202500135558",
            password_hash=hash_password("placeholder-long-password"),
            nickname="在册",
            phone_e164=_PHONE,
            role=Role.STUDENT,
            status=UserStatus.ACTIVE,
        )
    )
    await db_session.commit()

    await _request_code(client)
    verified = await _code_and_verify(api_sms, client)

    assert verified.status_code == 200, verified.text
    assert verified.json()["tokens"]["access_token"]
