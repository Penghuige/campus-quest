# backend/tests/integration/community/test_votes.py
"""Like/dislike vote toggle against real PostgreSQL (spec §22, §31.8; plan
06 task 4).

Concurrency FIRST (the hard part this service exists to solve), then the
four spec transitions, then the gates:

- **Race 1 — none->+1 vs none->-1:** two independent sessions barriered on
  one Event both create from none; the UNIQUE(comment_id, user_id)
  constraint plus the loser's one retry of the locked path must leave
  EXACTLY ONE row whose value is one of the committed transitions, both
  callers succeed (the retry is part of the contract, not an error), and
  the counts each caller saw match that caller's own committed stance.
- **Race 2 — none->+1 vs +1->none:** the create/remove switch race; the
  outcome is genuinely either (the remover may run before the creator's
  row exists, or lock and delete it after), so the assertions are "one or
  zero rows, never a constraint violation, never an escaped exception".
- **Transitions (spec §22 允许):** none->like, like->none, like->dislike,
  dislike->like — each leaves at most one row, and a flip UPDATES the row
  in place (same primary key), never delete+reinsert.
- **Gates:** the T2 writer rule (Student + ACTIVE on the users row), the
  comment-visibility rule (PUBLISHED task; soft-deleted tombstones and
  Admin-hard-hidden comments are not votable), typed rejections for
  unknown users/comments and for out-of-domain values.

Harness notes (the claim-concurrency precedent):

- Race tests run through ``VoteService.set_vote`` with one *independent*
  session per call, all released simultaneously by an ``asyncio.Event``
  barrier; each connection is warmed with ``SELECT 1`` BEFORE the barrier
  so asyncpg setup time cannot serialize the contenders into hiding the
  interleaving under test.
- Seeding and cleanup use their own sessions with REAL commits: the
  savepoint-wrapped ``db_session`` fixture is invisible to other
  connections, so every committed row is removed by explicit committed
  DELETEs in ``finally`` (votes -> comments -> tasks -> users, the FK
  order). Usernames embed a per-run token so rows leaked by an aborted
  run can never collide with a later seeding pass.
- The transition and gate tests use the ordinary rollback harness.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.community.comment_service import (
    CommentDeletedError,
    CommenterAccountNotActiveError,
    CommenterNotFoundError,
    CommenterNotParticipantError,
    CommentNotFoundError,
)
from app.modules.community.models import Comment, CommentVote
from app.modules.community.schemas import VoteResult
from app.modules.community.vote_service import (
    InvalidVoteValueError,
    VoteService,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.tasks.enums import (
    DeadlineMode,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Task
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


async def _thread_fixture(db: AsyncSession) -> tuple[Task, User, User, User]:
    """A teacher-owned PUBLISHED task plus two ACTIVE students: the comment
    author and a second voter, so count assertions can span users."""
    teacher = _user(username="teacher0001@pku.edu.cn", role=Role.TEACHER)
    student = _user(username="20250010001", nickname="投票人甲")
    other = _user(username="20250010002", nickname="投票人乙")
    await _persist(db, teacher, student, other)
    task = _task(teacher)
    await _persist(db, task)
    return task, teacher, student, other


async def _root_comment(
    db: AsyncSession, task: Task, user: User, **overrides: Any
) -> Comment:
    comment = _comment(task, user, **overrides)
    await _persist(db, comment)
    return comment


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _votes(session: AsyncSession, comment_id: UUID) -> list[CommentVote]:
    return list(
        await session.scalars(
            select(CommentVote)
            .where(CommentVote.comment_id == comment_id)
            .order_by(CommentVote.created_at, CommentVote.id)
        )
    )


# --- concurrency harness ---------------------------------------------------------


@dataclasses.dataclass(slots=True)
class VoteOutcome:
    """One vote attempt's terminal state: a result, a business error, or —
    never, if the service is correct — an unexpected exception."""

    result: VoteResult | None = None
    error: BusinessError | None = None
    unexpected: BaseException | None = None


async def _vote_one(
    service: VoteService,
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    comment_id: UUID,
    value: int,
    start: asyncio.Event,
) -> VoteOutcome:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier (asyncpg setup is
        # ~10 ms of TCP + auth; without the warm-up the barrier releases
        # into sequential-looking runs that hide the interleaving under
        # test — the claim-concurrency lesson, verified by mutation there).
        await session.execute(text("SELECT 1"))
        await start.wait()  # park every transaction on one barrier
        try:
            result = await service.set_vote(session, user_id, comment_id, value)
        except BusinessError as exc:
            return VoteOutcome(error=exc)
        except Exception as exc:  # the "no 500" failure mode
            return VoteOutcome(unexpected=exc)
        return VoteOutcome(result=result)


async def _run_concurrently(
    service: VoteService,
    factory: async_sessionmaker[AsyncSession],
    calls: list[tuple[UUID, int]],
    comment_id: UUID,
) -> list[VoteOutcome]:
    start = asyncio.Event()
    tasks = [
        asyncio.create_task(
            _vote_one(service, factory, user_id, comment_id, value, start)
        )
        for user_id, value in calls
    ]
    await asyncio.sleep(0.05)  # let every coroutine reach the barrier
    start.set()
    return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=15))


# --- race 1: both create from none -------------------------------------------------


@pytest.mark.integration
async def test_concurrent_opposing_votes_from_none_leave_exactly_one_row(
    db_engine: AsyncEngine,
) -> None:
    """none->+1 races none->-1 across two independent sessions: the UNIQUE
    constraint makes exactly one insert win; the loser's one retry of the
    locked path applies its transition ON the winner's row. Exactly one
    row survives, both callers succeed, the final value is one of the two
    committed transitions, and the counts each caller saw match the rows
    as of that caller's own commit."""
    factory = _factory(db_engine)
    service = VoteService()
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    comment_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001", nickname="并发投票人")
            await _persist(session, teacher, student)
            task = _task(teacher)
            await _persist(session, task)
            comment = _comment(task, student)
            await _persist(session, comment)
            await session.commit()
            task_ids.append(task.id)
            comment_ids.append(comment.id)
            user_ids.extend([teacher.id, student.id])

        outcomes = await _run_concurrently(
            service,
            factory,
            [(student.id, 1), (student.id, -1)],
            comment_ids[0],
        )

        assert not [o for o in outcomes if o.unexpected is not None], [
            repr(o.unexpected) for o in outcomes if o.unexpected is not None
        ]
        assert not [o for o in outcomes if o.error is not None], [
            repr(o.error) for o in outcomes if o.error is not None
        ]

        # Each caller applied its own transition (the loser retried onto
        # the winner's row), so each result reports its requested stance
        # with counts matching that stance.
        assert sorted(o.result.current_value for o in outcomes) == [-1, 1]
        for outcome in outcomes:
            assert outcome.result.likes == (outcome.result.current_value == 1)
            assert outcome.result.dislikes == (outcome.result.current_value == -1)

        async with factory() as session:
            rows = await _votes(session, comment_ids[0])
            assert len(rows) == 1  # never two rows for one (comment, user)
            final = rows[0]
            assert final.user_id == student.id
            assert final.value in (1, -1)  # one of the committed transitions

            # The caller whose transition committed LAST reported counts
            # that still match the final rows (the other's counts were
            # correct at its own commit instant).
            last = next(o for o in outcomes if o.result.current_value == final.value)
            assert last.result.likes == (final.value == 1)
            assert last.result.dislikes == (final.value == -1)
    finally:
        async with factory() as session:
            if comment_ids:
                await session.execute(
                    delete(CommentVote).where(CommentVote.comment_id.in_(comment_ids))
                )
                await session.execute(
                    delete(Comment).where(Comment.id.in_(comment_ids))
                )
            if task_ids:
                await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.commit()


