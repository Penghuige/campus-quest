# backend/tests/integration/rankings/test_honor_grants.py
"""Honors against real PostgreSQL (spec §18; plan 05 task 7; §38.6 rows).

Covers the DB half of the fixed-rule evaluation:

- lifetime rules judged from recomputed facts (completed claims, on-time
  streak by §19's tier-100 judgment, earned points from the ranking
  ledger aggregate — redemptions never count, spec §17.1);
- periodic honors: one Honor ROW PER PERIOD, rank == 1 grants 卷王,
  monthly rank <= 3 grants Top 3, and re-evaluating the same period
  grants nothing new (UNIQUE(user_id, honor_id) + the definition
  pre-check); different periods stay independent;
- display honor: ownership required, unset allowed, and the concrete
  ``UserDirectory`` adapter resolves the title from
  ``users.display_honor_id`` (the Plan 05 Task 6 placeholder filled in);
- Admin commemorative honors: admin-only create/grant, idempotent, and
  NEVER a ranking mutation — the points_ledger row count is pinned
  before/after (spec §18: 不允许该操作自动篡改排行榜积分);
- the schema-level guarantees: the period coherence CHECK, the auto
  definition partial unique indexes, and the grant unique constraint.

Harness: the shared ``db_session`` fixture (outer transaction rolled
back per test); claims are seeded directly through the tasks models the
way test_review_flow.py builds them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.directory import SqlAlchemyUserDirectory
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType
from app.modules.points.models import PointsLedger
from app.modules.rankings.honor_models import Honor, HonorType, UserHonor
from app.modules.rankings.honor_service import (
    CommemorativeAdminOnlyError,
    CommemorativeHonorRequiredError,
    HonorEvaluationEvent,
    HonorNotFoundError,
    HonorNotOwnedError,
    HonorService,
    HonorTrigger,
    UserNotFoundError,
)
from app.modules.tasks.enums import (
    AssignmentAvailability,
    ClaimStatus,
    DeadlineMode,
    RewardLockStatus,
    TaskRarity,
    TaskStatus,
    TaskType,
)
from app.modules.tasks.models import Assignment, AssignmentClaim, Task

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

# A stable September window for claim chronology.
_DAY_ZERO = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


# --- seeding helpers ------------------------------------------------------------------


def _user(username: str, role: Role = Role.STUDENT) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _task(owner: User, slug: str) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title=f"数据采集任务 {slug}",
        description="采集指定关键词下的笔记数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=50,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={
            "required_columns": [{"name": "url", "type": "string", "unique": True}]
        },
        submission_schema_version=1,
        allowed_file_types=["CSV"],
        max_file_size_bytes=10 * 1024 * 1024,
        notification_channels=["SMS"],
    )


def _assignment(task: Task, slug: str) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword=f"考研{slug}",
        availability_status=AssignmentAvailability.COMPLETED.value,
    )


def _terminal_claim(
    user: User,
    task: Task,
    assignment: Assignment,
    *,
    tier: int,
    terminal_at: datetime,
    status: ClaimStatus,
) -> AssignmentClaim:
    """One terminal claim carrying the §19 on-time judgment as its lock
    tier (100 = the §9.3 ladder's on-time arm)."""
    completed = status is ClaimStatus.COMPLETED
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=task.id,
        user_id=user.id,
        status=status.value,
        claimed_at=terminal_at - timedelta(days=3),
        deadline_at=terminal_at - timedelta(days=1),
        grace_deadline_at=terminal_at - timedelta(days=1) + timedelta(hours=24),
        reward_policy_snapshot={"version": 1},
        base_reward_points_snapshot=50,
        submission_schema_version=1,
        reward_lock_status=(
            RewardLockStatus.CONFIRMED.value
            if completed
            else RewardLockStatus.NONE.value
        ),
        reward_tier_locked=tier if completed else None,
        locked_reward_points=50 if completed else None,
        reward_locked_at=terminal_at - timedelta(days=1),
        terminal_at=terminal_at,
    )


def _reward(user: User, amount: int, effective_at: datetime) -> PointsLedger:
    return PointsLedger(
        user_id=user.id,
        ledger_type=LedgerType.ASSIGNMENT_REWARD,
        amount=amount,
        source_type="ASSIGNMENT_CLAIM",
        source_id=uuid4(),
        affects_balance=True,
        affects_ranking=True,
        ranking_effective_at=effective_at,
    )


