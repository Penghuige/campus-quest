# backend/tests/integration/community/test_comment_edit_delete.py
"""Comment edit history, soft delete, tombstones, and moderation
boundaries over real PostgreSQL (spec §21.2-§21.3; plan 06 task 3).

Drives ``CommentService.edit_comment`` / ``delete_own_comment`` /
``moderate_delete_comment`` / ``admin_hard_hide_subtree`` and the
tombstone-aware public list inside the rollback harness:

- edit (spec §21.3): owner-only (越权修改别人评论 is the §21.2 MUST), the
  same content normalizer/cap as creation, one append-only
  CommentRevision row holding the PREVIOUS version, an observable
  ``updated_at`` bump, and an immutable ``parent_id`` (there is no edit
  path that can change it);
- delete (spec §21.3): default SOFT delete — the trio
  deleted_at/deleted_by/delete_reason lands on the row and the content
  stays in storage; children of a deleted parent SURVIVE;
- tombstone rendering: a deleted PARENT with surviving (rendered)
  children stays in the public list as ``该评论已删除`` with null content;
  deleted leaves vanish; a deleted comment whose children all vanished
  vanishes too;
- Teacher moderation boundary: only the task owner or a collaborator
  holding MODERATE_COMMUNITY may moderate-delete, ALWAYS with a reason,
  and the action emits an audit-grade DomainEvent;
- Admin hard hide (spec §21.3 隐私/违法): Admin-only, mandatory reason,
  hides the WHOLE subtree's visible content (no tombstone) while rows,
  content, ids, and relations survive untouched for AuditLog-pending
  review, with an audit DomainEvent.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, FrozenClock, SystemClock
from app.core.error_codes import ErrorCode
from app.modules.community.comment_service import (
    COMMENT_HARD_HIDDEN,
    COMMENT_MODERATION_DELETED,
    CommentAdminRequiredError,
    CommentDeletedError,
    CommentModerationDeniedError,
    CommentNotFoundError,
    CommentOwnerRequiredError,
    CommentService,
    ModerationReasonRequiredError,
    ParentCommentDeletedError,
)
from app.modules.community.models import Comment, CommentRevision
from app.modules.community.schemas import CreateComment
from app.modules.community.serializers import serialize_moderation_comment
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import (
    Actor,
    DomainEventPublisher,
    InMemoryEventCollector,
)
from app.modules.identity.models import User
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task, TaskCollaborator

_T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_STUDENT_NICKNAME = "小北"
_OTHER_NICKNAME = "同学乙"

_DELETED_DISPLAY = "该评论已删除"


# --- seeding helpers ----------------------------------------------------------------


def _user(
    *,
    username: str,
    role: Role,
    nickname: str = "测试同学",
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User, **overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "owner_teacher_id": owner.id,
        "title": "小红书考研经验帖数据采集",
        "description": "采集指定关键词下的笔记正文与互动数据。",
        "task_type": TaskType.DATA_CRAWL,
        "rarity": TaskRarity.NORMAL,
        "base_reward_points": 100,
        "status": TaskStatus.PUBLISHED,
        "deadline_mode": DeadlineMode.RELATIVE,
        "duration_minutes": 4320,
        "allowed_file_types": ["CSV"],
        "max_file_size_bytes": 200 * 1024 * 1024,
        "notification_channels": ["SMS"],
    }
    fields.update(overrides)
    return Task(**fields)


def _comment(task: Task, user: User, **overrides: Any) -> Comment:
    fields: dict[str, Any] = {
        "task_id": task.id,
        "user_id": user.id,
        "content": "这个任务的说明很清楚，做起来很顺利。",
        "is_anonymous": False,
        "created_at": _T0,
    }
    fields.update(overrides)
    return Comment(**fields)


async def _seed(db: AsyncSession, *objects: Any) -> None:
    db.add_all(objects)
    await db.flush()


async def _thread_fixture(db: AsyncSession) -> tuple[Task, User, User, User]:
    """A teacher-owned PUBLISHED task plus two ACTIVE student commenters."""
    teacher = _user(username="teacher0001@pku.edu.cn", role=Role.TEACHER)
    student = _user(
        username="20250010001", role=Role.STUDENT, nickname=_STUDENT_NICKNAME
    )
    other = _user(username="20250010002", role=Role.STUDENT, nickname=_OTHER_NICKNAME)
    await _seed(db, teacher, student, other)
    task = _task(teacher)
    await _seed(db, task)
    return task, teacher, student, other


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


def _service(
    *,
    clock: Clock | None = None,
    events: DomainEventPublisher | None = None,
    comment_max_length: int = 2000,
) -> CommentService:
    return CommentService(
        comment_max_length=comment_max_length,
        clock=clock if clock is not None else SystemClock(),
        events=events if events is not None else InMemoryEventCollector(),
    )


async def _root_comment(
    db: AsyncSession, task: Task, user: User, **overrides: Any
) -> Comment:
    comment = _comment(task, user, **overrides)
    await _seed(db, comment)
    return comment


async def _reply(
    db: AsyncSession,
    task: Task,
    user: User,
    parent: Comment,
    **overrides: Any,
) -> Comment:
    reply = _comment(task, user, parent_id=parent.id, **overrides)
    await _seed(db, reply)
    return reply


async def _moderator_fixture(
    db: AsyncSession, task: Task, permissions: list[str], username: str
) -> User:
    collaborator = _user(username=username, role=Role.TEACHER)
    await _seed(db, collaborator)
    await _seed(
        db,
        TaskCollaborator(
            task_id=task.id, teacher_id=collaborator.id, permissions=permissions
        ),
    )
    return collaborator


async def _revisions(db: AsyncSession, comment_id: UUID) -> list[CommentRevision]:
    return list(
        await db.scalars(
            select(CommentRevision)
            .where(CommentRevision.comment_id == comment_id)
            .order_by(CommentRevision.edited_at)
        )
    )


async def _task_rows(db: AsyncSession, task: Task) -> dict[Any, Comment]:
    return {
        row.id: row
        for row in await db.scalars(select(Comment).where(Comment.task_id == task.id))
    }


# --- edit history (spec §21.3 编辑) --------------------------------------------------


@pytest.mark.integration
async def test_owner_edit_replaces_content_and_snapshots_previous_version(
    db_session: AsyncSession,
) -> None:
    """One edit: the row holds the new version, one append-only revision
    holds the PREVIOUS version plus edited_at, the DTO says 已编辑, and
    updated_at bumps to the edit instant."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(
        db_session, task, student, content="第一版", created_at=_T0
    )
    service = _service(clock=FrozenClock(_T1))

    public = await service.edit_comment(
        db_session, _actor(student), comment.id, "  第二版内容  "
    )

    assert public.content == "第二版内容"  # same trim/normalize rule as create
    assert public.edited is True
    assert public.deleted is False
    assert public.author_display == _STUDENT_NICKNAME

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.content == "第二版内容"
    assert row.parent_id is None  # parent_id immutable
    assert row.created_at == _T0
    assert row.updated_at == _T1  # observable bump (clock-owned)

    revisions = await _revisions(db_session, comment.id)
    assert len(revisions) == 1
    assert revisions[0].content == "第一版"  # the PREVIOUS version
    assert revisions[0].edited_at == _T1


