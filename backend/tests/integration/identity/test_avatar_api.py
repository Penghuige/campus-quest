# backend/tests/integration/identity/test_avatar_api.py
"""Avatar upload / serve / delete over real HTTP (QA defect #4, D1-D5).

Drives the real app (``create_app()``) with the in-memory object-storage
fake, real PostgreSQL (outer-rollback harness), and Redis db 15 — same
scaffolding contract as ``test_identity_api``.

Frozen contract under test:

- ``POST /api/v1/me/avatar`` — multipart direct upload, ≤ 2 MiB, magic
  byte validated (png/jpeg/webp only); replacement swaps the stored
  object and deletes the previous one; every committed change appends a
  ``USER_AVATAR_CHANGED`` audit row.
- ``DELETE /api/v1/me/avatar`` — clears the pointer and deletes the
  object; deleting when none is held is 404.
- ``GET /api/v1/users/{id}/avatar`` — login-gated cross-user byte proxy
  (never a presigned redirect), ``ETag`` + ``Cache-Control: private``，
  If-None-Match → 304.
- The stored object key never appears in ANY response body, header, or
  audit payload (the same privacy discipline as submission keys).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.integrations.errors import TemporaryProviderError
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_object_storage,
    get_rate_limiter,
    get_sms_sender,
)
from app.modules.identity.session_service import SessionService
from tests.fakes.integrations import (
    FakeEmailSender,
    FakeObjectStorage,
    FakeRateLimiter,
    FakeSmsSender,
)

_OTP_TEST_REDIS_DB = 15
# Anchored to the real now (second precision): PyJWT wall-clock-validates
# iat/exp, so a business clock frozen ahead of (or far behind) the real
# now makes every minted token "not yet valid"/"expired" — the
# test_identity_api anchor.
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

AVATAR_MAX_BYTES = 2 * 1024 * 1024
AUDIT_ACTION = "USER_AVATAR_CHANGED"

# Canonical 67-byte 1x1 transparent PNG.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6200010000050001"
    "0d0a2db40000000049454e44ae426082"
)


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
def fake_storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def api_app(
    db_session: AsyncSession,
    api_redis: aioredis.Redis,
    api_clock: FrozenClock,
    api_sms: FakeSmsSender,
    api_email: FakeEmailSender,
    fake_limiter: FakeRateLimiter,
    fake_storage: FakeObjectStorage,
):
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_identity_redis] = lambda: api_redis
    app.dependency_overrides[get_rate_limiter] = lambda: fake_limiter
    app.dependency_overrides[get_sms_sender] = lambda: api_sms
    app.dependency_overrides[get_email_sender] = lambda: api_email
    app.dependency_overrides[get_object_storage] = lambda: fake_storage
    return app


@pytest_asyncio.fixture
async def client(api_app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_student(db: AsyncSession, username: str, nickname: str) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=nickname,
        role=Role.STUDENT.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _login(client: httpx.AsyncClient, username: str) -> dict:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _upload(
    client: httpx.AsyncClient, headers: dict[str, str], content: bytes
) -> object:
    # The upload body IS the image bytes (no multipart envelope): the
    # contract under test rejects a parsing dependency for a fieldless
    # body and keeps the read size-bounded during streaming.
    return client.post(
        "/api/v1/me/avatar",
        headers=headers | {"Content-Type": "image/png"},
        content=content,
    )


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}, error
    assert error["request_id"] == response.headers["X-Request-ID"]
    return error


async def _avatar_audit_rows(db: AsyncSession) -> list[AuditLog]:
    result = await db.execute(select(AuditLog).where(AuditLog.action == AUDIT_ACTION))
    return list(result.scalars())


def _assert_key_leakage(response: httpx.Response, *keys: str) -> None:
    """No object key may surface in the raw body or any header."""
    raw = response.text
    for header_value in response.headers.values():
        assert all(key not in header_value for key in keys), (
            f"object key leaked into header {header_value!r}"
        )
    assert all(key not in raw for key in keys), "object key leaked into body"


def _stored_avatar_key(storage: FakeObjectStorage, user: User) -> str:
    matches = [key for key in storage.objects if key.startswith(f"avatars/{user.id}/")]
    assert len(matches) == 1, f"expected exactly one avatar object: {matches}"
    return matches[0]


# --- upload --------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_stores_avatar_and_flips_has_avatar(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_storage: FakeObjectStorage,
) -> None:
    user = await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")

    response = await _upload(client, _bearer(tokens), _PNG_1PX)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_avatar"] is True
    assert body["nickname"] == "头像测试一"

    key = _stored_avatar_key(fake_storage, user)
    _assert_key_leakage(response, key)
    assert fake_storage.objects[key].content_type == "image/png"

    # /me mirrors the same projection.
    me = await client.get("/api/v1/me", headers=_bearer(tokens))
    assert me.status_code == 200, me.text
    assert me.json()["has_avatar"] is True

    rows = await _avatar_audit_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.target_type == "USER"
    assert row.target_id == str(user.id)
    assert row.actor_user_id == user.id
    serialized = json.dumps(
        [row.details, row.before_snapshot, row.after_snapshot], default=str
    )
    assert key not in serialized


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_rejects_oversize_payload(
    client: httpx.AsyncClient, db_session: AsyncSession, fake_storage: FakeObjectStorage
) -> None:
    await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")
    oversize = _PNG_1PX + b"\x00" * (AVATAR_MAX_BYTES - len(_PNG_1PX) + 1)

    response = await _upload(client, _bearer(tokens), oversize)
    assert response.status_code == 400
    assert _envelope(response)["code"] == "FILE_TOO_LARGE"
    assert not fake_storage.objects, "rejected payload must not be stored"
    rows = await _avatar_audit_rows(db_session)
    assert rows == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"GIF89a\x01\x00\x01\x00\x00\xff\x00",  # gif magic
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",  # svg: XSS vector
        b"",  # empty file part
    ],
)
async def test_upload_rejects_disallowed_magic_bytes(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_storage: FakeObjectStorage,
    payload: bytes,
) -> None:
    await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")

    response = await _upload(client, _bearer(tokens), payload)
    assert response.status_code == 400
    assert _envelope(response)["code"] == "VALIDATION_ERROR"
    assert not fake_storage.objects
    assert await _avatar_audit_rows(db_session) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_replacement_deletes_previous_object(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_storage: FakeObjectStorage,
) -> None:
    user = await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")

    first = await _upload(client, _bearer(tokens), _PNG_1PX)
    assert first.status_code == 200, first.text
    first_key = _stored_avatar_key(fake_storage, user)

    second = await _upload(client, _bearer(tokens), _PNG_1PX)
    assert second.status_code == 200, second.text
    assert second.json()["has_avatar"] is True

    second_key = _stored_avatar_key(fake_storage, user)
    assert second_key != first_key, "replacement must mint a fresh key"
    assert first_key in fake_storage.deleted_keys
    assert first_key not in fake_storage.objects
    rows = await _avatar_audit_rows(db_session)
    assert len(rows) == 2


# --- delete --------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_avatar_clears_pointer_and_object(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_storage: FakeObjectStorage,
) -> None:
    user = await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")
    assert (await _upload(client, _bearer(tokens), _PNG_1PX)).status_code == 200
    key = _stored_avatar_key(fake_storage, user)

    response = await client.delete("/api/v1/me/avatar", headers=_bearer(tokens))
    assert response.status_code == 200, response.text
    assert response.json()["has_avatar"] is False
    assert key in fake_storage.deleted_keys
    assert key not in fake_storage.objects

    me = await client.get("/api/v1/me", headers=_bearer(tokens))
    assert me.json()["has_avatar"] is False

    rows = await _avatar_audit_rows(db_session)
    assert len(rows) == 2  # upload + delete

    # Deleting again without an avatar held is 404, not a silent no-op.
    repeat = await client.delete("/api/v1/me/avatar", headers=_bearer(tokens))
    assert repeat.status_code == 404
    assert _envelope(repeat)["code"] == "NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replacement_survives_cleanup_failure_as_orphan(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_storage: FakeObjectStorage,
) -> None:
    # The quiet-cleanup contract (review finding): a PROVIDER failure on
    # the post-commit delete of the replaced object must degrade to an
    # orphan — never a 500 over a committed success (whose retry would
    # then hit the rate limit for nothing). ProviderError does NOT
    # derive from OSError, so this is exactly the branch that escaped
    # before the fix.
    user = await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")
    assert (await _upload(client, _bearer(tokens), _PNG_1PX)).status_code == 200
    old_key = _stored_avatar_key(fake_storage, user)

    real_delete = fake_storage.delete_object

    def _failing_delete(*, object_key: str) -> None:
        if object_key == old_key:
            # One-shot: only the replaced-object cleanup path fails.
            fake_storage.delete_object = real_delete  # type: ignore[method-assign]
            raise TemporaryProviderError("simulated provider outage")
        return real_delete(object_key=object_key)

    fake_storage.delete_object = _failing_delete  # type: ignore[method-assign]

    replaced = await _upload(client, _bearer(tokens), _PNG_1PX)
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["has_avatar"] is True

    # The row moved to the new key; the old object is orphaned in place
    # (still stored, never deleted) — harmless, prefix-reconcilable.
    keys = sorted(
        key
        for key in fake_storage.objects
        if key.startswith(f"avatars/{user.id}/")
    )
    assert len(keys) == 2  # the orphan plus the live replacement
    new_key = keys[1] if keys[0] == old_key else keys[0]
    assert new_key != old_key
    assert old_key in fake_storage.objects
    assert old_key not in fake_storage.deleted_keys
    rows = await _avatar_audit_rows(db_session)
    assert len(rows) == 2  # both commits landed; nothing rolled back


# --- cross-user serve ----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_avatar_cross_user_with_etag_and_304(
    client: httpx.AsyncClient, db_session: AsyncSession, fake_storage: FakeObjectStorage
) -> None:
    owner = await _seed_student(db_session, "20250101", "头像测试一")
    reader = await _seed_student(db_session, "20250102", "头像测试二")
    assert (
        await _upload(client, _bearer(await _login(client, "20250101")), _PNG_1PX)
    ).status_code == 200
    key = _stored_avatar_key(fake_storage, owner)
    reader_tokens = await _login(client, "20250102")

    url = f"/api/v1/users/{owner.id}/avatar"
    response = await client.get(url, headers=_bearer(reader_tokens))
    assert response.status_code == 200, response.text
    assert response.content == _PNG_1PX
    assert response.headers["Content-Type"].startswith("image/png")
    assert key not in response.text

    etag = response.headers.get("ETag")
    assert etag, "avatar response must carry an ETag"
    cache_control = response.headers.get("Cache-Control", "")
    assert "private" in cache_control and "max-age" in cache_control

    conditional = await client.get(
        url, headers=_bearer(reader_tokens) | {"If-None-Match": etag}
    )
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers.get("ETag") == etag

    # The reader has no avatar of their own: avatarless user serves 404.
    avatarless = await client.get(
        f"/api/v1/users/{reader.id}/avatar", headers=_bearer(reader_tokens)
    )
    assert avatarless.status_code == 404
    assert _envelope(avatarless)["code"] == "NOT_FOUND"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_avatar_requires_authentication(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _seed_student(db_session, "20250101", "头像测试一")
    response = await client.get(f"/api/v1/users/{owner.id}/avatar")
    assert response.status_code == 401
    assert _envelope(response)["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_conditional_get_never_bypasses_authentication(
    client: httpx.AsyncClient, db_session: AsyncSession, fake_storage: FakeObjectStorage
) -> None:
    # The 304 shortcut must sit BEHIND the actor resolution: an
    # anonymous request carrying a valid ETag is 401, not a free 304
    # (the reviewer's authorization-matrix point).
    owner = await _seed_student(db_session, "20250101", "头像测试一")
    assert (
        await _upload(client, _bearer(await _login(client, "20250101")), _PNG_1PX)
    ).status_code == 200
    reader_tokens = await _login(client, "20250101")
    warm = await client.get(
        f"/api/v1/users/{owner.id}/avatar", headers=_bearer(reader_tokens)
    )
    assert warm.status_code == 200
    etag = warm.headers["ETag"]

    anonymous = await client.get(
        f"/api/v1/users/{owner.id}/avatar",
        headers={"If-None-Match": etag},
    )
    assert anonymous.status_code == 401
    assert _envelope(anonymous)["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_teacher_reads_student_avatar(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_storage: FakeObjectStorage,
    api_clock: FrozenClock,
) -> None:
    # The visibility matrix (reviewer's point): avatars are public
    # presentational material for EVERY authenticated role, not a
    # student-only surface. The teacher's token is a real issued pair
    # (live session row + real codec) so the bearer resolution path is
    # the production one — only the TOTP HTTP dance is skipped.
    student = await _seed_student(db_session, "20250101", "头像测试一")
    assert (
        await _upload(client, _bearer(await _login(client, "20250101")), _PNG_1PX)
    ).status_code == 200

    teacher = User(
        username="teacher@pku.edu.cn",
        password_hash=hash_password(_PASSWORD),
        nickname="教师",
        email_normalized="teacher@pku.edu.cn",
        role=Role.TEACHER.value,
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(teacher)
    await db_session.flush()
    sessions = SessionService(
        clock=api_clock,
        access_codec=get_access_token_codec(),
        refresh_token_ttl_days=get_settings().refresh_token_ttl_days,
    )
    _, tokens = await sessions.issue_session(
        db_session, user=teacher, now=api_clock.now()
    )
    await db_session.commit()

    response = await client.get(
        f"/api/v1/users/{student.id}/avatar",
        headers=_bearer({"access_token": tokens.access_token}),
    )
    assert response.status_code == 200, response.text
    assert response.content == _PNG_1PX


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_avatar_unknown_user_is_404(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")
    response = await client.get(
        f"/api/v1/users/{uuid4()}/avatar", headers=_bearer(tokens)
    )
    assert response.status_code == 404
    assert _envelope(response)["code"] == "NOT_FOUND"


# --- upload guards -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_requires_authentication(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_student(db_session, "20250101", "头像测试一")
    response = await _upload(client, {}, _PNG_1PX)
    assert response.status_code == 401
    assert _envelope(response)["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_is_rate_limited_per_account(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    fake_limiter: FakeRateLimiter,
    fake_storage: FakeObjectStorage,
) -> None:
    await _seed_student(db_session, "20250101", "头像测试一")
    tokens = await _login(client, "20250101")
    fake_limiter.fail_on("me:avatar-upload")

    response = await _upload(client, _bearer(tokens), _PNG_1PX)
    assert response.status_code == 429
    assert _envelope(response)["code"] == "RATE_LIMITED"
    assert not fake_storage.objects