def _redemption(user: User, amount: int) -> PointsLedger:
    """A spend: hits the balance, never the ranking (spec §17.1)."""
    return PointsLedger(
        user_id=user.id,
        ledger_type=LedgerType.REWARD_REDEMPTION,
        amount=amount,
        source_type="REWARD_REDEMPTION",
        source_id=uuid4(),
        affects_balance=True,
        affects_ranking=False,
        ranking_effective_at=None,
    )


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


async def _seed_claims(
    db_session: AsyncSession,
    owner: User,
    student: User,
    *,
    tiers: list[int],
    statuses: list[ClaimStatus] | None = None,
) -> None:
    """``len(tiers)`` claims for one student, one assignment each, in
    chronological order (terminal_at staggered)."""
    if statuses is None:
        statuses = [ClaimStatus.COMPLETED] * len(tiers)
    task = _task(owner, f"{student.username[-4:]}{len(tiers)}")
    await _flush(db_session, task)
    for index, (tier, status) in enumerate(zip(tiers, statuses, strict=True)):
        assignment = _assignment(task, f"{index:03d}")
        await _flush(db_session, assignment)
        await _flush(
            db_session,
            _terminal_claim(
                student,
                task,
                assignment,
                tier=tier,
                terminal_at=_DAY_ZERO + timedelta(days=index, hours=1),
                status=status,
            ),
        )


async def _granted_names(
    db_session: AsyncSession, user_id: UUID
) -> set[str]:
    rows = (
        await db_session.execute(
            select(Honor.name)
            .join(UserHonor, UserHonor.honor_id == Honor.id)
            .where(UserHonor.user_id == user_id)
        )
    ).scalars()
    return set(rows)


async def _newly_granted_names(
    db_session: AsyncSession, granted: list[UserHonor]
) -> set[str]:
    honor_ids = {row.honor_id for row in granted}
    if not honor_ids:
        return set()
    rows = (
        await db_session.execute(select(Honor.name).where(Honor.id.in_(honor_ids)))
    ).scalars()
    return set(rows)


async def _user_honors(
    db_session: AsyncSession, user_id: UUID
) -> list[UserHonor]:
    return list(
        (
            await db_session.execute(
                select(UserHonor).where(UserHonor.user_id == user_id)
            )
        ).scalars()
    )


async def _ledger_count(db_session: AsyncSession) -> int:
    return int(
        await db_session.scalar(
            select(func.count()).select_from(PointsLedger)
        )
        or 0
    )


@pytest.fixture
def honors() -> HonorService:
    return HonorService()


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


# --- lifetime rules from recomputed facts ---------------------------------------------


@pytest.mark.integration
async def test_claim_completed_grants_from_all_lifetime_facts(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """10 on-time completions + 500 earned points grant the 1 and 10
    completed honors, the streak honor, and the 500-point honor in ONE
    evaluation (missed intermediate triggers self-heal); a replay
    grants nothing."""
    teacher = _user("t1000", Role.TEACHER)
    student = _user("2025001001")
    await _flush(db_session, teacher, student)
    await _seed_claims(db_session, teacher, student, tiers=[100] * 10)
    for index in range(10):
        await _flush(
            db_session, _reward(student, 50, _DAY_ZERO + timedelta(days=index))
        )

    granted = await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED),
    )

    expected = {
        "首次完成任务",
        "累计完成 10 个任务",
        "连续 10 个任务按时",
        "累计获得 500 积分",
    }
    assert await _newly_granted_names(db_session, granted) == expected
    assert await _granted_names(db_session, student.id) == expected
    # Four lifetime Honor rows, all period-less.
    honor_rows = (
        await db_session.execute(select(Honor).where(Honor.period.is_(None)))
    ).scalars()
    assert sorted(row.honor_type for row in honor_rows) == [
        "ON_TIME_STREAK",
        "TOTAL_COMPLETED",
        "TOTAL_COMPLETED",
        "TOTAL_EARNED_POINTS",
    ]

    replay = await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED),
    )
    assert replay == []


