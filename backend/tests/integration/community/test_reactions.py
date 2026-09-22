# backend/tests/integration/community/test_reactions.py
"""Emoji reactions against real PostgreSQL (spec §22, §31.9; plan 06 task 5).

Whitelist FIRST (the surface this task adds to the community module),
then the toggle semantics, then concurrency (the hard part), then the
gates:

- **Whitelist (spec §22: V1 emoji 从 Admin 配置白名单选择):** the default
  provider answers with exactly the spec's eight emoji; anything outside
  the configured set is refused with the typed VALIDATION_ERROR before
  any database touch — including an HTML snippet (不允许 HTML 或图片
  reaction), a skin-toned variant, a bare ❤ without VS16, and
  multi-emoji sequences, because membership is exact-string over the
  configured set. A fake port stands in for Plan 08's Admin setting to
  pin the seam: the configured set, not the code, decides.
- **Toggle (相同 emoji 重复点击 = toggle):** first click INSERTs exactly
  one row, the same emoji re-click DELETEs it, two different emoji
  coexist for one user/comment (UNIQUE is per emoji, §31.9), and the
  same emoji coexists across users.
- **Concurrency (§31.9 at-most-one-row per triple):**
  - Same-user same-emoji toggles released on one barrier both find the
    row absent and both INSERT: the UNIQUE(comment_id, user_id, emoji)
    anchor makes exactly one insert win; the loser's one retry replays
    its ADD intent onto the winner's committed row — a no-op returning
    True, NOT a fresh toggle judgment that would remove it. Exactly one
    row survives, both callers report added.
  - Toggles over a pre-committed row (add-vs-remove): both judge REMOVE,
    but the FOR UPDATE lock serializes them — the winner deletes, and
    the loser's locked re-read re-evaluates against the committed delete,
    finds nothing, and judges ADD. One remove and one add race; the
    outcome is one row, never a violation, never an escaped exception.
- **Gates:** the T2/T4 writer rule (Student + ACTIVE on the users row),
  the comment-visibility rule (PUBLISHED task; soft-deleted tombstones
  and Admin-hard-hidden comments are not reactable), typed rejections
  for unknown users/comments.

Harness notes (the T4 precedent, itself the claim-concurrency one):

- Race tests run through ``ReactionService.toggle_reaction`` with one
  *independent* session per call, all released simultaneously by an
  ``asyncio.Event`` barrier; each connection is warmed with ``SELECT 1``
  BEFORE the barrier so asyncpg setup time cannot serialize the
  contenders into hiding the interleaving under test.
- Seeding and cleanup use their own sessions with REAL commits: the
  savepoint-wrapped ``db_session`` fixture is invisible to other
  connections, so every committed row is removed by explicit committed
  DELETEs in ``finally`` (reactions -> comments -> tasks -> users, the
  FK order). Usernames embed a per-run token so rows leaked by an
  aborted run can never collide with a later seeding pass.
- The whitelist, toggle, and gate tests use the ordinary rollback
  harness.
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
from app.modules.community.models import Comment, CommentReaction
from app.modules.community.reaction_service import (
    DefaultEmojiWhitelistProvider,
    ReactionService,
    UnknownEmojiError,
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

_SPEC_DEFAULT_EMOJI = frozenset({"👍", "❤️", "😂", "🎉", "😭", "👀", "🤔", "🔥"})


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
    author and a second reactor, so cross-user assertions can span users."""
    teacher = _user(username="teacher0001@pku.edu.cn", role=Role.TEACHER)
    student = _user(username="20250010001", nickname="表情人甲")
    other = _user(username="20250010002", nickname="表情人乙")
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


async def _reactions(session: AsyncSession, comment_id: UUID) -> list[CommentReaction]:
    return list(
        await session.scalars(
            select(CommentReaction)
            .where(CommentReaction.comment_id == comment_id)
            .order_by(CommentReaction.emoji)
        )
    )


class _FakeWhitelist:
    """Plan 08 stand-in: the Admin-configured whitelist as a seam — the
    configured set, not the code, decides what is reactable."""

    def __init__(self, *emoji: str) -> None:
        self._allowed = frozenset(emoji)

    def allowed(self) -> frozenset[str]:
        return self._allowed


# --- whitelist ---------------------------------------------------------------------