# --- race 2: create races remove ---------------------------------------------------


@pytest.mark.integration
async def test_concurrent_create_vs_remove_race_leaves_at_most_one_row(
    db_engine: AsyncEngine,
) -> None:
    """none->+1 races +1->none: the remover either runs before the creator's
    row exists (no-op remove) or locks and deletes the created row. Either
    interleaving is a legal outcome — one row (value +1) or zero rows —
    but never a constraint violation and never an escaped exception."""
    factory = _factory(db_engine)
    service = VoteService()
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    comment_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}002", nickname="切换投票人")
            await _persist(session, teacher, student)
            task = _task(teacher)
            await _persist(session, task)
            comment = _comment(task, student)
            await _persist(session, comment)
            await session.commit()
            task_ids.append(task.id)
            comment_ids.append(comment.id)
            user_ids.extend([teacher.id, student.id])

        outcomes = await _run_concurrently(
            service,
            factory,
            [(student.id, 1), (student.id, 0)],
            comment_ids[0],
        )

        assert not [o for o in outcomes if o.unexpected is not None], [
            repr(o.unexpected) for o in outcomes if o.unexpected is not None
        ]
        assert not [o for o in outcomes if o.error is not None], [
            repr(o.error) for o in outcomes if o.error is not None
        ]
        assert [o.result.current_value for o in outcomes] == [1, 0]

        async with factory() as session:
            rows = await _votes(session, comment_ids[0])
            assert len(rows) in (0, 1)  # at most one row, never two
            if rows:
                assert rows[0].value == 1  # only the creator ever inserts
    finally:
        async with factory() as session:
            if comment_ids:
                await session.execute(
                    delete(CommentVote).where(CommentVote.comment_id.in_(comment_ids))
                )
                await session.execute(
                    delete(Comment).where(Comment.id.in_(comment_ids))
                )
            if task_ids:
                await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.commit()