@pytest.mark.integration
async def test_second_edit_appends_second_revision_of_previous_version(
    db_session: AsyncSession,
) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(
        db_session, task, student, content="版本零", created_at=_T0
    )

    await _service(clock=FrozenClock(_T1)).edit_comment(
        db_session, _actor(student), comment.id, "版本一"
    )
    await _service(clock=FrozenClock(_T2)).edit_comment(
        db_session, _actor(student), comment.id, "版本二"
    )

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.content == "版本二"  # 普通用户只看到最新版
    revisions = await _revisions(db_session, comment.id)
    assert [(r.content, r.edited_at) for r in revisions] == [
        ("版本零", _T1),
        ("版本一", _T2),
    ]


@pytest.mark.integration
async def test_edit_is_owner_only(db_session: AsyncSession) -> None:
    """Spec §21.2 MUST 越权修改别人评论: another student's edit is a typed
    PERMISSION_DENIED and leaves both the row and the history untouched."""
    task, _, student, other = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="原文")
    service = _service()

    with pytest.raises(CommentOwnerRequiredError) as raised:
        await service.edit_comment(db_session, _actor(other), comment.id, "越权改写")
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.content == "原文"
    assert await _revisions(db_session, comment.id) == []


@pytest.mark.integration
async def test_edit_rejects_missing_comment(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    with pytest.raises(CommentNotFoundError) as raised:
        await _service().edit_comment(db_session, _actor(student), uuid4(), "改空气")
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404


@pytest.mark.integration
async def test_edit_rejects_deleted_comment(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="将删除")
    await _service(clock=FrozenClock(_T1)).delete_own_comment(
        db_session, _actor(student), comment.id
    )

    with pytest.raises(CommentDeletedError) as raised:
        await _service().edit_comment(
            db_session, _actor(student), comment.id, "编辑墓碑"
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _revisions(db_session, comment.id) == []


@pytest.mark.integration
async def test_edit_revalidates_content_with_creation_rules(
    db_session: AsyncSession,
) -> None:
    """The edit path reuses the §21.1 normalizer and the same configurable
    cap; a rejected edit leaves no revision and no content change."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="原文")
    service = _service()

    with pytest.raises(Exception) as whitespace:
        await service.edit_comment(db_session, _actor(student), comment.id, " \n\t ")
    assert getattr(whitespace.value, "code", None) == ErrorCode.VALIDATION_ERROR

    capped = _service(comment_max_length=5)
    with pytest.raises(Exception) as over:
        await capped.edit_comment(
            db_session, _actor(student), comment.id, "超过五字上限"
        )
    assert getattr(over.value, "code", None) == ErrorCode.VALIDATION_ERROR
    assert over.value.details == {"max_length": 5}

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.content == "原文"
    assert await _revisions(db_session, comment.id) == []


@pytest.mark.integration
async def test_edit_never_changes_parent_id(db_session: AsyncSession) -> None:
    """``edit_comment`` takes no parent argument at all (task 3 contract):
    a reply stays anchored to its root through an edit."""
    task, _, student, _ = await _thread_fixture(db_session)
    root = await _root_comment(db_session, task, student, content="根评论")
    reply = await _reply(db_session, task, student, root, content="原回复")

    await _service().edit_comment(db_session, _actor(student), reply.id, "新回复")

    row = await db_session.scalar(select(Comment).where(Comment.id == reply.id))
    assert row is not None
    assert row.parent_id == root.id


# --- owner soft delete + tombstones (spec §21.3 删除) --------------------------------


@pytest.mark.integration
async def test_owner_soft_delete_sets_trio_and_row_survives(
    db_session: AsyncSession,
) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(
        db_session, task, student, content="待删除", created_at=_T0
    )
    await _service(clock=FrozenClock(_T1)).delete_own_comment(
        db_session, _actor(student), comment.id
    )

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None  # soft: the row — and the content — survive
    assert row.content == "待删除"
    assert row.deleted_at == _T1
    assert row.deleted_by == student.id
    assert row.delete_reason == "owner"  # the sentinel code when no reason given
    assert row.is_hard_hidden is False


@pytest.mark.integration
async def test_owner_soft_delete_records_explicit_reason(
    db_session: AsyncSession,
) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="写错了")
    service = _service(clock=FrozenClock(_T1))

    await service.delete_own_comment(
        db_session, _actor(student), comment.id, reason="发错了地方"
    )
    explicit = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert explicit is not None
    assert explicit.delete_reason == "发错了地方"

    blank_target = await _root_comment(db_session, task, student, content="空白理由")
    # A blank reason is treated as absent: the sentinel lands instead.
    await service.delete_own_comment(
        db_session, _actor(student), blank_target.id, reason="   "
    )
    blank = await db_session.scalar(
        select(Comment).where(Comment.id == blank_target.id)
    )
    assert blank is not None
    assert blank.delete_reason == "owner"


@pytest.mark.integration
async def test_delete_own_is_owner_only(db_session: AsyncSession) -> None:
    task, _, student, other = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="别人的目标")
    service = _service()

    with pytest.raises(CommentOwnerRequiredError) as raised:
        await service.delete_own_comment(db_session, _actor(other), comment.id)
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_at is None

    with pytest.raises(CommentNotFoundError):
        await service.delete_own_comment(db_session, _actor(student), uuid4())


@pytest.mark.integration
async def test_delete_own_rejects_already_deleted(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="一次就够")
    service = _service(clock=FrozenClock(_T1))
    await service.delete_own_comment(db_session, _actor(student), comment.id)

    with pytest.raises(CommentDeletedError):
        await service.delete_own_comment(db_session, _actor(student), comment.id)


@pytest.mark.integration
async def test_deleted_parent_with_surviving_child_renders_tombstone(
    db_session: AsyncSession,
) -> None:
    """Spec §21.3: 父评论删除后子评论保留, 前台显示“该评论已删除”. The
    tombstone keeps the thread slot (and the child's anchor) with null
    content and a uniform display — including for an anonymous author,
    whose anonymity marker is subsumed by the tombstone."""
    task, _, student, other = await _thread_fixture(db_session)
    parent = await _root_comment(
        db_session,
        task,
        student,
        content="被删除的父评论",
        is_anonymous=True,
        created_at=_T0,
    )
    child = await _reply(
        db_session,
        task,
        other,
        parent,
        content="子评论仍然可见",
        created_at=_T0 + timedelta(minutes=1),
    )
    await _service(clock=FrozenClock(_T1)).delete_own_comment(
        db_session, _actor(student), parent.id
    )

    items, total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert total == 2
    by_id = {item.id: item for item in items}

    tombstone = by_id[parent.id]
    assert tombstone.deleted is True
    assert tombstone.content is None
    assert tombstone.author_display == _DELETED_DISPLAY
    assert tombstone.parent_id is None

    surviving = by_id[child.id]
    assert surviving.deleted is False
    assert surviving.content == "子评论仍然可见"
    assert surviving.parent_id == parent.id
    assert surviving.author_display == _OTHER_NICKNAME

    wire = json.dumps(
        [dataclasses.asdict(item) for item in items], default=str, ensure_ascii=False
    )
    assert "被删除的父评论" not in wire  # the content is gone from the surface


@pytest.mark.integration
async def test_deleted_leaf_comment_vanishes_entirely(
    db_session: AsyncSession,
) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    leaf = await _root_comment(db_session, task, student, content="无儿女的叶子")
    live = await _root_comment(db_session, task, student, content="存活评论")
    await _service().delete_own_comment(db_session, _actor(student), leaf.id)

    items, total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert total == 1
    assert [item.id for item in items] == [live.id]
    assert items[0].deleted is False


@pytest.mark.integration
async def test_deleted_parent_with_only_vanished_children_vanishes_too(
    db_session: AsyncSession,
) -> None:
    """A tombstone exists to anchor surviving replies: when every child of
    a deleted parent is itself a vanished deleted leaf, the parent leaves
    the list as well — nothing renderable remains under it."""
    task, _, student, other = await _thread_fixture(db_session)
    parent = await _root_comment(db_session, task, student, content="父")
    child = await _reply(db_session, task, other, parent, content="子")
    bystander = await _root_comment(db_session, task, student, content="旁观者")
    service = _service()
    await service.delete_own_comment(db_session, _actor(other), child.id)
    await service.delete_own_comment(db_session, _actor(student), parent.id)

    items, total = await service.list_comments(db_session, task.id, limit=20, offset=0)
    assert total == 1
    assert [item.id for item in items] == [bystander.id]


# --- Teacher moderation boundary (spec §21.4 治理, plan step 3) ----------------------


@pytest.mark.integration
async def test_task_owner_teacher_moderates_with_reason_and_audit_event(
    db_session: AsyncSession,
) -> None:
    task, teacher, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="待治理")
    events = InMemoryEventCollector()
    service = _service(clock=FrozenClock(_T1), events=events)

    await service.moderate_delete_comment(
        db_session, _actor(teacher), comment.id, "包含不实信息"
    )

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_at == _T1
    assert row.deleted_by == teacher.id
    assert row.delete_reason == "包含不实信息"
    assert row.is_hard_hidden is False

    audits = events.of_type(COMMENT_MODERATION_DELETED)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.aggregate_type == "Comment"
    assert audit.aggregate_id == comment.id
    assert audit.occurred_at == _T1
    assert audit.payload["reason"] == "包含不实信息"
    assert audit.payload["deleted_by"] == str(teacher.id)
    assert audit.payload["task_id"] == str(task.id)
    assert audit.payload["comment_id"] == str(comment.id)
    assert len(events.events) == 1  # nothing else leaked onto the stream


@pytest.mark.integration
async def test_collaborator_with_moderate_community_may_moderate(
    db_session: AsyncSession,
) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="协作者可治理")
    collaborator = await _moderator_fixture(
        db_session, task, ["MODERATE_COMMUNITY"], "teacher0002@pku.edu.cn"
    )

    await _service().moderate_delete_comment(
        db_session, _actor(collaborator), comment.id, "垃圾内容"
    )

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_by == collaborator.id
    assert row.delete_reason == "垃圾内容"


@pytest.mark.integration
async def test_collaborator_without_moderate_community_is_denied(
    db_session: AsyncSession,
) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="越权目标")
    viewer = await _moderator_fixture(
        db_session, task, ["VIEW_TASK"], "teacher0003@pku.edu.cn"
    )
    events = InMemoryEventCollector()
    service = _service(events=events)

    with pytest.raises(CommentModerationDeniedError) as raised:
        await service.moderate_delete_comment(
            db_session, _actor(viewer), comment.id, "理由充分但无权限"
        )
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_at is None
    assert events.events == []


@pytest.mark.integration
async def test_unrelated_teacher_is_denied(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="无关任务")
    stranger = _user(username="teacher0004@pku.edu.cn", role=Role.TEACHER)
    await _seed(db_session, stranger)
    events = InMemoryEventCollector()

    with pytest.raises(CommentModerationDeniedError):
        await _service(events=events).moderate_delete_comment(
            db_session, _actor(stranger), comment.id, "跨任务治理"
        )

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_at is None
    assert events.events == []


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.STUDENT, Role.ADMIN])
async def test_moderation_delete_is_a_teacher_surface(
    db_session: AsyncSession, role: Role
) -> None:
    """Students moderate nothing (not even their own comment through this
    path); Admin's community-removal tool is the audited hard hide, not
    the teacher moderation delete."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="角色边界")
    if role is Role.STUDENT:
        actor_user: User = student  # the author themself still cannot "moderate"
    else:
        actor_user = _user(username="boundary-admin", role=role)
        await _seed(db_session, actor_user)

    with pytest.raises(CommentModerationDeniedError):
        await _service().moderate_delete_comment(
            db_session, _actor(actor_user), comment.id, "角色不符"
        )

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_at is None


@pytest.mark.integration
@pytest.mark.parametrize("reason", ["", "   "])
async def test_moderation_delete_requires_a_reason(
    db_session: AsyncSession, reason: str
) -> None:
    task, teacher, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="必须给理由")
    events = InMemoryEventCollector()
    service = _service(events=events)

    with pytest.raises(ModerationReasonRequiredError) as raised:
        await service.moderate_delete_comment(
            db_session, _actor(teacher), comment.id, reason
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400

    row = await db_session.scalar(select(Comment).where(Comment.id == comment.id))
    assert row is not None
    assert row.deleted_at is None
    assert events.events == []


@pytest.mark.integration
async def test_moderating_deleted_comment_rejected(db_session: AsyncSession) -> None:
    task, teacher, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student, content="一次就够")
    service = _service()
    await service.delete_own_comment(db_session, _actor(student), comment.id)

    with pytest.raises(CommentDeletedError):
        await service.moderate_delete_comment(
            db_session, _actor(teacher), comment.id, "重复治理"
        )
    with pytest.raises(CommentNotFoundError):
        await service.moderate_delete_comment(
            db_session, _actor(teacher), uuid4(), "治理空气"
        )


# --- Admin hard hide (spec §21.3 隐私/违法场景, plan step 4) -------------------------


async def _three_level_thread(
    db_session: AsyncSession,
) -> tuple[Task, User, Comment, Comment, Comment, Comment]:
    """root -> child -> grandchild by two students, plus an unrelated live
    root comment on the same task."""
    task, _, student, other = await _thread_fixture(db_session)
    root = await _root_comment(
        db_session, task, student, content="根：涉及隐私", created_at=_T0
    )
    child = await _reply(
        db_session,
        task,
        other,
        root,
        content="子：转发了敏感内容",
        created_at=_T0 + timedelta(minutes=1),
    )
    grandchild = await _reply(
        db_session,
        task,
        student,
        child,
        content="孙：又引用了一次",
        created_at=_T0 + timedelta(minutes=2),
    )
    bystander = await _root_comment(
        db_session,
        task,
        other,
        content="无关评论",
        created_at=_T0 + timedelta(minutes=3),
    )
    return task, student, root, child, grandchild, bystander


def _admin(username: str) -> User:
    return _user(username=username, role=Role.ADMIN)


@pytest.mark.integration
async def test_admin_hard_hide_removes_subtree_from_public_view_and_audits(
    db_session: AsyncSession,
) -> None:
    task, _, root, child, grandchild, bystander = await _three_level_thread(db_session)
    admin = _admin("admin-0001@pku.edu.cn")
    await _seed(db_session, admin)
    events = InMemoryEventCollector()
    service = _service(clock=FrozenClock(_T1), events=events)

    await service.admin_hard_hide_subtree(
        db_session, _actor(admin), root.id, "涉及个人隐私"
    )

    # Rows, content, ids, and relations all SURVIVE (no cascade delete):
    # this is a visibility removal, not a data removal.
    rows = await _task_rows(db_session, task)
    for comment in (root, child, grandchild):
        row = rows[comment.id]
        assert row.is_hard_hidden is True
        assert row.deleted_at == _T1
        assert row.deleted_by == admin.id
        assert row.delete_reason == "ADMIN_HARD_HIDE: 涉及个人隐私"
        assert row.content  # content preserved for AuditLog-pending review
    assert rows[bystander.id].is_hard_hidden is False
    assert rows[grandchild.id].parent_id == child.id
    assert rows[child.id].parent_id == root.id

    # The public list renders NOTHING of the hidden subtree — no tombstone.
    items, total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert total == 1
    assert [item.id for item in items] == [bystander.id]

    audits = events.of_type(COMMENT_HARD_HIDDEN)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.aggregate_type == "Comment"
    assert audit.aggregate_id == root.id
    assert audit.occurred_at == _T1
    assert audit.payload["reason"] == "涉及个人隐私"
    assert audit.payload["deleted_by"] == str(admin.id)
    assert audit.payload["task_id"] == str(task.id)
    assert set(audit.payload["hidden_comment_ids"]) == {
        str(root.id),
        str(child.id),
        str(grandchild.id),
    }
    assert len(events.events) == 1


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.TEACHER, Role.STUDENT])
async def test_hard_hide_requires_admin(db_session: AsyncSession, role: Role) -> None:
    task, teacher, _, root, _, _ = await _three_level_thread(db_session)
    if role is Role.TEACHER:
        actor_user: User = teacher  # even the task owner cannot hard-hide
    else:
        actor_user = _user(username="student-hider", role=role)
        await _seed(db_session, actor_user)
    events = InMemoryEventCollector()

    with pytest.raises(CommentAdminRequiredError) as raised:
        await _service(events=events).admin_hard_hide_subtree(
            db_session, _actor(actor_user), root.id, "越权隐藏"
        )
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403

    row = await db_session.scalar(select(Comment).where(Comment.id == root.id))
    assert row is not None
    assert row.is_hard_hidden is False
    assert row.deleted_at is None
    assert events.events == []