@pytest.mark.integration
def test_default_whitelist_is_exactly_the_spec_eight() -> None:
    """The V1 default provider answers with the spec §22 defaults — the
    eight listed emoji, exactly (no extras, no omissions)."""
    assert DefaultEmojiWhitelistProvider().allowed() == _SPEC_DEFAULT_EMOJI


@pytest.mark.integration
@pytest.mark.parametrize(
    "emoji",
    [
        "🐷",  # a perfectly good emoji, just not configured
        "👍🏻",  # skin-toned variant: a different string than 👍
        "❤",  # bare heart without the VS16: ❤️ is configured, ❤ is not
        "👍👍",  # multi-grapheme sequence of allowed parts
        "🔥❤️",  # mixed multi-emoji sequence of allowed parts
        "<script>alert('x')</script>",  # HTML is never a reaction
        "<img src=x onerror=alert(1)>",  # nor an "image reaction"
        "👍 ",  # trailing whitespace
        "",
    ],
)
async def test_unknown_emoji_is_rejected_before_any_database_touch(
    db_session: AsyncSession, emoji: str
) -> None:
    """Membership is exact-string over the configured set: anything else —
    unlisted emoji, variants, sequences, HTML snippets — is the typed
    VALIDATION_ERROR, and no row is ever created."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)

    with pytest.raises(UnknownEmojiError) as raised:
        await ReactionService().toggle_reaction(
            db_session, student.id, comment.id, emoji
        )
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert raised.value.details == {"emoji": emoji}
    assert await _reactions(db_session, comment.id) == []


@pytest.mark.integration
async def test_whitelist_is_checked_before_the_gates(db_session: AsyncSession) -> None:
    """The whitelist refusal wins even when the user and comment are
    nonsense: the emoji check precedes every gate read."""
    with pytest.raises(UnknownEmojiError):
        await ReactionService().toggle_reaction(db_session, uuid4(), uuid4(), "🐷")


@pytest.mark.integration
async def test_whitelist_port_is_the_admin_configured_seam(
    db_session: AsyncSession,
) -> None:
    """A narrower configured set narrows the surface (Plan 08's audited
    setting will sit behind exactly this port): a custom member becomes
    reactable and a default member stops being reactable."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = ReactionService(whitelist=_FakeWhitelist("🫡"))

    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "🫡") is True
    )
    rows = await _reactions(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].emoji == "🫡"

    with pytest.raises(UnknownEmojiError):
        await service.toggle_reaction(db_session, student.id, comment.id, "👍")
    assert len(await _reactions(db_session, comment.id)) == 1  # untouched


# --- toggle semantics ---------------------------------------------------------------


@pytest.mark.integration
async def test_first_click_adds_exactly_one_row(db_session: AsyncSession) -> None:
    """Absent -> INSERT: one row with the caller's (comment, user, emoji)
    — the multi-codepoint ❤️ stored intact (String(16) is characters)."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)

    added = await ReactionService().toggle_reaction(
        db_session, student.id, comment.id, "❤️"
    )

    assert added is True
    rows = await _reactions(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].comment_id == comment.id
    assert rows[0].user_id == student.id
    assert rows[0].emoji == "❤️"


@pytest.mark.integration
async def test_same_emoji_reclick_removes_the_row(db_session: AsyncSession) -> None:
    """相同 emoji 重复点击 = toggle (spec §22): the second click on the
    same emoji reports removal and leaves no row behind."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = ReactionService()

    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "👍") is True
    )
    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "👍") is False
    )
    assert await _reactions(db_session, comment.id) == []


