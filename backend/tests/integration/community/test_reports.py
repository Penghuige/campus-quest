# backend/tests/integration/community/test_reports.py
"""Comment reports against real PostgreSQL (spec §23; plan 06 task 6).

Order of concern — the surface this task adds, then its two hard rules
(non-destruction and reporter-identity privacy), then the moderation
surface:

- **Category matrix (spec §23 类别):** each of the four closed members
  (SPAM/HARASSMENT/PRIVACY/OTHER) files exactly one OPEN report; anything
  else — an unlisted word, the lowercase spelling, an empty string, None,
  a non-string — is the typed VALIDATION_ERROR raised BEFORE any
  database touch (the reaction whitelist-first precedent), and it wins
  over the gates.
- **Note:** stored trimmed, blank-or-absent becomes None (notes are
  optional), and the length cap is a typed rejection measured on the
  trimmed text — with the cap injectable so the constant, not the code,
  decides (the comment_max_length pattern).
- **Duplicate (spec §23 同一用户对同一评论同一类别 SHOULD 防止重复刷举报):
  re-reporting the same (comment, reporter, category) is IDEMPOTENT —
  the existing report row comes back (same id, first note wins, still
  exactly one row), never an error; a different category and a different
  reporter each file their own row (the UNIQUE is per triple).
- **Non-destruction (spec §23 不自动删除评论):** reporting writes ONLY
  the report row — the comment's soft-delete trio, hard-hide flag,
  content, and revision history all stay untouched, and the public list
  keeps rendering the comment live.
- **Reporter identity (spec §23 被举报用户不可看到举报者身份):** absent
  from every student-facing surface — the public comment DTO carries no
  report material at all (by construction: no such field exists), the
  serialized page leaks neither the reporter's id nor their nickname,
  and the reported user cannot reach ``list_task_reports``. Reporter
  identity appears exactly once, on the staff-gated moderation listing.
- **Moderation surface (进入治理队列):** ``list_task_reports`` is the
  owner/MODERATE_COMMUNITY/Admin surface (students, unrelated teachers,
  and under-privileged collaborators are denied), reaches non-PUBLISHED
  task history like every governance path, answers NOT_FOUND for unknown
  tasks, scopes to the requested task, carries the comment context (the
  task-8 base shape — no comment-author identity) plus the REPORTER
  identity moderators act on, keeps reports on since-deleted comments
  (the queue reviews history), orders newest first, and pages by
  limit/offset with the rendered total.

Harness: the ordinary rollback suite — no concurrency tests here (the
UNIQUE triple's both-create race is arbitrated by the database exactly
as votes/reactions are, and the sequential idempotency contract is what
this task pins), so every seed lives in the savepoint-wrapped
``db_session`` and needs no manual cleanup.
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

from app.core.error_codes import ErrorCode
from app.modules.community.comment_service import CommentService
from app.modules.community.enums import ReportCategory
from app.modules.community.models import Comment, CommentReport, CommentRevision
from app.modules.community.report_service import (
    DEFAULT_REPORT_NOTE_MAX_LENGTH,
    InvalidReportCategoryError,
    ReportNoteTooLongError,
    ReportService,
    ReportViewDeniedError,
)
from app.modules.community.schemas import CommentPublic
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task, TaskCollaborator
from app.modules.tasks.service import TaskNotFoundError

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


# --- seeding helpers -------------------------------------------------------------


def _user(
    *,
    username: str,
    role: Role = Role.STUDENT,
    status: UserStatus = UserStatus.ACTIVE,
    nickname: str = "测试同学",
) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=nickname,
        phone_e164=None,
        role=role,
        status=status,
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
        "created_at": _NOW,
    }
    fields.update(overrides)
    return Comment(**fields)


async def _persist(session: AsyncSession, *objects: Any) -> None:
    """Add and flush; parents must be flushed before children reference
    their server-generated ids at construction time."""
    session.add_all(objects)
    await session.flush()


async def _thread_fixture(
    db: AsyncSession,
) -> tuple[Task, User, User, User]:
    """A teacher-owned PUBLISHED task plus two ACTIVE students: the
    comment author (the reported user) and a separate reporter, so the
    privacy assertions always span two distinct identities."""
    teacher = _user(username="teacher0001@pku.edu.cn", role=Role.TEACHER)
    author = _user(username="20250010001", nickname="发帖人甲")
    reporter = _user(username="20250010002", nickname="举报人小报")
    await _persist(db, teacher, author, reporter)
    task = _task(teacher)
    await _persist(db, task)
    return task, teacher, author, reporter


async def _root_comment(
    db: AsyncSession, task: Task, user: User, **overrides: Any
) -> Comment:
    comment = _comment(task, user, **overrides)
    await _persist(db, comment)
    return comment


async def _collaborator(
    db: AsyncSession, task: Task, permissions: list[str], username: str
) -> User:
    collaborator = _user(username=username, role=Role.TEACHER)
    await _persist(db, collaborator)
    await _persist(
        db,
        TaskCollaborator(
            task_id=task.id, teacher_id=collaborator.id, permissions=permissions
        ),
    )
    return collaborator


def _actor(user: User, *, role: Role | None = None) -> Actor:
    return Actor(user_id=user.id, role=role if role is not None else Role(user.role))


async def _reports(db: AsyncSession, comment_id: UUID) -> list[CommentReport]:
    return list(
        await db.scalars(
            select(CommentReport)
            .where(CommentReport.comment_id == comment_id)
            .order_by(CommentReport.created_at, CommentReport.id)
        )
    )


async def _seeded_report(
    db: AsyncSession,
    comment: Comment,
    reporter: User,
    *,
    category: str = "SPAM",
    created_at: datetime,
) -> CommentReport:
    """A directly-inserted report row (the pagination/order fixtures need
    deterministic timestamps the service's now() default cannot give)."""
    report = CommentReport(
        comment_id=comment.id,
        reporter_user_id=reporter.id,
        category=category,
        note=None,
        status="OPEN",
        created_at=created_at,
    )
    await _persist(db, report)
    return report


# --- category matrix -----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "category",
    [
        ReportCategory.SPAM,
        ReportCategory.HARASSMENT,
        ReportCategory.PRIVACY,
        ReportCategory.OTHER,
    ],
)
async def test_each_category_files_exactly_one_open_report(
    db_session: AsyncSession, category: ReportCategory
) -> None:
    """All four closed-set members file: one row, the caller as reporter,
    OPEN status, the trimmed note stored."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    report = await ReportService().report_comment(
        db_session, reporter.id, comment.id, category.value, note="  广告骚扰  "
    )

    rows = await _reports(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].id == report.id
    assert rows[0].category == category.value
    assert rows[0].reporter_user_id == reporter.id
    assert rows[0].comment_id == comment.id
    assert rows[0].status == "OPEN"
    assert rows[0].note == "广告骚扰"
    assert rows[0].created_at is not None
    assert rows[0].handled_by is None
    assert rows[0].handled_at is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "category",
    [
        "ABUSE",  # a plausible category that is simply not in the set
        "spam",  # the closed set is exact: lowercase is not a member
        "",
        "SPAM,HARASSMENT",  # not a list surface
        None,
        7,
    ],
)
async def test_invalid_category_is_rejected_before_any_database_touch(
    db_session: AsyncSession, category: Any
) -> None:
    """Anything outside the closed set is the typed VALIDATION_ERROR and
    leaves no row behind."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    with pytest.raises(InvalidReportCategoryError) as raised:
        await ReportService().report_comment(
            db_session, reporter.id, comment.id, category
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert raised.value.details == {"category": str(category)}
    assert await _reports(db_session, comment.id) == []


@pytest.mark.integration
async def test_category_check_precedes_the_gates(db_session: AsyncSession) -> None:
    """The closed-set refusal wins even when the user and comment are
    nonsense: the category check precedes every gate read (the
    whitelist-first precedent)."""
    with pytest.raises(InvalidReportCategoryError):
        await ReportService().report_comment(
            db_session,
            uuid4(),
            uuid4(),
            "ABUSE",
        )


# --- note handling ------------------------------------------------------------------


@pytest.mark.integration
async def test_blank_note_is_stored_as_none(db_session: AsyncSession) -> None:
    """Notes are optional: absent, whitespace-only, and empty all land as
    None — a blank note is no note, never an empty string."""
    task, _, author, reporter = await _thread_fixture(db_session)
    service = ReportService()
    blank = await _root_comment(db_session, task, author, content="空白备注目标")
    absent = await _root_comment(db_session, task, author, content="缺省备注目标")
    whitespace = await _root_comment(db_session, task, author, content="纯空格备注目标")

    assert (
        await service.report_comment(
            db_session, reporter.id, blank.id, "OTHER", note="   "
        )
    ).note is None
    assert (
        await service.report_comment(db_session, reporter.id, absent.id, "OTHER")
    ).note is None
    assert (
        await service.report_comment(
            db_session, reporter.id, whitespace.id, "OTHER", note=""
        )
    ).note is None


@pytest.mark.integration
async def test_note_over_the_cap_is_a_typed_rejection(
    db_session: AsyncSession,
) -> None:
    """The cap is measured on the trimmed note in code points, inclusive
    at the boundary, and refuses with the typed VALIDATION_ERROR leaving
    no row — the comment-content precedent, moderation annotation sized."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service = ReportService()

    with pytest.raises(ReportNoteTooLongError) as raised:
        await service.report_comment(
            db_session,
            reporter.id,
            comment.id,
            "SPAM",
            note="钩" * (DEFAULT_REPORT_NOTE_MAX_LENGTH + 1),
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert raised.value.details == {"max_length": DEFAULT_REPORT_NOTE_MAX_LENGTH}
    assert await _reports(db_session, comment.id) == []

    boundary = await _root_comment(db_session, task, author, content="正好卡线的备注")
    assert (
        await service.report_comment(
            db_session,
            reporter.id,
            boundary.id,
            "SPAM",
            note="钩" * DEFAULT_REPORT_NOTE_MAX_LENGTH,
        )
    ).note == "钩" * DEFAULT_REPORT_NOTE_MAX_LENGTH


@pytest.mark.integration
async def test_note_cap_is_injectable(db_session: AsyncSession) -> None:
    """The constant, not the code, decides: a narrower injected cap
    rejects a note the default admits (the comment_max_length seam)."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    narrow = ReportService(note_max_length=10)

    with pytest.raises(ReportNoteTooLongError):
        await narrow.report_comment(
            db_session, reporter.id, comment.id, "SPAM", note="一二三四五六七八九十一"
        )
    assert await _reports(db_session, comment.id) == []


# --- duplicate handling --------------------------------------------------------------


@pytest.mark.integration
async def test_duplicate_same_category_is_idempotent(db_session: AsyncSession) -> None:
    """Re-reporting the same (comment, reporter, category) returns the
    EXISTING report — same id, first note wins, still exactly one row —
    never an error (spec §23 SHOULD; the UNIQUE triple is the anchor)."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service = ReportService()

    first = await service.report_comment(
        db_session, reporter.id, comment.id, "SPAM", note="第一条备注"
    )
    second = await service.report_comment(
        db_session, reporter.id, comment.id, "SPAM", note="第二条备注"
    )

    assert second.id == first.id
    assert second.note == "第一条备注"  # first-report-wins, no note rewrite
    assert second.created_at == first.created_at
    rows = await _reports(db_session, comment.id)
    assert len(rows) == 1


@pytest.mark.integration
async def test_same_reporter_different_category_coexists(
    db_session: AsyncSession,
) -> None:
    """The UNIQUE is per (comment, reporter, CATEGORY): the same reporter
    may file a different category on the same comment — two rows."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    service = ReportService()

    spam = await service.report_comment(db_session, reporter.id, comment.id, "SPAM")
    harassment = await service.report_comment(
        db_session, reporter.id, comment.id, "HARASSMENT"
    )

    assert spam.id != harassment.id
    rows = await _reports(db_session, comment.id)
    assert {row.category for row in rows} == {"SPAM", "HARASSMENT"}


@pytest.mark.integration
async def test_different_reporter_same_category_coexists(
    db_session: AsyncSession,
) -> None:
    """Two reporters, same category, same comment: the anchor is per
    reporter too — two rows, each with its own reporter."""
    task, _, author, reporter = await _thread_fixture(db_session)
    second_reporter = _user(username="20250010003", nickname="举报人小乙")
    await _persist(db_session, second_reporter)
    comment = await _root_comment(db_session, task, author)
    service = ReportService()

    await service.report_comment(db_session, reporter.id, comment.id, "PRIVACY")
    await service.report_comment(db_session, second_reporter.id, comment.id, "PRIVACY")

    rows = await _reports(db_session, comment.id)
    assert len(rows) == 2
    assert {row.reporter_user_id for row in rows} == {
        reporter.id,
        second_reporter.id,
    }


# --- non-destruction -----------------------------------------------------------------


@pytest.mark.integration
async def test_reporting_never_touches_the_comment_row(
    db_session: AsyncSession,
) -> None:
    """不自动删除评论 (spec §23): the report writes ONLY its own row —
    the soft-delete trio, the hard-hide flag, the content, and the edit
    history all stay exactly as they were."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    await ReportService().report_comment(
        db_session, reporter.id, comment.id, "HARASSMENT"
    )

    await db_session.refresh(comment)
    assert comment.deleted_at is None
    assert comment.deleted_by is None
    assert comment.delete_reason is None
    assert comment.is_hard_hidden is False
    assert comment.content == "这个任务的说明很清楚，做起来很顺利。"
    revisions = list(
        await db_session.scalars(
            select(CommentRevision).where(CommentRevision.comment_id == comment.id)
        )
    )
    assert revisions == []


@pytest.mark.integration
async def test_reported_comment_still_renders_in_the_public_list(
    db_session: AsyncSession,
) -> None:
    """The queue holds the report; the thread holds the comment: the
    public list keeps rendering it live with content and author."""
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)

    await ReportService().report_comment(db_session, reporter.id, comment.id, "SPAM")

    page, total = await CommentService().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    assert total == 1
    assert [item.id for item in page] == [comment.id]
    assert page[0].content == comment.content
    assert page[0].deleted is False
    assert page[0].author_display == "发帖人甲"


# --- reporter identity privacy --------------------------------------------------------


@pytest.mark.integration
async def test_reporter_identity_is_absent_from_student_surfaces(
    db_session: AsyncSession,
) -> None:
    """被举报用户不可看到举报者身份 (spec §23), held by construction:

    - the public comment DTO has no report material at all — no field
      name carries report/reporter semantics;
    - the serialized page leaks neither the reporter's user id nor their
      nickname;
    - the reported user (and any student) cannot reach the moderation
      listing — the only surface reporter identity ever renders on.
    """
    task, _, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    await ReportService().report_comment(
        db_session, reporter.id, comment.id, "PRIVACY", note="泄露了同学的手机号"
    )

    assert not any(
        "report" in field.name for field in dataclasses.fields(CommentPublic)
    )
    page, _ = await CommentService().list_comments(
        db_session, task.id, limit=20, offset=0
    )
    dump = json.dumps([dataclasses.asdict(item) for item in page], default=str)
    assert str(reporter.id) not in dump
    assert "举报人小报" not in dump

    with pytest.raises(ReportViewDeniedError) as raised:
        await ReportService().list_task_reports(
            db_session, _actor(author), task.id, limit=20, offset=0
        )
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403


# --- moderation permission matrix ------------------------------------------------


@pytest.mark.integration
async def test_owner_teacher_lists_the_queue(db_session: AsyncSession) -> None:
    task, teacher, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    report = await ReportService().report_comment(
        db_session, reporter.id, comment.id, "SPAM", note="垃圾广告"
    )

    page, total = await ReportService().list_task_reports(
        db_session, _actor(teacher), task.id, limit=20, offset=0
    )
    assert total == 1
    assert [item.id for item in page] == [report.id]


@pytest.mark.integration
async def test_moderate_community_collaborator_lists_the_queue(
    db_session: AsyncSession,
) -> None:
    task, _, author, reporter = await _thread_fixture(db_session)
    collaborator = await _collaborator(
        db_session, task, ["MODERATE_COMMUNITY"], "teacher0002@pku.edu.cn"
    )
    comment = await _root_comment(db_session, task, author)
    await ReportService().report_comment(db_session, reporter.id, comment.id, "OTHER")

    _, total = await ReportService().list_task_reports(
        db_session, _actor(collaborator), task.id, limit=20, offset=0
    )
    assert total == 1


@pytest.mark.integration
async def test_admin_lists_the_queue(db_session: AsyncSession) -> None:
    """Admin joins the report-queue readers (unlike the moderate-DELETE
    path, which reserves Admin's community power to the hard hide):
    reviewing the queue hides nothing from the platform operator."""
    task, _, author, reporter = await _thread_fixture(db_session)
    admin = _user(username="admin0001@pku.edu.cn", role=Role.ADMIN)
    await _persist(db_session, admin)
    comment = await _root_comment(db_session, task, author)
    await ReportService().report_comment(db_session, reporter.id, comment.id, "SPAM")

    _, total = await ReportService().list_task_reports(
        db_session, _actor(admin), task.id, limit=20, offset=0
    )
    assert total == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("permissions", "username", "role"),
    [
        (["VIEW_TASK", "REVIEW_SUBMISSIONS"], "teacher0003@pku.edu.cn", Role.TEACHER),
        (None, "teacher0004@pku.edu.cn", Role.TEACHER),  # unrelated teacher
        (None, "20250010004", Role.STUDENT),  # a student — even the reporter
    ],
)
async def test_unauthorized_actors_are_denied_the_queue(
    db_session: AsyncSession,
    permissions: list[str] | None,
    username: str,
    role: Role,
) -> None:
    """Only owner/MODERATE_COMMUNITY/Admin read the queue: a collaborator
    without the capability, an unrelated teacher, and students are all
    the same typed PERMISSION_DENIED — with nothing about the queue's
    contents in the payload."""
    task, _, author, reporter = await _thread_fixture(db_session)
    outsider = _user(username=username, role=role, nickname="无关人员")
    await _persist(db_session, outsider)
    if permissions is not None:
        # The under-privileged collaborator IS the seeded outsider — one
        # user, one collaborator row, no username collision.
        await _persist(
            db_session,
            TaskCollaborator(
                task_id=task.id, teacher_id=outsider.id, permissions=permissions
            ),
        )
    comment = await _root_comment(db_session, task, author)
    await ReportService().report_comment(db_session, reporter.id, comment.id, "SPAM")

    actor = _actor(reporter if role is Role.STUDENT else outsider)
    with pytest.raises(ReportViewDeniedError) as raised:
        await ReportService().list_task_reports(
            db_session, actor, task.id, limit=20, offset=0
        )
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403
    assert raised.value.details == {"task_id": str(task.id)}


@pytest.mark.integration
async def test_listing_requires_an_existing_task(db_session: AsyncSession) -> None:
    """Unknown task ids answer the shared NOT_FOUND — the moderation
    surface never confirms or denies existence beyond visibility."""
    task, teacher, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    await ReportService().report_comment(db_session, reporter.id, comment.id, "SPAM")

    with pytest.raises(TaskNotFoundError) as raised:
        await ReportService().list_task_reports(
            db_session,
            _actor(teacher),
            task_id=uuid4(),
            limit=20,
            offset=0,
        )
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404

    # sanity: the seeded task itself lists fine.
    _, total = await ReportService().list_task_reports(
        db_session, _actor(teacher), task.id, limit=20, offset=0
    )
    assert total == 1


@pytest.mark.integration
async def test_listing_reaches_non_published_task_history(
    db_session: AsyncSession,
) -> None:
    """Governance, not browsing: the queue was filed while the task was
    PUBLISHED, the task then PAUSED, and the queue stays readable (the
    moderate-delete precedent — moderation must reach paused/closed
    history too)."""
    teacher = _user(username="teacher0005@pku.edu.cn", role=Role.TEACHER)
    author = _user(username="20250010005", nickname="暂停帖作者")
    reporter = _user(username="20250010006", nickname="暂停帖举报人")
    await _persist(db_session, teacher, author, reporter)
    published_then_paused = _task(teacher)
    await _persist(db_session, published_then_paused)
    comment = await _root_comment(db_session, published_then_paused, author)
    await ReportService().report_comment(db_session, reporter.id, comment.id, "SPAM")
    published_then_paused.status = TaskStatus.PAUSED
    await db_session.flush()

    _, total = await ReportService().list_task_reports(
        db_session, _actor(teacher), published_then_paused.id, limit=20, offset=0
    )
    assert total == 1


# --- listing content ------------------------------------------------------------------


@pytest.mark.integration
async def test_listing_scopes_to_the_task_and_carries_context_and_reporter(
    db_session: AsyncSession,
) -> None:
    """The moderation row: report facts, the comment context in the
    task-8 base shape (no comment-author identity), and the REPORTER
    identity moderators act on — the one surface it ever renders on."""
    teacher = _user(username="teacher0006@pku.edu.cn", role=Role.TEACHER)
    author = _user(username="20250010007", nickname="甲任务作者")
    reporter = _user(username="20250010008", nickname="队列举报人")
    other_author = _user(username="20250010009", nickname="乙任务作者")
    await _persist(db_session, teacher, author, reporter, other_author)
    task_a = _task(teacher)
    task_b = _task(teacher)
    await _persist(db_session, task_a, task_b)
    on_a = await _root_comment(db_session, task_a, author, is_anonymous=True)
    on_b = await _root_comment(db_session, task_b, other_author)
    service = ReportService()
    report = await service.report_comment(
        db_session, reporter.id, on_a.id, "HARASSMENT", note="辱骂其他同学"
    )
    await service.report_comment(db_session, reporter.id, on_b.id, "SPAM")

    page, total = await service.list_task_reports(
        db_session, _actor(teacher), task_a.id, limit=20, offset=0
    )

    assert total == 1  # task B's report stays on task B's queue
    [view] = page
    assert view.id == report.id
    assert view.category == "HARASSMENT"
    assert view.note == "辱骂其他同学"
    assert view.status == "OPEN"
    assert view.reporter_user_id == reporter.id
    assert view.reporter_nickname == "队列举报人"
    assert view.handled_by is None
    assert view.handled_at is None
    # comment context: the task-8 shape — id, content, anonymity; no
    # comment-author identity field exists to fill.
    assert view.comment.id == on_a.id
    assert view.comment.content == on_a.content
    assert view.comment.is_anonymous is True
    assert view.comment.deleted is False


@pytest.mark.integration
async def test_listing_keeps_reports_on_since_deleted_comments(
    db_session: AsyncSession,
) -> None:
    """The queue reviews history: a report on a comment that was later
    soft-deleted (or hard-hidden — same trio) stays listed, with the
    comment context flagging the removal for the moderator."""
    task, teacher, author, reporter = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, author)
    await ReportService().report_comment(db_session, reporter.id, comment.id, "SPAM")
    comment.deleted_at = _NOW + timedelta(minutes=5)
    comment.deleted_by = teacher.id
    comment.delete_reason = "owner"
    await db_session.flush()

    page, total = await ReportService().list_task_reports(
        db_session, _actor(teacher), task.id, limit=20, offset=0
    )
    assert total == 1
    assert page[0].comment.deleted is True
    assert page[0].comment.hard_hidden is False


# --- pagination ------------------------------------------------------------------


@pytest.mark.integration
async def test_listing_orders_newest_first_and_pages_by_limit_offset(
    db_session: AsyncSession,
) -> None:
    """Pagination smoke: newest first, one offset page at a time, the
    rendered total on every call, and past-the-end offsets answer an
    empty page — never an error (the query_service pattern)."""
    task, teacher, author, reporter = await _thread_fixture(db_session)
    comments = [
        await _root_comment(db_session, task, author, content=f"被举报的评论 {index}")
        for index in range(5)
    ]
    seeded: list[CommentReport] = []
    for index, comment in enumerate(comments):
        seeded.append(
            await _seeded_report(
                db_session,
                comment,
                reporter,
                category="OTHER",
                created_at=_NOW + timedelta(minutes=index),
            )
        )

    service = ReportService()
    page, total = await service.list_task_reports(
        db_session, _actor(teacher), task.id, limit=2, offset=0
    )
    assert total == 5
    assert [item.id for item in page] == [
        seeded[4].id,
        seeded[3].id,
    ]

    page, total = await service.list_task_reports(
        db_session, _actor(teacher), task.id, limit=2, offset=2
    )
    assert total == 5
    assert [item.id for item in page] == [seeded[2].id, seeded[1].id]

    page, total = await service.list_task_reports(
        db_session, _actor(teacher), task.id, limit=2, offset=4
    )
    assert total == 5
    assert [item.id for item in page] == [seeded[0].id]

    page, total = await service.list_task_reports(
        db_session, _actor(teacher), task.id, limit=2, offset=20
    )
    assert total == 5
    assert page == []