@pytest.mark.integration
async def test_streak_counts_backwards_until_a_late_completion(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """The streak is the CURRENT run: a late (tier 50) completion AFTER
    ten on-time ones resets it to 0, while a late completion BEFORE ten
    on-time ones leaves the run intact (ordering is by terminal_at)."""
    teacher = _user("t1001", Role.TEACHER)
    late_after = _user("2025001002")
    late_before = _user("2025001003")
    await _flush(db_session, teacher, late_after, late_before)
    await _seed_claims(db_session, teacher, late_after, tiers=[100] * 10 + [50])
    await _seed_claims(db_session, teacher, late_before, tiers=[50] + [100] * 10)

    await honors.evaluate_honors(
        db_session,
        late_after.id,
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED),
    )
    await honors.evaluate_honors(
        db_session,
        late_before.id,
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED),
    )

    assert "连续 10 个任务按时" not in await _granted_names(db_session, late_after.id)
    assert "连续 10 个任务按时" in await _granted_names(db_session, late_before.id)


@pytest.mark.integration
async def test_only_completed_claims_count(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """An ABANDONED claim is not a completion: one COMPLETED claim
    grants only the first-completion honor."""
    teacher = _user("t1002", Role.TEACHER)
    student = _user("2025001004")
    await _flush(db_session, teacher, student)
    await _seed_claims(
        db_session,
        teacher,
        student,
        tiers=[100, 100],
        statuses=[ClaimStatus.COMPLETED, ClaimStatus.ABANDONED],
    )

    granted = await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(trigger=HonorTrigger.CLAIM_COMPLETED),
    )

    assert await _newly_granted_names(db_session, granted) == {"首次完成任务"}


@pytest.mark.integration
async def test_earned_points_follows_the_ranking_ledger_not_the_balance(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """600 earned minus a 100-point redemption still counts as 500
    EARNED (spec §15.1/§17.1: spending never touches ranking
    contribution) — exactly the 500 threshold honor, not 2000."""
    student = _user("2025001005")
    await _flush(db_session, student)
    await _flush(
        db_session,
        _reward(student, 600, _DAY_ZERO),
        _redemption(student, -100),
    )

    granted = await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(trigger=HonorTrigger.POINTS_CHANGED),
    )

    assert await _newly_granted_names(db_session, granted) == {"累计获得 500 积分"}


# --- periodic rank honors ----------------------------------------------------


@pytest.mark.integration
async def test_daily_rank_one_grants_the_period_honor(
    db_session: AsyncSession, honors: HonorService
) -> None:
    student = _user("2025001006")
    second = _user("2025001007")
    await _flush(db_session, student, second)

    granted = await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=1, period="2026-09-21"
        ),
    )
    rejected = await honors.evaluate_honors(
        db_session,
        second.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=2, period="2026-09-21"
        ),
    )

    assert await _newly_granted_names(db_session, granted) == {"今日卷王"}
    (honor,) = (
        await db_session.execute(select(Honor).where(Honor.name == "今日卷王"))
    ).scalars()
    assert honor.period == "2026-09-21"
    assert honor.honor_type == HonorType.DAILY_RANK.value
    assert rejected == []


@pytest.mark.integration
async def test_monthly_rank_top_one_and_top_three_with_period_idempotency(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """Rank 1 grants 本月卷王 + 月度 Top 3 for 2026-09; evaluating the
    SAME period again grants nothing and creates no rows (the task
    brief's idempotency row)."""
    student = _user("2025001008")
    await _flush(db_session, student)
    event = HonorEvaluationEvent(
        trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-09"
    )

    granted = await honors.evaluate_honors(db_session, student.id, event)

    assert await _newly_granted_names(db_session, granted) == {
        "本月卷王",
        "月度 Top 3",
    }
    monthly_rows = (
        await db_session.execute(select(Honor).where(Honor.period == "2026-09"))
    ).scalars()
    assert sorted(row.name for row in monthly_rows) == ["月度 Top 3", "本月卷王"]

    replay = await honors.evaluate_honors(db_session, student.id, event)
    assert replay == []
    assert len(await _user_honors(db_session, student.id)) == 2


@pytest.mark.integration
async def test_periodic_honors_are_independent_across_periods(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """2026-09 rank 1 and 2026-10 rank 1 are two different Honor rows
    (one row per period) and both are grantable."""
    student = _user("2025001009")
    await _flush(db_session, student)
    await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-09"
        ),
    )
    await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-10"
        ),
    )

    kings = (
        await db_session.execute(select(Honor).where(Honor.name == "本月卷王"))
    ).scalars()
    assert sorted(row.period for row in kings) == ["2026-09", "2026-10"]
    # Each month granted its own king AND its own Top 3: four rows total.
    assert len(await _user_honors(db_session, student.id)) == 4


