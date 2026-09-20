# backend/tests/integration/community/test_community_constraints.py
"""Database-level community constraints (spec §20-23, §31.7-31.9).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects duplicates and invalid values even when the
application forgets:

- UNIQUE(comment_id, user_id) on comment_votes (§22, §31.8);
- UNIQUE(comment_id, user_id, emoji) on comment_reactions (§22, §31.9) —
  the same user MAY react with a different emoji on the same comment;
- vote value CHECK IN (1, -1) (§22);
- UNIQUE(task_id, user_id) on task_ratings (§20, §31.7) with rating
  CHECK between 1 and 5 inclusive;
- report category CHECK over the closed SPAM/HARASSMENT/PRIVACY/OTHER
  set (§23);
- UNIQUE(comment_id, reporter_user_id, category) on comment_reports —
  the same reporter MAY file different categories on the same comment,
  but never the same category twice (§23 SHOULD 防止重复刷举报).

Rows are built after their parents flush: ids are server-generated, so a
transient parent's id is still None at construction time.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.community.enums import ReportCategory, VoteValue
from app.modules.community.models import (
    Comment,
    CommentReaction,
    CommentReport,
    CommentVote,
    TaskRating,
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

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _user(*, username: str, role: Role) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _teacher(username: str = "teacher0001") -> User:
    return _user(username=username, role=Role.TEACHER)


def _student(username: str = "20250010001") -> User:
    return _user(username=username, role=Role.STUDENT)


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
    }
    fields.update(overrides)
    return Comment(**fields)


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


async def _commented_task(
    db_session: AsyncSession,
) -> tuple[Task, Comment]:
    """Flush a teacher-owned task plus one student comment on it."""
    owner = _teacher()
    student = _student()
    await _flush(db_session, owner, student)
    task = _task(owner)
    await _flush(db_session, task)
    comment = _comment(task, student)
    await _flush(db_session, comment)
    return task, comment


@pytest.mark.integration
async def test_duplicate_vote_rejected(db_session: AsyncSession) -> None:
    """One CommentVote per user per Comment (spec §22, §31.8)."""
    _, comment = await _commented_task(db_session)
    voter = _student("20250010002")
    await _flush(db_session, voter)

    await _flush(
        db_session,
        CommentVote(comment_id=comment.id, user_id=voter.id, value=VoteValue.UP),
    )

    db_session.add(
        CommentVote(comment_id=comment.id, user_id=voter.id, value=VoteValue.DOWN)
    )
    with pytest.raises(IntegrityError, match="uq_comment_votes_comment_id_user_id"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("value", [0, 2, -2])
async def test_vote_value_outside_plus_minus_one_rejected(
    db_session: AsyncSession, value: int
) -> None:
    """Vote value is exactly +1 or -1 (spec §22): zero, two, and minus two
    are rejected by the database CHECK, not only by application validation."""
    _, comment = await _commented_task(db_session)
    voter = _student("20250010002")
    await _flush(db_session, voter)

    db_session.add(CommentVote(comment_id=comment.id, user_id=voter.id, value=value))
    with pytest.raises(IntegrityError, match="ck_comment_votes_value"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_reaction_same_emoji_rejected(
    db_session: AsyncSession,
) -> None:
    """One row per user per comment per emoji (spec §22, §31.9)."""
    _, comment = await _commented_task(db_session)
    reactor = _student("20250010002")
    await _flush(db_session, reactor)

    await _flush(
        db_session,
        CommentReaction(comment_id=comment.id, user_id=reactor.id, emoji="👍"),
    )

    db_session.add(
        CommentReaction(comment_id=comment.id, user_id=reactor.id, emoji="👍")
    )
    with pytest.raises(
        IntegrityError, match="uq_comment_reactions_comment_id_user_id_emoji"
    ):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_same_user_different_emoji_reaction_allowed(
    db_session: AsyncSession,
) -> None:
    """Only the full triple is unique: the same user stacking a second,
    different emoji on the same comment inserts cleanly (spec §22)."""
    _, comment = await _commented_task(db_session)
    reactor = _student("20250010002")
    await _flush(db_session, reactor)

    await _flush(
        db_session,
        CommentReaction(comment_id=comment.id, user_id=reactor.id, emoji="👍"),
        CommentReaction(comment_id=comment.id, user_id=reactor.id, emoji="🔥"),
    )


@pytest.mark.integration
@pytest.mark.parametrize("rating", [0, 6])
async def test_rating_outside_one_to_five_rejected(
    db_session: AsyncSession, rating: int
) -> None:
    """Ratings run 1-5 inclusive (spec §20): zero and six are rejected by
    the database CHECK, not only by application validation."""
    task, _ = await _commented_task(db_session)
    rater = _student("20250010002")
    await _flush(db_session, rater)

    db_session.add(TaskRating(task_id=task.id, user_id=rater.id, rating=rating))
    with pytest.raises(IntegrityError, match="ck_task_ratings_rating"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("rating", [1, 5])
async def test_rating_boundary_values_accepted(
    db_session: AsyncSession, rating: int
) -> None:
    """Both ends of the 1-5 range insert cleanly (spec §20)."""
    task, _ = await _commented_task(db_session)
    rater = _student(f"2025002000{rating}")
    await _flush(db_session, rater)

    await _flush(
        db_session, TaskRating(task_id=task.id, user_id=rater.id, rating=rating)
    )


@pytest.mark.integration
async def test_duplicate_task_rating_rejected(
    db_session: AsyncSession,
) -> None:
    """One TaskRating per user per Task (spec §20, §31.7): updates happen
    in place; a second row for the same pair is rejected."""
    task, _ = await _commented_task(db_session)
    rater = _student("20250010002")
    await _flush(db_session, rater)

    await _flush(db_session, TaskRating(task_id=task.id, user_id=rater.id, rating=4))

    db_session.add(TaskRating(task_id=task.id, user_id=rater.id, rating=2))
    with pytest.raises(IntegrityError, match="uq_task_ratings_task_id_user_id"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_report_invalid_category_rejected(
    db_session: AsyncSession,
) -> None:
    """Report categories are the closed SPAM/HARASSMENT/PRIVACY/OTHER set
    (spec §23): anything else is rejected by the database CHECK."""
    _, comment = await _commented_task(db_session)
    reporter = _student("20250010002")
    await _flush(db_session, reporter)

    db_session.add(
        CommentReport(
            comment_id=comment.id,
            reporter_user_id=reporter.id,
            category="DEFAMATION",
        )
    )
    with pytest.raises(IntegrityError, match="ck_comment_reports_category"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_duplicate_report_same_category_rejected(
    db_session: AsyncSession,
) -> None:
    """Same reporter, same comment, same category files once (spec §23:
    同一用户对同一评论同一类别 SHOULD 防止重复刷举报)."""
    _, comment = await _commented_task(db_session)
    reporter = _student("20250010002")
    await _flush(db_session, reporter)

    await _flush(
        db_session,
        CommentReport(
            comment_id=comment.id,
            reporter_user_id=reporter.id,
            category=ReportCategory.SPAM,
        ),
    )

    db_session.add(
        CommentReport(
            comment_id=comment.id,
            reporter_user_id=reporter.id,
            category=ReportCategory.SPAM,
        )
    )
    with pytest.raises(
        IntegrityError,
        match="uq_comment_reports_comment_id_reporter_user_id_category",
    ):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_same_reporter_different_category_allowed(
    db_session: AsyncSession,
) -> None:
    """Only the full triple is unique: escalating the same comment with a
    second category stays representable (spec §23 lists four independent
    categories, and re-reporting rules are per category)."""
    _, comment = await _commented_task(db_session)
    reporter = _student("20250010002")
    await _flush(db_session, reporter)

    await _flush(
        db_session,
        CommentReport(
            comment_id=comment.id,
            reporter_user_id=reporter.id,
            category=ReportCategory.SPAM,
        ),
        CommentReport(
            comment_id=comment.id,
            reporter_user_id=reporter.id,
            category=ReportCategory.PRIVACY,
        ),
    )
