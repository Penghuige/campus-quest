# backend/tests/integration/tasks/test_submission_template_api.py
"""GET /tasks/{id}/submission-template (V1.1 spec §5 CSV template endpoint).

A header-only CSV with BOM — the exact column order the task's schema
declares (required first, then optional) — so a student's filled-in file
passes the validator's header check on the first try. CSV tasks only;
other families answer the typed 404.
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
)
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task
from tests.fakes.integrations import FakeEmailSender, FakeRateLimiter

pytestmark = pytest.mark.integration

_TPL_REDIS_DB = 5
_T0 = datetime.now(UTC).replace(microsecond=0)
_STUDENT = "20250013456"
_PASSWORD = "correct-horse-battery"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in _LOCAL_HOSTS:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_TPL_REDIS_DB}"))


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


async def _seed_world(
    db: AsyncSession, *, schema: dict | None, file_types: list[str]
) -> tuple[User, Task]:
    teacher = User(
        username="template-teacher@campus.example.edu",
        password_hash=None,
        nickname="模板教师",
        email_normalized="template-teacher@campus.example.edu",
        email_verified_at=_T0,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    student = User(
        username=_STUDENT,
        password_hash=hash_password(_PASSWORD),
        nickname="模板同学",
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )
    db.add_all([teacher, student])
    await db.flush()
    task = Task(
        owner_teacher_id=teacher.id,
        title="模板下载任务",
        description="template fixture",
        task_type=TaskType.DATA_CRAWL.value,
        rarity=TaskRarity.NORMAL.value,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED.value,
        deadline_mode=DeadlineMode.RELATIVE.value,
        duration_minutes=4320,
        allowed_file_types=file_types,
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
        submission_schema=schema,
        submission_schema_version=1 if schema else None,
    )
    db.add(task)
    await db.flush()
    return student, task


def _login_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mint(db: AsyncSession, user: User) -> str:
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


async def test_template_streams_bom_csv_with_schema_columns(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student, task = await _seed_world(
        db_session,
        schema={
            "required_columns": [
                {"name": "标题", "type": "string"},
                {"name": "链接", "type": "string"},
            ],
            "optional_columns": [{"name": "点赞数", "type": "integer"}],
        },
        file_types=["CSV"],
    )
    headers = _login_headers(await _mint(db_session, student))

    response = await client.get(
        f"/api/v1/tasks/{task.id}/submission-template", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    # BOM first: Excel opens the file with correct UTF-8 decoding.
    assert response.content.startswith(b"\xef\xbb\xbf")
    body = response.content.decode("utf-8-sig")
    assert body == "标题,链接,点赞数\r\n"
    # The disposition: plain quoted filename (the contract the frontend
    # pins its parser on).
    assert (
        response.headers.get("content-disposition")
        == 'attachment; filename="template.csv"'
    )


async def test_template_requires_no_optional_columns_when_absent(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student, task = await _seed_world(
        db_session,
        schema={"required_columns": [{"name": "内容", "type": "string"}]},
        file_types=["CSV"],
    )
    headers = _login_headers(await _mint(db_session, student))

    response = await client.get(
        f"/api/v1/tasks/{task.id}/submission-template", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.content.decode("utf-8-sig") == "内容\r\n"


async def test_non_csv_task_answers_typed_404(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student, task = await _seed_world(
        db_session,
        schema=None,
        file_types=["DOCX", "PDF"],
    )
    headers = _login_headers(await _mint(db_session, student))

    response = await client.get(
        f"/api/v1/tasks/{task.id}/submission-template", headers=headers
    )

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"


async def test_unknown_task_404_and_login_required(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    student, _ = await _seed_world(
        db_session, schema={"required_columns": []}, file_types=["CSV"]
    )
    headers = _login_headers(await _mint(db_session, student))

    unknown = await client.get(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000001/submission-template",
        headers=headers,
    )
    assert unknown.status_code == 404, unknown.text

    anonymous = await client.get(
        "/api/v1/tasks/00000000-0000-0000-0000-000000000001/submission-template"
    )
    assert anonymous.status_code == 401, anonymous.text