@pytest.mark.integration
async def test_evaluation_for_unknown_user_is_rejected(
    db_session: AsyncSession, honors: HonorService
) -> None:
    with pytest.raises(UserNotFoundError):
        await honors.evaluate_honors(
            db_session,
            uuid4(),
            HonorEvaluationEvent(
                trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-09"
            ),
        )
    with pytest.raises(UserNotFoundError):
        await honors.set_display_honor(db_session, uuid4(), None)


# --- display honor -------------------------------------------------------------


@pytest.mark.integration
async def test_display_honor_requires_ownership_and_resolves_through_directory(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """The single display honor is a pointer the user must own: setting
    an unowned (or unknown) honor is rejected, clearing with None works,
    swapping to another owned honor works, and the concrete
    ``UserDirectory`` adapter surfaces the title the leaderboard
    enriches with (spec §17/§18)."""
    student = _user("2025001010")
    other = _user("2025001011")
    await _flush(db_session, student, other)
    await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.MONTHLY_RANK_KNOWN, rank=1, period="2026-09"
        ),
    )
    # A honor row this user can never own: another user's daily crown in
    # a different period (rank 1 of 2026-09-22, not the shared 2026-09
    # monthly rows a rank-1 winner also owns).
    await honors.evaluate_honors(
        db_session,
        other.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=1, period="2026-09-22"
        ),
    )
    directory = SqlAlchemyUserDirectory()
    king_honor = await db_session.scalar(
        select(Honor).where(Honor.name == "本月卷王", Honor.period == "2026-09")
    )
    other_honor = await db_session.scalar(
        select(Honor).where(Honor.name == "月度 Top 3", Honor.period == "2026-09")
    )
    assert king_honor is not None and other_honor is not None

    unowned_honor = await db_session.scalar(
        select(Honor).where(Honor.name == "今日卷王", Honor.period == "2026-09-22")
    )
    assert unowned_honor is not None

    # Not owned: another user's honor row and an unknown id alike.
    with pytest.raises(HonorNotOwnedError):
        await honors.set_display_honor(db_session, student.id, unowned_honor.id)
    with pytest.raises(HonorNotOwnedError):
        await honors.set_display_honor(db_session, student.id, uuid4())

    # Owned: select, see it through the directory, swap, clear.
    await honors.set_display_honor(db_session, student.id, king_honor.id)
    profile = await directory.get_display_profile(db_session, student.id)
    assert profile is not None
    assert (profile.nickname, profile.display_honor_title) == (
        student.nickname,
        "本月卷王",
    )

    await honors.set_display_honor(db_session, student.id, other_honor.id)
    swapped = await directory.get_display_profile(db_session, student.id)
    assert swapped is not None and swapped.display_honor_title == "月度 Top 3"

    await honors.set_display_honor(db_session, student.id, None)
    cleared = await directory.get_display_profile(db_session, student.id)
    assert cleared is not None and cleared.display_honor_title is None


# --- admin commemorative honors ------------------------------------------------