@pytest.mark.integration
async def test_two_different_emoji_coexist_for_one_user(
    db_session: AsyncSession,
) -> None:
    """UNIQUE(comment_id, user_id, emoji) is per emoji (§31.9): 👍 and 🔥
    coexist for the same user/comment, and removing one leaves the other
    untouched."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = ReactionService()

    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "👍") is True
    )
    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "🔥") is True
    )
    assert {row.emoji for row in await _reactions(db_session, comment.id)} == {
        "👍",
        "🔥",
    }

    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "👍") is False
    )
    rows = await _reactions(db_session, comment.id)
    assert [row.emoji for row in rows] == ["🔥"]  # 🔥 survives 👍's removal


@pytest.mark.integration
async def test_same_emoji_coexists_across_users(db_session: AsyncSession) -> None:
    """The per-user anchor: two users reacting with the same emoji are two
    rows — each toggles only their own."""
    task, _, student, other = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = ReactionService()

    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "🎉") is True
    )
    assert await service.toggle_reaction(db_session, other.id, comment.id, "🎉") is True
    assert len(await _reactions(db_session, comment.id)) == 2

    assert (
        await service.toggle_reaction(db_session, student.id, comment.id, "🎉") is False
    )
    rows = await _reactions(db_session, comment.id)
    assert len(rows) == 1
    assert rows[0].user_id == other.id


# --- concurrency harness -------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class ReactionOutcome:
    """One toggle attempt's terminal state: a bool, a business error, or —
    never, if the service is correct — an unexpected exception."""

    added: bool | None = None
    error: BusinessError | None = None
    unexpected: BaseException | None = None


async def _toggle_one(
    service: ReactionService,
    factory: async_sessionmaker[AsyncSession],
    user_id: UUID,
    comment_id: UUID,
    emoji: str,
    start: asyncio.Event,
) -> ReactionOutcome:
    async with factory() as session:
        # Warm the pooled connection BEFORE the barrier (asyncpg setup is
        # ~10 ms of TCP + auth; without the warm-up the barrier releases
        # into sequential-looking runs that hide the interleaving under
        # test — the claim-concurrency lesson, verified by mutation there).
        await session.execute(text("SELECT 1"))
        await start.wait()  # park every transaction on one barrier
        try:
            added = await service.toggle_reaction(session, user_id, comment_id, emoji)
        except BusinessError as exc:
            return ReactionOutcome(error=exc)
        except Exception as exc:  # the "no 500" failure mode
            return ReactionOutcome(unexpected=exc)
        return ReactionOutcome(added=added)


async def _run_concurrently(
    service: ReactionService,
    factory: async_sessionmaker[AsyncSession],
    calls: list[tuple[UUID, str]],
    comment_id: UUID,
) -> list[ReactionOutcome]:
    start = asyncio.Event()
    tasks = [
        asyncio.create_task(
            _toggle_one(service, factory, user_id, comment_id, emoji, start)
        )
        for user_id, emoji in calls
    ]
    await asyncio.sleep(0.05)  # let every coroutine reach the barrier
    start.set()
    return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=15))


async def _triple_rows(
    session: AsyncSession, comment_id: UUID, user_id: UUID, emoji: str
) -> list[CommentReaction]:
    return list(
        await session.scalars(
            select(CommentReaction).where(
                CommentReaction.comment_id == comment_id,
                CommentReaction.user_id == user_id,
                CommentReaction.emoji == emoji,
            )
        )
    )


# --- race 1: same-user same-emoji, both create from absent ----------------------------


@pytest.mark.integration
async def test_concurrent_same_user_same_emoji_toggles_leave_exactly_one_row(
    db_engine: AsyncEngine,
) -> None:
    """Two toggles of the SAME (user, emoji) released on one barrier both
    find the row absent and both INSERT: the UNIQUE(comment_id, user_id,
    emoji) anchor (§31.9) makes exactly one insert win; the loser's one
    retry replays its ADD intent onto the winner's committed row — a
    no-op returning True, NOT a fresh toggle judgment that would remove
    it (two racing clicks are ONE reaction, not add-then-remove).

    The both-add interleaving is the one under test; a fully serialized
    pair (one click committing before the other's row read) is a
    DIFFERENT legal story — judged remove over the winner's row, zero
    rows left — so the barriered pair is re-run on a fresh emoji anchor
    per round until a round genuinely races, and THAT round is pinned to
    exactly one row with both callers reporting added. Every round,
    interleaved or not, must stay legal: no constraint violation
    escaping, no unexpected exception, at most one row per triple."""
    factory = _factory(db_engine)
    service = ReactionService()
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    comment_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}001", nickname="并发表情人")
            await _persist(session, teacher, student)
            task = _task(teacher)
            await _persist(session, task)
            comment = _comment(task, student)
            await _persist(session, comment)
            await session.commit()
            task_ids.append(task.id)
            comment_ids.append(comment.id)
            user_ids.extend([teacher.id, student.id])

        raced = False
        for emoji in ("👍", "❤️", "😂", "🎉", "😭", "👀"):
            outcomes = await _run_concurrently(
                service,
                factory,
                [(student.id, emoji), (student.id, emoji)],
                comment_ids[0],
            )

            assert not [o for o in outcomes if o.unexpected is not None], [
                repr(o.unexpected) for o in outcomes if o.unexpected is not None
            ]
            assert not [o for o in outcomes if o.error is not None], [
                repr(o.error) for o in outcomes if o.error is not None
            ]

            async with factory() as session:
                rows = await _triple_rows(session, comment_ids[0], student.id, emoji)
                added = sorted(o.added for o in outcomes)
                if added == [True, True]:
                    # Both callers found the row absent and inserted, so
                    # the loser's retry is what made it report added: the
                    # retry is contract, not error, and it CONFIRMED the
                    # winner's row instead of toggling it away.
                    raced = True
                    assert len(rows) == 1  # never two rows for one triple
                    assert rows[0].emoji == emoji
                else:
                    # The serialized pair: one add, then a judged remove
                    # over the committed row — zero rows, still legal.
                    assert added == [False, True]
                    assert rows == []
        assert raced, "no round interleaved; the retry path went unexercised"
    finally:
        async with factory() as session:
            if comment_ids:
                await session.execute(
                    delete(CommentReaction).where(
                        CommentReaction.comment_id.in_(comment_ids)
                    )
                )
                await session.execute(
                    delete(Comment).where(Comment.id.in_(comment_ids))
                )
            if task_ids:
                await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.commit()


# --- race 2: add-vs-remove over a pre-committed row -----------------------------------


@pytest.mark.integration
async def test_concurrent_toggles_over_an_existing_row_end_at_one_row(
    db_engine: AsyncEngine,
) -> None:
    """With the row pre-committed, both toggles judge REMOVE, but the FOR
    UPDATE lock serializes them: the winner deletes and commits, and the
    loser's locked re-read re-evaluates against the committed delete,
    finds nothing, and judges ADD. One remove and one add transition race
    (the add-vs-remove shape); the outcome is one row — never two, never
    a constraint violation, never an escaped exception."""
    factory = _factory(db_engine)
    service = ReactionService()
    run = uuid4().hex[:8]

    task_ids: list[UUID] = []
    user_ids: list[UUID] = []
    comment_ids: list[UUID] = []
    try:
        async with factory() as session:
            teacher = _user(username=f"t{run}", role=Role.TEACHER)
            student = _user(username=f"2025{run}002", nickname="切换表情人")
            await _persist(session, teacher, student)
            task = _task(teacher)
            await _persist(session, task)
            comment = _comment(task, student)
            await _persist(session, comment)
            await session.commit()
            task_ids.append(task.id)
            comment_ids.append(comment.id)
            user_ids.extend([teacher.id, student.id])

        # The pre-committed row both toggles will judge REMOVE on.
        async with factory() as session:
            assert (
                await service.toggle_reaction(session, student.id, comment_ids[0], "🔥")
                is True
            )

        outcomes = await _run_concurrently(
            service,
            factory,
            [(student.id, "🔥"), (student.id, "🔥")],
            comment_ids[0],
        )

        assert not [o for o in outcomes if o.unexpected is not None], [
            repr(o.unexpected) for o in outcomes if o.unexpected is not None
        ]
        assert not [o for o in outcomes if o.error is not None], [
            repr(o.error) for o in outcomes if o.error is not None
        ]

        # One caller removed, the other re-added: exactly one row, never
        # two (the §31.9 at-most-one-row guarantee for the triple).
        assert sorted(o.added for o in outcomes) == [False, True]

        async with factory() as session:
            rows = await _reactions(session, comment_ids[0])
            assert len(rows) == 1
            assert rows[0].user_id == student.id
            assert rows[0].emoji == "🔥"
    finally:
        async with factory() as session:
            if comment_ids:
                await session.execute(
                    delete(CommentReaction).where(
                        CommentReaction.comment_id.in_(comment_ids)
                    )
                )
                await session.execute(
                    delete(Comment).where(Comment.id.in_(comment_ids))
                )
            if task_ids:
                await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.commit()


# --- gates ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reaction_admits_teachers_refuses_admin(
    db_session: AsyncSession,
) -> None:
    """The PR #2 hardening participant ruling (spec §4.2 "普通社区能力"):
    a Teacher reacts like any Student; Admin is refused with the typed
    participant error (its community powers are the governance
    surfaces), and nothing is written for it."""
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    teacher = _user(username="teacher-reactor@pku.edu.cn", role=Role.TEACHER)
    admin = _user(username="admin-reactor@pku.edu.cn", role=Role.ADMIN)
    await _persist(db_session, teacher, admin)

    added = await ReactionService().toggle_reaction(
        db_session, teacher.id, comment.id, "👍"
    )
    assert added is True
    assert len(await _reactions(db_session, comment.id)) == 1

    with pytest.raises(CommenterNotParticipantError) as raised:
        await ReactionService().toggle_reaction(db_session, admin.id, comment.id, "👍")
    assert raised.value.code == ErrorCode.PERMISSION_DENIED
    assert raised.value.status_code == 403
    assert len(await _reactions(db_session, comment.id)) == 1


@pytest.mark.integration
async def test_reaction_requires_an_active_account(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    suspended = _user(username="20250010004", status=UserStatus.SUSPENDED)
    await _persist(db_session, suspended)

    with pytest.raises(CommenterAccountNotActiveError) as raised:
        await ReactionService().toggle_reaction(
            db_session, suspended.id, comment.id, "👍"
        )
    assert raised.value.code == ErrorCode.ACCOUNT_NOT_ACTIVE
    assert raised.value.status_code == 403
    assert await _reactions(db_session, comment.id) == []


@pytest.mark.integration
async def test_reaction_rejects_unknown_user(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)

    with pytest.raises(CommenterNotFoundError) as raised:
        await ReactionService().toggle_reaction(db_session, uuid4(), comment.id, "👍")
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404


@pytest.mark.integration
async def test_reaction_rejects_unknown_comment(db_session: AsyncSession) -> None:
    task, _, student, _ = await _thread_fixture(db_session)

    with pytest.raises(CommentNotFoundError) as raised:
        await ReactionService().toggle_reaction(db_session, student.id, uuid4(), "👍")
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404


@pytest.mark.integration
async def test_reaction_rejects_soft_deleted_tombstone(
    db_session: AsyncSession,
) -> None:
    """A tombstone is not a reactable anchor (the §21.2/§21.3 ruling
    replies and votes already follow): new reactions on a soft-deleted
    comment are refused with the shared typed error, and existing rows
    elsewhere stay untouched."""
    task, _, student, other = await _thread_fixture(db_session)
    comment = await _root_comment(db_session, task, student)
    service = ReactionService()
    await service.toggle_reaction(db_session, other.id, comment.id, "👍")
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
        await service.toggle_reaction(db_session, other.id, deleted.id, "🔥")
    assert raised.value.code == ErrorCode.VALIDATION_ERROR
    assert raised.value.status_code == 400
    assert await _reactions(db_session, deleted.id) == []
    assert len(await _reactions(db_session, comment.id)) == 1  # unrelated intact


@pytest.mark.integration
async def test_reaction_rejects_hard_hidden_comment(db_session: AsyncSession) -> None:
    """Admin hard hide writes the soft-delete trio too, so the same guard
    refuses reactions on hard-hidden comments (one guard covers both, the
    T3/T4 precedent)."""
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
        await ReactionService().toggle_reaction(db_session, other.id, hidden.id, "👍")
    assert await _reactions(db_session, hidden.id) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "status",
    [TaskStatus.DRAFT, TaskStatus.PAUSED, TaskStatus.CLOSED, TaskStatus.ARCHIVED],
)
async def test_reaction_requires_a_published_task(
    db_session: AsyncSession, status: TaskStatus
) -> None:
    """The public-surface visibility rule: anything but PUBLISHED — and
    unknown ids alike — answers the shared NOT_FOUND, never the reason."""
    task, teacher, student, _ = await _thread_fixture(db_session)
    off_surface = _task(teacher, status=status)
    await _persist(db_session, off_surface)
    comment = await _root_comment(db_session, off_surface, student)

    with pytest.raises(TaskNotFoundError) as raised:
        await ReactionService().toggle_reaction(
            db_session, student.id, comment.id, "👍"
        )
    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.status_code == 404
    assert await _reactions(db_session, comment.id) == []
