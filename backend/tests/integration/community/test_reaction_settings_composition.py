# backend/tests/integration/community/test_reaction_settings_composition.py
"""The emoji-whitelist composition test (PR #5 final review fix B): the
Admin setting is CONSUMED by the data plane, through the real app.

Drives ``create_app()`` — the community and system routers mounted under
/api/v1 with their REAL composition roots — over the rollback-harness
session. The provider dependencies under test
(``get_reaction_service``'s store-backed ``SystemEmojiWhitelistProvider``)
are NOT overridden (G18): only the harness allowances are (session,
FrozenClock anchored to the real now for JWT ``exp``, the flushed Redis
test database behind the REAL fixed-window limiter).

The owner's three scenarios (spec §22; G7 — the row is the fact, the
spec-eight default the seed):

- no EMOJI_WHITELIST row -> a default emoji (👍) reacts fine (the
  deployment seed path);
- Admin sets EMOJI_WHITELIST=["🎓"] via the audited settings API ->
  "🎓" reacts, the removed default 👍 is the typed VALIDATION_ERROR
  envelope and writes nothing;
- the row is deleted (direct database edit — settings have no delete
  API) -> the default set is back: 👍 toggles (a member again) and
  another seed emoji lands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.community.models import Comment, CommentReaction
from app.modules.community.router import get_community_redis
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.system.models import SystemSetting
from app.modules.system.service import EMOJI_WHITELIST
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task

pytestmark = pytest.mark.integration

# PyJWT validates `exp` at decode time against wall-clock time, so the
# frozen clock is anchored to the real now (the API-suite convention);
# it also pins the REAL limiter's fixed-window index.
_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_REACTION_PATH = "/api/v1/comments/{comment_id}/reactions"
_SETTING_PATH = "/api/v1/admin/settings/emoji-whitelist"

_COMMUNITY_TEST_REDIS_DB = 13


def _test_redis_url() -> str:
    base = get_settings().redis_url
    parts = urlsplit(base)
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"API integration tests refuse non-local Redis: {base!r}")
    return urlunsplit(parts._replace(path=f"/{_COMMUNITY_TEST_REDIS_DB}"))


# --- fixtures -----------------------------------------------------------------------


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
def api_app(
    db_session: AsyncSession, api_redis: aioredis.Redis, api_clock: FrozenClock
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    # The REAL RedisFixedWindowLimiter stays wired (only its Redis client
    # is pointed at the flushed test database) — the test_reaction_service
    # provider itself stays the production binding under test.
    app.dependency_overrides[get_community_redis] = lambda: api_redis
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


@pytest_asyncio.fixture(autouse=True)
async def no_emoji_whitelist_row(db_session: AsyncSession) -> None:
    """Start from the no-row state inside this test's rolled-back
    transaction: the DELETE rides the harness transaction, so a row some
    other suite durably committed is invisible here and restored at
    teardown (the swept_task_catalogue pattern)."""
    await db_session.execute(
        delete(SystemSetting).where(SystemSetting.key == EMOJI_WHITELIST)
    )


# --- seeding helpers ----------------------------------------------------------------


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


async def _tokens(db: AsyncSession, clock: FrozenClock, user: User) -> dict:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, pair = await sessions.issue_session(db, user=user, now=clock.now())
    return {"access_token": pair.access_token}


def _bearer(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _admin_headers(
    db: AsyncSession, clock: FrozenClock, *, username: str
) -> dict[str, str]:
    """An ADMIN account able to pass ``require_admin_actor`` (role +
    ACTIVE + a confirmed TOTP credential row — the stand-in secret is
    opaque to the guard)."""
    admin = _user(username=username, role=Role.ADMIN)
    db.add(admin)
    await db.flush()
    db.add(
        TotpCredential(
            user_id=admin.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return _bearer(await _tokens(db, clock, admin))


def _published_task(owner: User) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title="小红书考研经验帖数据采集",
        description="采集指定关键词下的笔记正文与互动数据。",
        task_type=TaskType.DATA_CRAWL.value,
        rarity=TaskRarity.NORMAL.value,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED.value,
        deadline_mode=DeadlineMode.RELATIVE.value,
        duration_minutes=4320,
        allowed_file_types=["CSV"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
        published_at=_T0,
        submission_schema={"columns": ["title", "likes"]},
        submission_schema_version=1,
    )


async def _seed(db: AsyncSession, *objects: Any) -> None:
    db.add_all(objects)
    await db.flush()


# --- the owner's three scenarios ----------------------------------------------------


@pytest.mark.integration
async def test_emoji_whitelist_row_tightens_reactions_and_seed_returns_without_it(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """No row -> the spec-eight seed admits 👍; the audited ["🎓"] row
    admits 🎓 and refuses the removed 👍 (typed 422, nothing written);
    deleting the row restores the seed (👍 toggles again, ❤️ lands)."""
    teacher = _user(username="reaction-comp-teacher", role=Role.TEACHER)
    student = _user(username="2025reaction01", role=Role.STUDENT)
    await _seed(db_session, teacher, student)
    task = _published_task(teacher)
    await _seed(db_session, task)
    comment = Comment(
        task_id=task.id,
        user_id=student.id,
        parent_id=None,
        content="这个任务的说明很清楚，做起来很顺利。",
        is_anonymous=False,
        created_at=_T0,
    )
    await _seed(db_session, comment)
    student_headers = _bearer(await _tokens(db_session, api_clock, student))
    admin_headers = await _admin_headers(
        db_session, api_clock, username="reaction-comp-admin"
    )

    # 1. No row: the deployment seed admits the default 👍.
    seeded = await client.post(
        _REACTION_PATH.format(comment_id=comment.id),
        json={"emoji": "👍"},
        headers=student_headers,
    )
    assert seeded.status_code == 200, seeded.text
    assert seeded.json() == {"emoji": "👍", "added": True, "counts": {"👍": 1}}

    # 2. The audited row narrows the whitelist to ["🎓"] (spec §22).
    narrowed = await client.put(
        _SETTING_PATH,
        json={"value": ["🎓"], "reason": "终审修复 B 组合验证"},
        headers=admin_headers,
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json() == {
        "key": EMOJI_WHITELIST,
        "value": '["🎓"]',
        "version": 1,
    }

    allowed = await client.post(
        _REACTION_PATH.format(comment_id=comment.id),
        json={"emoji": "🎓"},
        headers=student_headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {
        "emoji": "🎓",
        "added": True,
        "counts": {"👍": 1, "🎓": 1},
    }

    # 3. The removed default emoji is refused — the typed §29 envelope,
    #    and the refusal wrote nothing (the §22 only-members rule).
    refused = await client.post(
        _REACTION_PATH.format(comment_id=comment.id),
        json={"emoji": "👍"},
        headers=student_headers,
    )
    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["emoji"] == "👍"
    thumbs_up = await db_session.scalar(
        select(func.count())
        .select_from(CommentReaction)
        .where(
            CommentReaction.comment_id == comment.id,
            CommentReaction.user_id == student.id,
            CommentReaction.emoji == "👍",
        )
    )
    assert thumbs_up == 1  # exactly the seed-path row, no second insert

    # 4. Deleting the row (direct database edit — settings have no
    #    delete API) returns the whitelist to the deployment seed.
    await db_session.execute(
        delete(SystemSetting).where(SystemSetting.key == EMOJI_WHITELIST)
    )
    back_to_seed = await client.post(
        _REACTION_PATH.format(comment_id=comment.id),
        json={"emoji": "❤️"},
        headers=student_headers,
    )
    assert back_to_seed.status_code == 200, back_to_seed.text
    assert back_to_seed.json() == {
        "emoji": "❤️",
        "added": True,
        "counts": {"👍": 1, "🎓": 1, "❤️": 1},
    }
    toggled = await client.post(
        _REACTION_PATH.format(comment_id=comment.id),
        json={"emoji": "👍"},
        headers=student_headers,
    )
    assert toggled.status_code == 200, toggled.text
    assert toggled.json() == {
        "emoji": "👍",
        "added": False,  # a member again: the same click toggles off
        "counts": {"🎓": 1, "❤️": 1},
    }