# --- the four spec §22 transitions --------------------------------------------------


@pytest.mark.integration
async def test_none_to_like_inserts_exactly_one_row(
    db_session: AsyncSession,
) -> None:
    """none -> like: one INSERT, one row, and the result reports the
    caller's new stance with the comment's counts."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)

    result = await VoteService().set_vote(db_session, student.id, comment.id, 1)

    assert result.current_value == 1
    assert result.likes == 1
    assert result.dislikes == 0
    rows = await _votes(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].value == 1
    assert rows[0].user_id == student.id


@pytest.mark.integration
async def test_like_to_none_deletes_the_row(db_session: AsyncSession) -> None:
    """like -> none: "none" is the ABSENCE of a row (spec §22 允许 none),
    so the toggle deletes it — no value=0 placeholder is ever stored."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = VoteService()
    await service.set_vote(db_session, student.id, comment.id, 1)

    result = await service.set_vote(db_session, student.id, comment.id, 0)

    assert result.current_value == 0
    assert result.likes == 0
    assert result.dislikes == 0
    assert await _votes(db_session, comment.id) == []  # the row is GONE


@pytest.mark.integration
async def test_like_to_dislike_flips_the_row_in_place(
    db_session: AsyncSession,
) -> None:
    """like -> dislike: an UPDATE of the SAME row (same primary key), never
    delete-plus-reinsert, and still exactly one row."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = VoteService()
    await service.set_vote(db_session, student.id, comment.id, 1)
    inserted_id = (await _votes(db_session, comment.id))[0].id

    result = await service.set_vote(db_session, student.id, comment.id, -1)

    assert result.current_value == -1
    assert result.likes == 0
    assert result.dislikes == 1
    rows = await _votes(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].id == inserted_id  # flipped in place
    assert rows[0].value == -1


@pytest.mark.integration
async def test_dislike_to_like_flips_the_row_in_place(
    db_session: AsyncSession,
) -> None:
    """dislike -> like: the mirror flip, again one row updated in place."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = VoteService()
    await service.set_vote(db_session, student.id, comment.id, -1)
    inserted_id = (await _votes(db_session, comment.id))[0].id

    result = await service.set_vote(db_session, student.id, comment.id, 1)

    assert result.current_value == 1
    assert result.likes == 1
    assert result.dislikes == 0
    rows = await _votes(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].id == inserted_id  # flipped in place
    assert rows[0].value == 1


