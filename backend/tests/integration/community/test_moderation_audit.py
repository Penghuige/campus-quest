# backend/tests/integration/community/test_moderation_audit.py
"""The durable audit rows of the two comment-moderation surfaces
(spec §30/§21.3/§21.4; PR #2 hardening pass 4a: G11/G12).

- ``COMMENT_MODERATE_DELETED`` on moderate_delete_comment: before/after
  carry the visibility-trio migration (ids, timestamps, reason codes —
  never content, never author identity), the mandatory reason rides the
  free-text column, the DomainEvent stream is unchanged, and the typed
  already-deleted replay writes NO second row;
- ``COMMENT_ADMIN_HARD_HIDDEN`` on admin_hard_hide_subtree: before/after
  carry the affected subtree size and the root state migration (§21.3
  AuditLog 在 hard hide 后存活 — rows survive, so does the trace);

the HTTP surface threads ``AuditContext`` (X-Request-ID + peer ip on
the row), and the PII negative assertion proves the seeded author's
nickname/phone appear in NO audit snapshot even though the comment
content itself contains them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.community.comment_service import (
    AUDIT_ACTION_COMMENT_ADMIN_HARD_HIDDEN,
    AUDIT_ACTION_COMMENT_MODERATE_DELETED,
    CommentDeletedError,
    CommentService,
)
from app.modules.community.models import Comment
from app.modules.community.router import get_event_publisher
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor, InMemoryEventCollector
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task

pytestmark = pytest.mark.integration

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)
# Anchored to the REAL now: the access-token guard validates exp
# against the wall clock (the existing API-test convention).
_T0 = datetime.now(UTC).replace(microsecond=0)

_AUTHOR_NICKNAME = "夜航评论员西弗"
_AUTHOR_PHONE = "+8613900000002"


def _user(username: str, role: Role, *, nickname: str, phone: str | None) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=phone,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _comment(task: Task, user: User, *, parent_id: Any = None) -> Comment:
    return Comment(
        task_id=task.id,
        user_id=user.id,
        parent_id=parent_id,
        content=f"{_AUTHOR_NICKNAME} 之前整理过 {_AUTHOR_PHONE} 可以联系。",
        is_anonymous=False,
        created_at=_T0,
    )


async def _world(db: AsyncSession) -> dict[str, Any]:
    """Owner teacher + author student + a published task + a two-comment
    thread (root and child) the moderation surfaces act on."""
    suffix = uuid4().hex[:8]
    owner = _user(
        f"modaud-t{suffix}", Role.TEACHER, nickname="治理教师贝亚", phone=None
    )
    admin = _user(
        f"modaud-a{suffix}", Role.ADMIN, nickname="治理管理员卡洛", phone=None
    )
    author = _user(
        f"modaud-s{suffix}",
        Role.STUDENT,
        nickname=_AUTHOR_NICKNAME,
        phone=_AUTHOR_PHONE,
    )
    db.add_all((owner, admin, author))
    await db.flush()
    task = Task(
        owner_teacher_id=owner.id,
        title="小红书考研经验帖数据采集",
        description="采集指定关键词下的笔记正文与互动数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"columns": ["url", "title"]},
        submission_schema_version=1,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
        published_at=_T0,
    )
    db.add(task)
    await db.flush()
    root = _comment(task, author)
    db.add(root)
    await db.flush()
    child = _comment(task, author, parent_id=root.id)
    db.add(child)
    await db.commit()  # close the seeding savepoint segment
    return {
        "owner": owner,
        "admin": admin,
        "author": author,
        "task": task,
        "root": root,
        "child": child,
    }


def _service() -> tuple[CommentService, InMemoryEventCollector]:
    collector = InMemoryEventCollector()
    return CommentService(clock=FrozenClock(_T0), events=collector), collector


async def _audit_rows(db: AsyncSession, comment_id: Any) -> list[AuditLog]:
    return list(
        (
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.target_id == str(comment_id))
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).all()
    )


def _assert_no_pii(row: AuditLog) -> None:
    """G11 negative assertion: the comment content quotes the author's
    nickname and phone, but no audit JSON column may."""
    for column in ("before_snapshot", "after_snapshot", "details"):
        blob = json.dumps(getattr(row, column) or {}, ensure_ascii=False)
        assert _AUTHOR_NICKNAME not in blob, f"{column} leaked nickname"
        assert _AUTHOR_PHONE not in blob, f"{column} leaked phone"


# --- moderate_delete_comment (§30 comment-management deletion) ------------------------


async def test_moderate_delete_writes_one_audit_row_with_the_visibility_trio(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    root = world["root"]
    owner = world["owner"]
    service, events = _service()

    await service.moderate_delete_comment(
        db_session, Actor(user_id=owner.id, role=Role.TEACHER), root.id, "灌水刷屏"
    )

    rows = await _audit_rows(db_session, root.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_COMMENT_MODERATE_DELETED
    assert row.target_type == "comment"
    assert row.target_id == str(root.id)
    assert row.actor_user_id == owner.id
    assert row.actor_role == Role.TEACHER.value
    assert row.reason == "灌水刷屏"
    assert row.before_snapshot == {
        "deleted_at": None,
        "delete_reason": None,  # a moderation target is never pre-deleted
        "is_hard_hidden": False,
    }
    assert row.after_snapshot == {
        "deleted_at": _T0.isoformat(),
        "deleted_by": str(owner.id),
        "delete_reason": "灌水刷屏",
        "is_hard_hidden": False,
    }
    assert row.details == {"task_id": str(world["task"].id)}
    assert row.ip_address is None
    assert row.request_id is None
    assert row.created_at is not None
    _assert_no_pii(row)
    # The notification/audit event stream is untouched beside the row.
    assert [event.event_type for event in events.events] == [
        "COMMENT_MODERATION_DELETED"
    ]


async def test_moderate_delete_replay_writes_no_second_row(
    db_session: AsyncSession,
) -> None:
    """The already-deleted replay is the typed 409 (CommentDeletedError)
    and writes nothing — the decision stands once."""
    world = await _world(db_session)
    root = world["root"]
    owner = world["owner"]
    service, _events = _service()
    actor = Actor(user_id=owner.id, role=Role.TEACHER)

    await service.moderate_delete_comment(db_session, actor, root.id, "灌水刷屏")
    with pytest.raises(CommentDeletedError):
        await service.moderate_delete_comment(db_session, actor, root.id, "再次尝试")

    rows = await _audit_rows(db_session, root.id)
    assert len(rows) == 1


# --- admin_hard_hide_subtree (§21.3 AuditLog 在 hard hide 后存活) --------------------


async def test_hard_hide_audits_subtree_scale_and_root_migration(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session)
    root = world["root"]
    admin = world["admin"]
    service, events = _service()

    await service.admin_hard_hide_subtree(
        db_session,
        Actor(user_id=admin.id, role=Role.ADMIN),
        root.id,
        "涉个人信息，依法移除",
    )

    rows = await _audit_rows(db_session, root.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_COMMENT_ADMIN_HARD_HIDDEN
    assert row.actor_user_id == admin.id
    assert row.actor_role == Role.ADMIN.value
    assert row.reason == "涉个人信息，依法移除"  # verbatim, no code prefix
    assert row.before_snapshot == {
        "subtree_size": 2,  # root + one child
        "root_is_hard_hidden": False,
        "root_deleted_at": None,
    }
    assert row.after_snapshot == {
        "subtree_size": 2,
        "root_is_hard_hidden": True,
        "root_deleted_at": _T0.isoformat(),
        "root_delete_reason_code": "ADMIN_HARD_HIDE",
    }
    assert row.details == {"task_id": str(world["task"].id)}
    assert row.ip_address is None
    assert row.request_id is None
    _assert_no_pii(row)
    assert [event.event_type for event in events.events] == ["COMMENT_HARD_HIDDEN"]


# --- the HTTP seam: AuditContext lands on the row -------------------------------------


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def event_collector() -> InMemoryEventCollector:
    return InMemoryEventCollector()


@pytest_asyncio.fixture
async def api_app(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    event_collector: InMemoryEventCollector,
) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    app.dependency_overrides[get_event_publisher] = lambda: event_collector
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _staff_headers(
    db: AsyncSession, clock: FrozenClock, user: User
) -> dict[str, str]:
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def test_http_moderate_delete_carries_request_id_and_ip(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The router constructs AuditContext.from_request for the
    moderation endpoints: the propagated id and peer ip land on the
    durable row (spec §30)."""
    world = await _world(db_session)
    root = world["root"]
    headers = await _staff_headers(db_session, api_clock, world["owner"])
    headers["X-Request-ID"] = "modaud-delete-0001"

    response = await client.request(
        "DELETE",
        f"/api/v1/teacher/comments/{root.id}",
        json={"reason": "灌水刷屏"},
        headers=headers,
    )
    assert response.status_code == 204, response.text

    rows = await _audit_rows(db_session, root.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_COMMENT_MODERATE_DELETED
    assert row.request_id == "modaud-delete-0001"
    # httpx ASGITransport reports the local peer 127.0.0.1.
    assert row.ip_address == "127.0.0.1"
    _assert_no_pii(row)


async def test_http_hard_hide_carries_request_id_and_ip(
    db_session: AsyncSession,
    api_clock: FrozenClock,
    client: httpx.AsyncClient,
) -> None:
    """The hard-hide endpoint threads the same AuditContext seam: the
    Admin decision's durable row names where the request came from."""
    world = await _world(db_session)
    root = world["root"]
    headers = await _staff_headers(db_session, api_clock, world["admin"])
    headers["X-Request-ID"] = "modaud-hide-0002"

    response = await client.post(
        f"/api/v1/teacher/comments/{root.id}/hard-hide",
        json={"reason": "涉个人信息，依法移除"},
        headers=headers,
    )
    assert response.status_code == 204, response.text

    rows = await _audit_rows(db_session, root.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_COMMENT_ADMIN_HARD_HIDDEN
    assert row.request_id == "modaud-hide-0002"
    assert row.ip_address == "127.0.0.1"
    _assert_no_pii(row)