@pytest.mark.integration
async def test_commemorative_honor_admin_only_idempotent_and_never_ranks(
    db_session: AsyncSession, honors: HonorService
) -> None:
    """Admin creates + grants a纪念 honor (idempotently); non-admins are
    denied; an AUTO honor cannot be granted by hand; and the whole
    surface writes no points_ledger row at all (spec §18's 不允许篡改
    排行榜积分, pinned by row count)."""
    admin = _user("a0001", Role.ADMIN)
    teacher = _user("t1003", Role.TEACHER)
    student = _user("2025001012")
    await _flush(db_session, admin, teacher, student)
    before = await _ledger_count(db_session)

    with pytest.raises(CommemorativeAdminOnlyError):
        await honors.create_commemorative_honor(
            db_session,
            _actor(teacher),
            name="开学纪念",
            description="2026 秋季开学纪念。",
        )

    honor = await honors.create_commemorative_honor(
        db_session,
        _actor(admin),
        name="开学纪念",
        description="2026 秋季开学纪念。",
    )
    assert honor.honor_type == HonorType.COMMEMORATIVE.value
    assert honor.is_auto is False and honor.period is None

    with pytest.raises(CommemorativeAdminOnlyError):
        await honors.grant_commemorative_honor(
            db_session, _actor(teacher), user_id=student.id, honor_id=honor.id
        )

    grant = await honors.grant_commemorative_honor(
        db_session, _actor(admin), user_id=student.id, honor_id=honor.id
    )
    replay = await honors.grant_commemorative_honor(
        db_session, _actor(admin), user_id=student.id, honor_id=honor.id
    )
    assert replay.id == grant.id
    assert len(await _user_honors(db_session, student.id)) == 1

    with pytest.raises(HonorNotFoundError):
        await honors.grant_commemorative_honor(
            db_session, _actor(admin), user_id=student.id, honor_id=uuid4()
        )

    # AUTO honors are evaluate_honors territory only.
    await honors.evaluate_honors(
        db_session,
        student.id,
        HonorEvaluationEvent(
            trigger=HonorTrigger.DAILY_RANK_KNOWN, rank=1, period="2026-09-21"
        ),
    )
    auto_honor = await db_session.scalar(
        select(Honor).where(Honor.is_auto.is_(True))
    )
    assert auto_honor is not None
    with pytest.raises(CommemorativeHonorRequiredError):
        await honors.grant_commemorative_honor(
            db_session, _actor(admin), user_id=student.id, honor_id=auto_honor.id
        )

    # A granted commemorative honor is displayable like any owned honor.
    await honors.set_display_honor(db_session, student.id, honor.id)
    profile = await SqlAlchemyUserDirectory().get_display_profile(
        db_session, student.id
    )
    assert profile is not None and profile.display_honor_title == "开学纪念"

    assert await _ledger_count(db_session) == before


# --- schema-level guarantees ----------------------------------------------------


async def _assert_integrity_error(db_session: AsyncSession, seed: Any) -> None:
    """Flush ``seed`` inside a savepoint and require the constraint to
    fire; the savepoint rollback keeps the test transaction usable."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(seed)
            await db_session.flush()


@pytest.mark.integration
async def test_period_coherence_check(db_session: AsyncSession) -> None:
    """DAILY_RANK without a period and TOTAL_COMPLETED with one are both
    unrepresentable (spec §18: periodic honors MUST carry a period), as
    is an unknown honor_type."""
    student = _user("2025001013")
    await _flush(db_session, student)
    await _assert_integrity_error(
        db_session,
        Honor(honor_type="DAILY_RANK", name="x", is_auto=True, period=None),
    )
    await _assert_integrity_error(
        db_session,
        Honor(
            honor_type="TOTAL_COMPLETED", name="y", is_auto=True, period="2026-09"
        ),
    )
    await _assert_integrity_error(
        db_session, Honor(honor_type="BOGUS", name="z", is_auto=True, period=None)
    )


@pytest.mark.integration
async def test_auto_definition_and_grant_uniqueness(
    db_session: AsyncSession
) -> None:
    """The partial unique indexes keep one auto row per (type, name[,
    period]) — a different period is a DIFFERENT row — and
    UNIQUE(user_id, honor_id) keeps one grant. A same-named
    COMMEMORATIVE row is NOT covered (admin names may repeat)."""
    student = _user("2025001014")
    await _flush(db_session, student)
    honor = Honor(
        honor_type="TOTAL_COMPLETED",
        name="首次完成任务",
        is_auto=True,
        period=None,
    )
    await _flush(db_session, honor)
    await _assert_integrity_error(
        db_session,
        Honor(
            honor_type="TOTAL_COMPLETED",
            name="首次完成任务",
            is_auto=True,
            period=None,
        ),
    )
    monthly = Honor(
        honor_type="MONTHLY_RANK", name="月度 Top 3", is_auto=True, period="2026-09"
    )
    await _flush(db_session, monthly)
    await _assert_integrity_error(
        db_session,
        Honor(
            honor_type="MONTHLY_RANK",
            name="月度 Top 3",
            is_auto=True,
            period="2026-09",
        ),
    )
    # Same definition, next period: a legal second row.
    await _flush(
        db_session,
        Honor(
            honor_type="MONTHLY_RANK",
            name="月度 Top 3",
            is_auto=True,
            period="2026-10",
        ),
    )
    twin = Honor(
        honor_type="COMMEMORATIVE", name="首次完成任务", is_auto=False, period=None
    )
    await _flush(db_session, twin)  # admin rows live outside the indexes

    await _flush(db_session, UserHonor(user_id=student.id, honor_id=honor.id))
    await _assert_integrity_error(
        db_session, UserHonor(user_id=student.id, honor_id=honor.id)
    )