@pytest.mark.integration
async def test_full_toggle_cycle_stays_within_one_row(
    db_session: AsyncSession,
) -> None:
    """The spec's four transitions chained — none->like, like->dislike,
    dislike->like, like->none — never leave more than one row and end
    back at none (no row)."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = VoteService()
    seen_row_ids: set[UUID] = set()

    for value in (1, -1, 1):
        result = await service.set_vote(db_session, student.id, comment.id, value)
        assert result.current_value == value
        rows = await _votes(db_session, comment.id)
        assert len(rows) == 1
        seen_row_ids.add(rows[0].id)
        assert rows[0].value == value

    assert await service.set_vote(db_session, student.id, comment.id, 0) == VoteResult(
        current_value=0, likes=0, dislikes=0
    )
    assert await _votes(db_session, comment.id) == []
    assert len(seen_row_ids) == 1  # one row, updated in place throughout


@pytest.mark.integration
async def test_counts_span_users_and_same_value_vote_is_idempotent(
    db_session: AsyncSession,
) -> None:
    """Two users voting opposite directions give likes=1, dislikes=1; a
    repeat of an existing stance neither adds a row nor changes counts
    (idempotent), and removing an absent vote is a no-op returning none."""
    task, _, student, other = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = VoteService()

    assert await service.set_vote(db_session, student.id, comment.id, 1) == VoteResult(
        current_value=1, likes=1, dislikes=0
    )
    assert await service.set_vote(db_session, other.id, comment.id, -1) == VoteResult(
        current_value=-1, likes=1, dislikes=1
    )

    # Same-value re-vote: still exactly two rows, counts unchanged.
    repeat = await service.set_vote(db_session, student.id, comment.id, 1)
    assert repeat == VoteResult(current_value=1, likes=1, dislikes=1)
    assert len(await _votes(db_session, comment.id)) == 2

    # Remove-when-none: zero rows created, stance none.
    third = _user(username="20250010003", nickname="路人丙")
    await _persist(db_session, third)
    assert await service.set_vote(db_session, third.id, comment.id, 0) == VoteResult(
        current_value=0, likes=1, dislikes=1
    )
    assert len(await _votes(db_session, comment.id)) == 2


# --- gates --------------------------------------------------------------------------


@pytest.mark.integration
async def test_vote_admits_teachers_refuses_admin(db_session: AsyncSession) -> None:
    """The PR #2 hardening participant ruling (spec §4.2 "普通社区能力"):
    a Teacher votes like any Student — same toggle, same counts; Admin
    is refused with the typed participant error (its community powers
    are the governance surfaces), and nothing is written for it."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    teacher = _user(username="teacher-voter@pku.edu.cn", role=Role.TEACHER)
    admin = _user(username="admin-voter@pku.edu.cn", role=Role.ADMIN)
    await _persist(db_session, teacher, admin)

    voted = await VoteService().set_vote(db_session, teacher.id, comment.id, 1)
    assert voted == VoteResult(current_value=1, likes=1, dislikes=0)
    assert len(await _votes(db_session, comment.id)) == 1

    with pytest.raises(CommenterNotParticipantError) as raised:
        await VoteService().set_vote(db_session, admin.id, comment.id, 1)
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403
    assert len(await _votes(db_session, comment.id)) == 1