@pytest.mark.integration
@pytest.mark.parametrize("reason", ["", "   "])
async def test_hard_hide_requires_a_reason(
    db_session: AsyncSession, reason: str
) -> None:
    _, _, root, _, _, _ = await _three_level_thread(db_session)
    admin = _admin("admin-0002@pku.edu.cn")
    await _seed(db_session, admin)
    events = InMemoryEventCollector()

    with pytest.raises(ModerationReasonRequiredError) as raised:
        await _service(events=events).admin_hard_hide_subtree(
            db_session, _actor(admin), root.id, reason
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400

    row = await db_session.scalar(select(Comment).where(Comment.id == root.id))
    assert row is not None
    assert row.is_hard_hidden is False
    assert events.events == []


@pytest.mark.integration
async def test_hard_hide_of_middle_comment_hides_only_its_subtree(
    db_session: AsyncSession,
) -> None:
    task, _, root, child, grandchild, bystander = await _three_level_thread(db_session)
    admin = _admin("admin-0003@pku.edu.cn")
    await _seed(db_session, admin)

    await _service().admin_hard_hide_subtree(
        db_session, _actor(admin), child.id, "只隐藏中间层"
    )

    rows = await _task_rows(db_session, task)
    assert rows[root.id].is_hard_hidden is False
    assert rows[root.id].deleted_at is None
    assert rows[child.id].is_hard_hidden is True
    assert rows[grandchild.id].is_hard_hidden is True
    assert rows[bystander.id].is_hard_hidden is False

    items, total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert {item.id for item in items} == {root.id, bystander.id}
    assert total == 2


@pytest.mark.integration
async def test_hard_hidden_comment_still_serializes_for_moderation_with_flag(
    db_session: AsyncSession,
) -> None:
    """T8 shape: the moderation surface still sees the hidden row — with
    its content for review and an explicit hard_hidden flag — while the
    public surface renders nothing."""
    task, _, root, _, _, _ = await _three_level_thread(db_session)
    admin = _admin("admin-0004@pku.edu.cn")
    await _seed(db_session, admin)
    await _service().admin_hard_hide_subtree(
        db_session, _actor(admin), root.id, "隐私下架"
    )

    row = await db_session.scalar(select(Comment).where(Comment.id == root.id))
    assert row is not None
    moderation = serialize_moderation_comment(
        row, author_nickname=_STUDENT_NICKNAME, key_secret="integration-key-secret"
    )
    assert moderation.content == "根：涉及隐私"
    assert moderation.hard_hidden is True
    assert moderation.deleted is True
    assert moderation.moderation_key is None  # named record: no key

    items, _total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert all(item.id != root.id for item in items)


@pytest.mark.integration
async def test_reply_to_hard_hidden_comment_rejected(db_session: AsyncSession) -> None:
    """Spec §21.2 bars replying under 彻底隐藏 comments: the hide sets the
    soft-delete trio, so the existing parent guard refuses new anchors."""
    task, _, root, _, _, _ = await _three_level_thread(db_session)
    admin = _admin("admin-0005@pku.edu.cn")
    await _seed(db_session, admin)
    await _service().admin_hard_hide_subtree(
        db_session, _actor(admin), root.id, "整串下架"
    )
    student = _user(username="20250010003", role=Role.STUDENT)
    await _seed(db_session, student)

    with pytest.raises(ParentCommentDeletedError):
        await _service().create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="回复隐藏评论", parent_id=root.id),
        )


@pytest.mark.integration
async def test_hard_hide_escalates_already_soft_deleted_root(
    db_session: AsyncSession,
) -> None:
    """Hard hide is the STRONGER removal: it may target a comment that was
    already soft-deleted (a tombstone) — the point is to disappear its
    surviving descendants' content too."""
    task, student, root, child, grandchild, bystander = await _three_level_thread(
        db_session
    )
    await _service(clock=FrozenClock(_T1)).delete_own_comment(
        db_session, _actor(student), root.id
    )
    items, _total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    # tombstone + surviving child, grandchild, and the bystander thread
    assert {item.id for item in items} == {
        root.id,
        child.id,
        grandchild.id,
        bystander.id,
    }

    admin = _admin("admin-0006@pku.edu.cn")
    await _seed(db_session, admin)
    await _service(clock=FrozenClock(_T2)).admin_hard_hide_subtree(
        db_session, _actor(admin), root.id, "升级为彻底隐藏"
    )

    rows = await _task_rows(db_session, task)
    assert rows[root.id].is_hard_hidden is True
    assert rows[child.id].is_hard_hidden is True
    items, total = await _service().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert [item.id for item in items] == [bystander.id]  # no tombstone survives
    assert total == 1
