# backend/tests/integration/tasks/test_platforms_api.py
"""The platform registry's API surface (V1.1 spec 2026-10-16 §3).

GET /platforms is the logged-in read-only dictionary (enabled only,
student UI consumes display_name); the admin CRUD carries
PLATFORM_CREATED/PLATFORM_UPDATED audit rows (flush-only) and the
TEACHER/STUDENT 403 matrix.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import get_business_clock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.providers import (
    get_email_sender,
    get_identity_redis,
    get_rate_limiter,
)
from app.modules.tasks.models import Platform
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

pytestmark = pytest.mark.integration

_API_REDIS_DB = 8
_T0 = datetime.now(UTC).replace(microsecond=0)
_ADMIN_EMAIL = "platform-admin@campus.example.edu"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_API_REDIS_DB}"))


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


async def _seed_admin(db: AsyncSession) -> User:
    admin = User(
        username=_ADMIN_EMAIL,
        password_hash=None,
        nickname="平台管理员",
        email_normalized=_ADMIN_EMAIL,
        email_verified_at=_T0,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(admin)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=admin.id,
            secret_encrypted=Fernet(
                get_settings().totp_encryption_key.encode()
            ).encrypt(b"SECRETPLACEHOLDER23456789"),
            confirmed_at=_T0,
        )
    )
    await db.flush()
    return admin


async def _mint_bearer(db: AsyncSession, user: User) -> str:
    from app.core.security import AccessTokenCodec
    from app.modules.identity.session_service import SessionService

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


async def _seed_role_user(db: AsyncSession, role: Role) -> User:
    if role is Role.ADMIN:
        return await _seed_admin(db)
    user = User(
        username=f"platform-{role.value}@campus.example.edu",
        password_hash=None,
        nickname=f"平台{role.value}",
        email_normalized=f"platform-{role.value}@campus.example.edu",
        email_verified_at=_T0,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def test_public_dictionary_lists_enabled_sorted(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student = User(
        username="20250014444",
        password_hash="x",
        nickname="平台字典",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    bearer = await _mint_bearer(db_session, student)

    response = await client.get(
        "/api/v1/platforms", headers={"Authorization": f"Bearer {bearer}"}
    )

    assert response.status_code == 200, response.text
    items = response.json()
    codes = [item["code"] for item in items]
    assert "xiaohongshu" in codes and "boss-zhipin" in codes
    # sort_order ascending, stable by code within an order value.
    orders = [item["sort_order"] for item in items]
    assert orders == sorted(orders)
    # The dictionary DTO carries no lifecycle/admin fields.
    for item in items:
        assert set(item) == {"code", "display_name", "sort_order"}


async def test_public_dictionary_requires_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/platforms")
    assert response.status_code == 401, response.text


async def test_admin_can_create_and_audit_row_lands(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)

    created = await client.post(
        "/api/v1/admin/platforms",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"code": "weibo", "display_name": "微博", "sort_order": 80},
    )

    assert created.status_code in (200, 201), created.text
    body = created.json()
    assert body["code"] == "weibo"
    assert body["enabled"] is True
    assert await db_session.get(Platform, "weibo") is not None
    actions = list(
        await db_session.scalars(
            select(AuditLog.action).where(
                AuditLog.actor_user_id == admin.id,
                AuditLog.target_type == "platform",
            )
        )
    )
    assert "PLATFORM_CREATED" in actions


async def test_admin_patch_updates_and_snapshots(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)

    patched = await client.patch(
        "/api/v1/admin/platforms/zhihu",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"display_name": "知乎新名", "sort_order": 35, "enabled": False},
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "知乎新名"
    assert patched.json()["enabled"] is False
    row = await db_session.get(Platform, "zhihu")
    assert row is not None and row.sort_order == 35

    audit = (
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "PLATFORM_UPDATED")
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).first()
    assert audit is not None
    assert audit.before_snapshot == {
        "display_name": "知乎",
        "sort_order": 30,
        "enabled": True,
    }
    assert audit.after_snapshot == {
        "display_name": "知乎新名",
        "sort_order": 35,
        "enabled": False,
    }


async def test_duplicate_code_conflicts(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)

    response = await client.post(
        "/api/v1/admin/platforms",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"code": "zhihu", "display_name": "知乎再来", "sort_order": 1},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_patch_unknown_code_404s(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)

    response = await client.patch(
        "/api/v1/admin/platforms/not-a-platform",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"display_name": "无此平台"},
    )

    assert response.status_code == 404, response.text


async def test_role_matrix_teacher_and_student_are_403(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    for role in (Role.TEACHER, Role.STUDENT):
        user = await _seed_role_user(db_session, role)
        bearer = await _mint_bearer(db_session, user)

        listed = await client.get(
            "/api/v1/admin/platforms",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        created = await client.post(
            "/api/v1/admin/platforms",
            headers={"Authorization": f"Bearer {bearer}"},
            json={"code": "nope", "display_name": "无权", "sort_order": 1},
        )
        assert listed.status_code == 403, (role, listed.text)
        assert created.status_code == 403, (role, created.text)


async def test_disabled_platform_leaves_the_public_dictionary(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)
    patched = await client.patch(
        "/api/v1/admin/platforms/lietou",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"enabled": False},
    )
    assert patched.status_code == 200, patched.text

    student = User(
        username="20250014445",
        password_hash="x",
        nickname="平台字典2",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db_session.add(student)
    await db_session.flush()
    student_bearer = await _mint_bearer(db_session, student)

    response = await client.get(
        "/api/v1/platforms",
        headers={"Authorization": f"Bearer {student_bearer}"},
    )
    codes = [item["code"] for item in response.json()]
    assert "lietou" not in codes
    assert "zhaopin" in codes


# --- import validation is registry-driven (spec §3) ---------------------------


async def _seed_import_task(db: AsyncSession, owner: User) -> str:
    from app.modules.tasks.enums import (
        DeadlineMode,
        TaskRarity,
        TaskStatus,
        TaskType,
    )
    from app.modules.tasks.models import Task

    task = Task(
        owner_teacher_id=owner.id,
        title="平台导入测试",
        description="registry import fixture",
        task_type=TaskType.DATA_CRAWL.value,
        rarity=TaskRarity.NORMAL.value,
        base_reward_points=10,
        status=TaskStatus.PUBLISHED.value,
        deadline_mode=DeadlineMode.RELATIVE.value,
        duration_minutes=60,
        allowed_file_types=["CSV"],
        max_file_size_bytes=1024 * 1024,
        notification_channels=["SMS"],
        submission_schema={"columns": ["title", "likes"]},
        submission_schema_version=1,
    )
    db.add(task)
    await db.flush()
    return str(task.id)


def _import_service(api_redis) -> object:
    from app.modules.tasks.importer import AssignmentImportService

    return AssignmentImportService(
        redis=api_redis,
        clock=FrozenClock(_T0),
        max_file_bytes=1024 * 1024,
        max_rows=100,
        keyword_max_length=200,
        preview_ttl_seconds=600,
    )


async def test_import_accepts_admin_created_platform(
    client: httpx.AsyncClient, db_session: AsyncSession, api_redis
) -> None:
    """The registry is OPEN: an admin-registered platform imports fine —
    zero-migration growth is the whole point."""
    from app.modules.identity.events import Actor

    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)
    created = await client.post(
        "/api/v1/admin/platforms",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"code": "weibo", "display_name": "微博", "sort_order": 80},
    )
    assert created.status_code == 201, created.text

    teacher = User(
        username="platform-import@campus.example.edu",
        password_hash=None,
        nickname="导入教师",
        email_normalized="platform-import@campus.example.edu",
        email_verified_at=_T0,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    task_id = await _seed_import_task(db_session, teacher)

    service = _import_service(api_redis)
    preview = await service.preview_assignments(
        db_session,
        Actor(user_id=teacher.id, role=Role.TEACHER),
        uuid.UUID(task_id),
        "platform,keyword\nweibo,热搜词\n".encode(),
    )
    assert preview.errors == ()
    assert len(preview.valid) == 1


async def test_import_rejects_disabled_and_unknown_platforms(
    client: httpx.AsyncClient, db_session: AsyncSession, api_redis
) -> None:
    """Disabled (enabled=false) bars NEW imports; unregistered stays the
    row-level UNSUPPORTED_PLATFORM preview error (spec §1/§3)."""
    from app.modules.identity.events import Actor

    admin = await _seed_admin(db_session)
    bearer = await _mint_bearer(db_session, admin)
    disabled = await client.patch(
        "/api/v1/admin/platforms/zhihu",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text

    teacher = User(
        username="platform-import2@campus.example.edu",
        password_hash=None,
        nickname="导入教师2",
        email_normalized="platform-import2@campus.example.edu",
        email_verified_at=_T0,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(teacher)
    await db_session.flush()
    task_id = await _seed_import_task(db_session, teacher)

    service = _import_service(api_redis)
    preview = await service.preview_assignments(
        db_session,
        Actor(user_id=teacher.id, role=Role.TEACHER),
        uuid.UUID(task_id),
        "platform,keyword\nzhihu,禁用词\nnot-a-platform,未知词\ndouyin,正常词\n".encode(),
    )
    codes = {(e.row_number, e.code) for e in preview.errors}
    assert (1, "UNSUPPORTED_PLATFORM") in codes
    assert (2, "UNSUPPORTED_PLATFORM") in codes
    assert len(preview.valid) == 1
