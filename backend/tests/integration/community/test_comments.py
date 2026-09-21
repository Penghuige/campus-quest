# backend/tests/integration/community/test_comments.py
"""Comment creation and safe serialization over real PostgreSQL (spec §21,
§21.1-§21.2, §21.4, §40; plan 06 task 2).

Drives ``CommentService.create_comment`` / ``list_comments`` inside the
rollback harness; rows are asserted in the database so the "author identity
ALWAYS stored" half of the anonymity contract is proven next to the
"never serialized" half:

- the write gates: community writes are a Student surface (spec §4.1) and
  need an ACTIVE account; the commented Task must be PUBLISHED — a DRAFT
  or unknown id reads as the same NOT_FOUND the public task surface
  answers (spec §6.2 DRAFT 不可见, "not visible", never "why");
- content rules (spec §21.1): whitespace-only rejected, configurable
  length cap (default 2000) rejected past the boundary, dangerous control
  characters stripped, and an XSS payload stored LITERALLY as plain text
  and returned as JSON text — the module never produces HTML;
- replies (spec §21.2): same-Task parent succeeds and lists; cross-Task,
  missing, and soft-deleted (tombstone) parents are rejected with typed
  errors;
- the public list: newest first with id tiebreak, offset pagination with
  total, deleted comments excluded (task 3 revisits tombstones), and the
  ``edited`` flag reflects CommentRevision existence;
- the leakage contract (spec §21.4/§40): for an anonymous author with a
  KNOWN student number, phone, and email, none of those values nor the
  raw ``user_id`` appear in the serialized comments or in error payloads.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.modules.community.comment_service import (
    CommenterNotParticipantError,
    CommentService,
    ParentCommentCrossTaskError,
    ParentCommentDeletedError,
    ParentCommentNotFoundError,
)
from app.modules.community.models import Comment, CommentRevision
from app.modules.community.schemas import CreateComment
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task
from app.modules.tasks.service import TaskNotFoundError

_T0 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_TEACHER_USERNAME = "teacher0001@pku.edu.cn"
_STUDENT_NUMBER = "20250010001"
_STUDENT_PHONE = "+8613800138000"
_STUDENT_EMAIL = "comment-author@pku.edu.cn"
_STUDENT_NICKNAME = "小北"

_XSS_PAYLOAD = "<img src=x onerror=alert(1)>"


# --- seeding helpers ----------------------------------------------------------------


def _user(
    *,
    username: str,
    role: Role,
    nickname: str = "测试同学",
    status: UserStatus = UserStatus.ACTIVE,
    phone_e164: str | None = None,
    email_normalized: str | None = None,
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=phone_e164,
        email_normalized=email_normalized,
        role=role,
        status=status,
    )


def _teacher() -> User:
    return _user(username=_TEACHER_USERNAME, role=Role.TEACHER)


def _identifiable_student() -> User:
    """A student whose identity facts are all known to the test: student
    number (username), phone, email. The leakage assertions hunt for
    exactly these strings."""
    return _user(
        username=_STUDENT_NUMBER,
        role=Role.STUDENT,
        nickname=_STUDENT_NICKNAME,
        phone_e164=_STUDENT_PHONE,
        email_normalized=_STUDENT_EMAIL,
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


def _deleted_comment(task: Task, user: User) -> Comment:
    """A soft-deleted comment carrying the full §21.3 trio."""
    return _comment(
        task,
        user,
        deleted_at=_T0,
        deleted_by=user.id,
        delete_reason="作者自行删除（测试种子）",
    )


async def _seed(db: AsyncSession, *objects: Any) -> None:
    db.add_all(objects)
    await db.flush()


async def _published_task(db: AsyncSession) -> tuple[Task, User, User]:
    """A teacher-owned PUBLISHED task plus one ACTIVE student commenter."""
    teacher = _teacher()
    student = _identifiable_student()
    await _seed(db, teacher, student)
    task = _task(teacher)
    await _seed(db, task)
    return task, teacher, student


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


def _json(payload: Any) -> str:
    """Serialize exactly the way the transport layer would (task 9 mounts
    the router; until then the DTO dump is the wire shape)."""
    return json.dumps(payload, default=str, ensure_ascii=False)


async def _comment_count(db: AsyncSession, task: Task) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(Comment).where(Comment.task_id == task.id)
        )
        or 0
    )


# --- the happy path and the write gates ----------------------------------------------


@pytest.mark.integration
async def test_student_creates_comment_and_public_list_shows_nickname(
    db_session: AsyncSession,
) -> None:
    task, _, student = await _published_task(db_session)
    service = CommentService()

    public = await service.create_comment(
        db_session,
        _actor(student),
        CreateComment(task_id=task.id, content="  很清楚  "),
    )

    assert public.content == "很清楚"  # trimmed
    assert public.is_anonymous is False
    assert public.author_display == _STUDENT_NICKNAME
    assert public.parent_id is None
    assert public.edited is False
    assert public.deleted is False

    # The identity ALWAYS lands in the row (spec §21.4): anonymity is a
    # display attribute, not a storage one.
    row = await db_session.scalar(select(Comment).where(Comment.task_id == task.id))
    assert row is not None
    assert row.user_id == student.id
    assert row.is_anonymous is False
    assert row.deleted_at is None


@pytest.mark.integration
async def test_teacher_participates_admin_is_refused(
    db_session: AsyncSession,
) -> None:
    """The PR #2 hardening participant ruling: spec §4.2's "除普通社区
    能力外，可：" grants Teacher the ordinary community capabilities, so a
    Teacher comments like any Student (same named display semantics);
    Admin is NOT a participant — its community powers are the governance
    surfaces — and the refusal is the typed participant error with the
    shared PERMISSION_DENIED code."""
    task, _, _ = await _published_task(db_session)
    teacher = _user(
        username="teacher-participant@pku.edu.cn", role=Role.TEACHER, nickname="王老师"
    )
    admin = _user(username="admin-nonparticipant@pku.edu.cn", role=Role.ADMIN)
    await _seed(db_session, teacher, admin)
    service = CommentService()

    public = await service.create_comment(
        db_session,
        _actor(teacher),
        CreateComment(task_id=task.id, content="教师也来补充说明"),
    )
    assert public.author_display == "王老师"  # the same named-display path
    assert public.is_anonymous is False
    assert await _comment_count(db_session, task) == 1

    with pytest.raises(CommenterNotParticipantError) as raised:
        await service.create_comment(
            db_session,
            _actor(admin),
            CreateComment(task_id=task.id, content="管理员不普通参与"),
        )
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403
    assert await _comment_count(db_session, task) == 1


@pytest.mark.integration
async def test_inactive_student_cannot_comment(db_session: AsyncSession) -> None:
    task, _, _ = await _published_task(db_session)
    suspended = _user(
        username="20250010002",
        role=Role.STUDENT,
        status=UserStatus.SUSPENDED,
    )
    await _seed(db_session, suspended)
    service = CommentService()

    with pytest.raises(Exception) as raised:
        await service.create_comment(
            db_session,
            _actor(suspended),
            CreateComment(task_id=task.id, content="被暂停的账号"),
        )
    assert getattr(raised.value, "code", None) == ErrorCode.ACCOUNT_NOT_ACTIVE
    assert raised.value.status_code == 403
    assert await _comment_count(db_session, task) == 0


@pytest.mark.integration
async def test_unknown_commenter_rejected(db_session: AsyncSession) -> None:
    task, _, _ = await _published_task(db_session)
    service = CommentService()

    with pytest.raises(Exception) as raised:
        await service.create_comment(
            db_session,
            Actor(user_id=uuid4(), role=Role.STUDENT),
            CreateComment(task_id=task.id, content="幽灵账号"),
        )
    assert getattr(raised.value, "code", None) == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404
    assert await _comment_count(db_session, task) == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "title"),
    [
        ({"status": TaskStatus.DRAFT}, "草稿任务"),
        ({"status": TaskStatus.PAUSED}, "已暂停任务"),
        ({"status": TaskStatus.CLOSED}, "已关闭任务"),
    ],
)
async def test_comment_requires_a_published_visible_task(
    db_session: AsyncSession, overrides: dict[str, Any], title: str
) -> None:
    """Only PUBLISHED tasks accept comments; every other status answers
    the same NOT_FOUND as the public task surface (spec §6.2: the student
    surface distinguishes visible from not, never why)."""
    teacher = _teacher()
    student = _identifiable_student()
    await _seed(db_session, teacher, student)
    task = _task(teacher, title=title, **overrides)
    await _seed(db_session, task)
    service = CommentService()

    with pytest.raises(TaskNotFoundError):
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="看不见的任务"),
        )

    with pytest.raises(TaskNotFoundError):
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=uuid4(), content="不存在的任务"),
        )
    assert await _comment_count(db_session, task) == 0


# --- content rules (spec §21.1) -------------------------------------------------------


@pytest.mark.integration
async def test_whitespace_only_comment_rejected(db_session: AsyncSession) -> None:
    task, _, student = await _published_task(db_session)
    service = CommentService()

    with pytest.raises(Exception) as raised:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content=" \n\t\r\x00 "),
        )
    assert getattr(raised.value, "code", None) == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _comment_count(db_session, task) == 0


@pytest.mark.integration
async def test_over_length_comment_rejected_at_2000_default(
    db_session: AsyncSession,
) -> None:
    task, _, student = await _published_task(db_session)
    service = CommentService()

    with pytest.raises(Exception) as raised:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="字" * 2001),
        )
    error = raised.value
    assert getattr(error, "code", None) == ErrorCode.VALIDATION_ERROR
    assert error.details == {"max_length": 2000}
    assert await _comment_count(db_session, task) == 0

    # Exactly 2000 characters passes: the boundary is inclusive.
    boundary = await service.create_comment(
        db_session,
        _actor(student),
        CreateComment(task_id=task.id, content="字" * 2000),
    )
    assert len(boundary.content) == 2000


@pytest.mark.integration
async def test_comment_length_cap_is_configurable(db_session: AsyncSession) -> None:
    """The cap travels from Settings through the composition root (task 9
    wires it); the service itself only knows the injected scalar."""
    task, _, student = await _published_task(db_session)
    service = CommentService(comment_max_length=5)

    with pytest.raises(Exception) as raised:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="超过五个字符"),
        )
    assert getattr(raised.value, "code", None) == ErrorCode.VALIDATION_ERROR
    assert raised.value.details == {"max_length": 5}

    public = await service.create_comment(
        db_session, _actor(student), CreateComment(task_id=task.id, content="恰好五字")
    )
    assert public.content == "恰好五字"


@pytest.mark.integration
async def test_dangerous_control_characters_stripped_before_storage(
    db_session: AsyncSession,
) -> None:
    task, _, student = await _published_task(db_session)
    service = CommentService()

    public = await service.create_comment(
        db_session,
        _actor(student),
        CreateComment(task_id=task.id, content="第一行\r\n第二行\x00\x1b\x7f"),
    )

    # \r, NUL, ESC, DEL removed; the newline survives (multi-line comments
    # stay expressible); surrounding whitespace trimmed.
    assert public.content == "第一行\n第二行"
    row = await db_session.scalar(select(Comment).where(Comment.task_id == task.id))
    assert row is not None
    assert row.content == "第一行\n第二行"


@pytest.mark.integration
async def test_xss_payload_stored_literally_and_returned_as_json_text(
    db_session: AsyncSession,
) -> None:
    """Spec §21.1 防 XSS by NEVER producing HTML: the payload round-trips
    through storage and serialization as plain text, and the JSON dump
    carries it as a string value — quote-delimited data, not markup. Any
    escaping that keeps clients safe is the transport's job (task 9), not
    a mutation of the stored content."""
    task, _, student = await _published_task(db_session)
    service = CommentService()

    public = await service.create_comment(
        db_session,
        _actor(student),
        CreateComment(task_id=task.id, content=_XSS_PAYLOAD),
    )
    assert public.content == _XSS_PAYLOAD  # the write path returns it verbatim

    row = await db_session.scalar(select(Comment).where(Comment.task_id == task.id))
    assert row is not None
    assert row.content == _XSS_PAYLOAD  # stored literally, unescaped

    items, _total = await service.list_comments(db_session, task.id, limit=20, offset=0)
    assert items[0].content == _XSS_PAYLOAD

    wire = _json([dataclasses.asdict(item) for item in items])
    # It rides inside a JSON string value ...
    assert f'"content": "{_XSS_PAYLOAD}"' in wire
    # ... and survives a full encode/decode round trip unchanged.
    assert json.loads(wire)[0]["content"] == _XSS_PAYLOAD


# --- replies (spec §21.2) -------------------------------------------------------------


@pytest.mark.integration
async def test_reply_to_same_task_comment_lists_with_parent(
    db_session: AsyncSession,
) -> None:
    task, _, student = await _published_task(db_session)
    other = _user(username="20250010002", role=Role.STUDENT, nickname="同学乙")
    await _seed(db_session, other)
    service = CommentService()

    root = await service.create_comment(
        db_session, _actor(student), CreateComment(task_id=task.id, content="根评论")
    )
    reply = await service.create_comment(
        db_session,
        _actor(other),
        CreateComment(task_id=task.id, content="同任务下的回复", parent_id=root.id),
    )

    assert reply.parent_id == root.id
    assert reply.author_display == "同学乙"

    items, total = await service.list_comments(db_session, task.id, limit=20, offset=0)
    assert total == 2
    assert {item.id for item in items} == {root.id, reply.id}


@pytest.mark.integration
async def test_reply_to_cross_task_comment_rejected(
    db_session: AsyncSession,
) -> None:
    """Spec §21.2 MUST: parent_id cannot point at another Task's comment.
    This same-Task check is also the V1 cycle guard — a create-only parent
    pointer validated against its own Task can never close a loop."""
    teacher = _teacher()
    student = _identifiable_student()
    await _seed(db_session, teacher, student)
    task_a = _task(teacher, title="任务甲")
    task_b = _task(teacher, title="任务乙")
    await _seed(db_session, task_a, task_b)
    foreign = _comment(task_b, student)
    await _seed(db_session, foreign)
    service = CommentService()

    with pytest.raises(ParentCommentCrossTaskError) as raised:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(
                task_id=task_a.id, content="跨任务回复", parent_id=foreign.id
            ),
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _comment_count(db_session, task_a) == 0


@pytest.mark.integration
async def test_reply_to_missing_comment_rejected(db_session: AsyncSession) -> None:
    task, _, student = await _published_task(db_session)
    service = CommentService()

    with pytest.raises(ParentCommentNotFoundError):
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="回复空气", parent_id=uuid4()),
        )
    assert await _comment_count(db_session, task) == 0


@pytest.mark.integration
async def test_reply_to_deleted_parent_rejected(db_session: AsyncSession) -> None:
    """Ruling (documented in comment_service): a soft-deleted parent is a
    tombstone, not a discussion anchor — spec §21.2 bars replying under
    彻底隐藏/删除 comments, so new replies are refused with a typed error
    while already-existing children survive (task 3 shows them)."""
    task, _, student = await _published_task(db_session)
    tombstone = _deleted_comment(task, student)
    await _seed(db_session, tombstone)
    service = CommentService()

    with pytest.raises(ParentCommentDeletedError) as raised:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="回复墓碑", parent_id=tombstone.id),
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _comment_count(db_session, task) == 1  # only the tombstone


# --- the public list ------------------------------------------------------------------


@pytest.mark.integration
async def test_list_pagination_newest_first_with_id_tiebreak(
    db_session: AsyncSession,
) -> None:
    task, _, student = await _published_task(db_session)
    seeded = [
        _comment(
            task,
            student,
            content=f"评论 {index}",
            created_at=_T0 - timedelta(hours=2 - index),
        )
        for index in range(3)
    ]
    # Two comments share a timestamp: the id ASC tiebreak must order them.
    seeded.append(_comment(task, student, content="同时刻甲", created_at=_T0))
    seeded.append(_comment(task, student, content="同时刻乙", created_at=_T0))
    await _seed(db_session, *seeded)
    service = CommentService()

    expected = sorted(
        sorted(seeded, key=lambda comment: comment.id),
        key=lambda comment: comment.created_at,
        reverse=True,
    )

    first_page, total = await service.list_comments(
        db_session, task.id, limit=3, offset=0
    )
    assert total == 5
    assert [item.id for item in first_page] == [comment.id for comment in expected[:3]]

    second_page, total = await service.list_comments(
        db_session, task.id, limit=3, offset=3
    )
    assert total == 5
    assert [item.id for item in second_page] == [comment.id for comment in expected[3:]]

    beyond, total = await service.list_comments(db_session, task.id, limit=3, offset=5)
    assert (beyond, total) == ([], 5)


@pytest.mark.integration
async def test_deleted_comments_excluded_from_public_list(
    db_session: AsyncSession,
) -> None:
    """Task 2 ruling: the public list simply EXCLUDES soft-deleted
    comments; task 3 revisits whether threads render tombstone markers
    (the DTO already carries the ``deleted`` flag for that decision)."""
    task, _, student = await _published_task(db_session)
    live = _comment(task, student, content="存活评论")
    tombstone = _deleted_comment(task, student)
    await _seed(db_session, live, tombstone)
    service = CommentService()

    items, total = await service.list_comments(db_session, task.id, limit=20, offset=0)
    assert total == 1
    assert [item.id for item in items] == [live.id]
    assert items[0].deleted is False


@pytest.mark.integration
async def test_edited_flag_reflects_revision_existence(
    db_session: AsyncSession,
) -> None:
    """The flag's source of truth is CommentRevision existence (spec §21.3
    显示“已编辑”): an edit appends a revision row, so the flag falls out
    of the listing query with no second projection to drift."""
    task, _, student = await _published_task(db_session)
    plain = _comment(task, student, content="从未编辑")
    edited = _comment(task, student, content="已编辑的评论")
    await _seed(db_session, plain, edited)
    await _seed(
        db_session,
        CommentRevision(
            comment_id=edited.id,
            content="已编辑的评论（第一版）",
            edited_at=_T0 + timedelta(minutes=5),
        ),
    )
    service = CommentService()

    items, _total = await service.list_comments(db_session, task.id, limit=20, offset=0)
    flags = {item.content: item.edited for item in items}
    assert flags == {"从未编辑": False, "已编辑的评论": True}


@pytest.mark.integration
async def test_list_requires_a_published_visible_task(
    db_session: AsyncSession,
) -> None:
    teacher = _teacher()
    await _seed(db_session, teacher)
    draft = _task(teacher, status=TaskStatus.DRAFT)
    await _seed(db_session, draft)
    service = CommentService()

    with pytest.raises(TaskNotFoundError):
        await service.list_comments(db_session, draft.id, limit=20, offset=0)
    with pytest.raises(TaskNotFoundError):
        await service.list_comments(db_session, uuid4(), limit=20, offset=0)


# --- the leakage contract (spec §21.4 / §40) ----------------------------------------


@pytest.mark.integration
async def test_anonymous_comment_leaks_no_identity_facts(
    db_session: AsyncSession,
) -> None:
    """The brief's step-1 test, end to end: an anonymous comment by a
    student with a KNOWN student number, phone, and email serializes for
    an ordinary reader without any of those values or the raw user_id —
    in the comment payloads AND in the error payloads the surface can
    produce."""
    task, _, student = await _published_task(db_session)
    service = CommentService()

    public = await service.create_comment(
        db_session,
        _actor(student),
        CreateComment(
            task_id=task.id, content="匿名提问：截止时间怎么算？", is_anonymous=True
        ),
    )

    # The row keeps the real identity; the DTO never shows it.
    row = await db_session.scalar(select(Comment).where(Comment.task_id == task.id))
    assert row is not None
    assert row.user_id == student.id
    assert row.is_anonymous is True
    assert public.author_display == "匿名用户"

    items, _total = await service.list_comments(db_session, task.id, limit=20, offset=0)
    wire = _json([dataclasses.asdict(item) for item in items])
    for fact in (_STUDENT_NUMBER, _STUDENT_PHONE, _STUDENT_EMAIL, str(student.id)):
        assert fact not in wire

    # Error payloads stay clean too: the typed errors this surface raises
    # carry ids and limits, never author material.
    errors: list[str] = []
    with pytest.raises(Exception) as whitespace:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=task.id, content="   "),
        )
    errors.append(
        _json(
            {
                "message": whitespace.value.message,
                "details": whitespace.value.details,
            }
        )
    )

    teacher = _user(username="teacher0002@pku.edu.cn", role=Role.TEACHER)
    await _seed(db_session, teacher)
    other_task = _task(teacher, title="另一个任务")
    await _seed(db_session, other_task)
    with pytest.raises(Exception) as cross:
        await service.create_comment(
            db_session,
            _actor(student),
            CreateComment(task_id=other_task.id, content="跨任务", parent_id=row.id),
        )
    errors.append(
        _json({"message": cross.value.message, "details": cross.value.details})
    )

    for payload in errors:
        for fact in (_STUDENT_NUMBER, _STUDENT_PHONE, _STUDENT_EMAIL, str(student.id)):
            assert fact not in payload