@pytest.mark.integration
async def test_vote_requires_an_active_account(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    suspended = _user(username="20250010004", status=UserStatus.SUSPENDED)
    await _persist(db_session, suspended)

    with pytest.raises(CommenterAccountNotActiveError) as raised:
        await VoteService().set_vote(db_session, suspended.id, comment.id, 1)
    assert raised.value.code == ErrorCode.ACCOUNT_NOT_ACTIVE
    assert raised.value.status_code == 403
    assert await _votes(db_session, comment.id) == []


@pytest.mark.integration
async def test_vote_rejects_unknown_user(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)

    with pytest.raises(CommenterNotFoundError) as raised:
        await VoteService().set_vote(db_session, uuid4(), comment.id, 1)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404


@pytest.mark.integration
async def test_vote_rejects_unknown_comment(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)

    with pytest.raises(CommentNotFoundError) as raised:
        await VoteService().set_vote(db_session, student.id, uuid4(), 1)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404


@pytest.mark.integration
async def test_vote_rejects_soft_deleted_tombstone(db_session: AsyncSession) -> None:
    """A tombstone is not a votable anchor (the §21.2/§21.3 ruling replies
    already follow): new votes on a soft-deleted comment are refused with
    the shared typed error, and existing rows stay untouched."""
    task, _, student, other = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = VoteService()
    await service.set_vote(db_session, other.id, comment.id, 1)
    deleted = await _root_comment(
        db_session,
        task,
        student,
        content="已删除的评论",
        deleted_at=_NOW + timedelta(minutes=1),
        deleted_by=student.id,
        delete_reason="owner",
    )

    with pytest.raises(CommentDeletedError) as raised:
        await service.set_vote(db_session, other.id, deleted.id, -1)
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _votes(db_session, deleted.id) == []
    assert len(await _votes(db_session, comment.id)) == 1  # unrelated intact


@pytest.mark.integration
async def test_vote_rejects_hard_hidden_comment(db_session: AsyncSession) -> None:
    """Admin hard hide writes the soft-delete trio too, so the same guard
    refuses votes on hard-hidden comments (one guard covers both, the
    reply-guard precedent from T3)."""
    task, _, student, other = await _thread_fixture(db_session)
    hidden = await _root_comment(
        db_session,
        task,
        student,
        content="被彻底隐藏的评论",
        deleted_at=_NOW + timedelta(minutes=1),
        deleted_by=student.id,
        delete_reason="ADMIN_HARD_HIDE: 涉及个人隐私",
        is_hard_hidden=True,
    )

    with pytest.raises(CommentDeletedError):
        await VoteService().set_vote(db_session, other.id, hidden.id, 1)
    assert await _votes(db_session, hidden.id) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "status",
    [TaskStatus.DRAFT, TaskStatus.PAUSED, TaskStatus.CLOSED, TaskStatus.ARCHIVED],
)
async def test_vote_requires_a_published_task(
    db_session: AsyncSession, status: TaskStatus
) -> None:
    """The public-surface visibility rule: anything but PUBLISHED — and
    unknown ids alike — answers the shared NOT_FOUND, never the reason."""
    task, teacher, student, _ = await _thread_fixture(db_session)
    off_surface = _task(teacher, status=status)
    await _persist(db_session, off_surface)
    comment = await _root_comment(db_session, off_surface, student)

    with pytest.raises(TaskNotFoundError) as raised:
        await VoteService().set_vote(db_session, student.id, comment.id, 1)
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404
    assert await _votes(db_session, comment.id) == []


@pytest.mark.integration
@pytest.mark.parametrize("value", [2, -2, 100, 0.5])
async def test_vote_rejects_out_of_domain_values(
    db_session: AsyncSession, value: int
) -> None:
    """value is exactly -1, 0, or 1 (the CHECK constraint's domain); the
    service refuses anything else BEFORE touching the database."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)

    with pytest.raises(InvalidVoteValueError) as raised:
        await VoteService().set_vote(db_session, student.id, comment.id, value)
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _votes(db_session, comment.id) == []
