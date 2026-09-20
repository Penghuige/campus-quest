# backend/tests/integration/points/test_ledger_service.py
"""LedgerService posting, wallet projection, and the reward grant port
(spec §15/§15.1, §14 step 8, §31.6/§31.12; plan 05 task 2).

What these tests prove, against real PostgreSQL:

- **Idempotent grant:** ``grant_assignment_reward`` twice for one Claim
  returns the SAME ledger row (the original entry wins even when the
  replay carries a different amount), and the wallet incremented exactly
  once — the UNIQUE(source_type, source_id, ledger_type) triple is the
  idempotency mechanism (spec §31.6: 绝不能发两次积分).
- **Same-transaction projection:** when the wallet update fails after
  the ledger insert flushed (an overspending redemption trips
  ``ck_point_wallets_available_points``), the caller's rollback removes
  the ledger row too — the service never commits (backend-engineering
  §5: the use-case boundary owns the transaction) and never leaves a
  half-applied entry.
- **Concurrent first entry:** two parallel posts for a wallet-less user
  produce ONE wallet row with the summed balance (INSERT ... ON CONFLICT
  DO NOTHING + FOR UPDATE, backend-engineering §7).
- **Concurrent duplicate grant:** two parallel grants for one Claim both
  return the same row; exactly one entry and one wallet increment land.
- **Spendable math:** ``get_spendable_points`` = wallet.available_points
  - SUM(ACTIVE reservations); RELEASED reservations hold nothing; a user
  without a wallet has 0 (spec §16.2).
- **Wallet projection rules:** available += amount for every
  balance-affecting entry; earned += amount only for POSITIVE
  ranking-affecting entries (spec §15.1 cumulative task contribution —
  a reversal repairs rankings through the ledger aggregation, not by
  decrementing earned_points).
- **Admin reason CHECK (0011):** PostgreSQL rejects an ADMIN_ADJUSTMENT
  row without a reason even when the service forgets; the service's
  friendly gate fires first.
- **Frozen port adapter:** ``PointsRewardPortAdapter`` satisfies the
  interfaces.md signature, grants ``locked_points``, attributes the
  ranking period to the claim's lock time, and replays idempotently.

Harness notes (backend-engineering §7): concurrency scenarios use
independent sessions and connections released on one barrier; seeding
and cleanup for those run on their own sessions with REAL commits (the
savepoint-wrapped ``db_session`` fixture is invisible to other
connections), and every committed row is removed by explicit committed
DELETEs in ``finally`` (ledger -> reservations/redemptions -> wallet ->
claims -> assignments -> tasks -> users, the FK order). Usernames embed
a per-run token so rows leaked by an aborted run cannot collide with a
later seeding pass.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType, ReservationStatus
from app.modules.points.ledger_service import (
    InvalidLedgerEntryError,
    LedgerService,
    PointsRewardPortAdapter,
    PostLedgerEntry,
)
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)
from app.modules.submissions.review_service import GrantResult, PointsRewardPort
from app.modules.tasks.claim_service import REWARD_POLICY_SNAPSHOT_V1
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

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_LOCK_TIME = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


# --- row helpers --------------------------------------------------------------------


def _student(username: str = "20250010001") -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _teacher(username: str) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试教师",
        phone_e164=None,
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )


def _reward_item(**overrides: Any) -> RewardItem:
    fields: dict[str, Any] = {
        "name": "平时成绩 +1",
        "description": "在参与课程的平时成绩中加一分。",
        "point_cost": 200,
        "stock": 10,
        "per_user_term_limit": 1,
    }
    fields.update(overrides)
    return RewardItem(**fields)


def _redemption(user: User, item: RewardItem) -> RewardRedemption:
    return RewardRedemption(
        user_id=user.id,
        reward_item_id=item.id,
        term_key="2026-fall",
        points=item.point_cost,
    )


def _task(owner: User) -> Task:
    return Task(
        owner_teacher_id=owner.id,
        title="图书馆座位使用情况采集",
        description="采集各楼层座位占用数据。",
        task_type=TaskType.DATA_CRAWL,
        rarity=TaskRarity.NORMAL,
        base_reward_points=100,
        status=TaskStatus.PUBLISHED,
        deadline_mode=DeadlineMode.RELATIVE,
        duration_minutes=4320,
        submission_schema={"columns": [{"name": "seat", "type": "string"}]},
        submission_schema_version=2,
        allowed_file_types=["CSV"],
        max_file_size_bytes=200 * 1024 * 1024,
        notification_channels=["SMS"],
    )


def _assignment(task: Task) -> Assignment:
    return Assignment(
        task_id=task.id,
        platform="xiaohongshu",
        keyword="图书馆座位",
        availability_status=AssignmentAvailability.OCCUPIED,
    )


def _claim(assignment: Assignment, user: User) -> AssignmentClaim:
    """A claim whose PROVISIONAL reward lock carries a lock time."""
    return AssignmentClaim(
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        user_id=user.id,
        status=ClaimStatus.CLAIMED,
        claimed_at=_LOCK_TIME,
        deadline_at=_LOCK_TIME + timedelta(days=3),
        grace_deadline_at=_LOCK_TIME + timedelta(days=4),
        reward_policy_snapshot=dict(REWARD_POLICY_SNAPSHOT_V1),
        base_reward_points_snapshot=100,
        submission_schema_version=2,
        reward_lock_status=RewardLockStatus.PROVISIONAL,
        locked_reward_points=80,
        reward_locked_at=_LOCK_TIME,
    )


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    """Add and flush; parents flush before children reference their
    server-generated ids at construction time."""
    db_session.add_all(objects)
    await db_session.flush()


async def _grant(
    service: LedgerService,
    db_session: AsyncSession,
    user_id: UUID,
    *,
    claim_id: UUID | None = None,
    amount: int = 100,
) -> PointsLedger:
    return await service.grant_assignment_reward(
        db_session,
        claim_id=claim_id or uuid4(),
        user_id=user_id,
        amount=amount,
        ranking_effective_at=_LOCK_TIME,
    )


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _run_behind_barrier(
    coro_factories: list[Any],
) -> list[Any]:
    """Park every concurrent call on one barrier, then release them
    together so the transactions genuinely contend for the same rows —
    the claim-concurrency harness shape. Each factory receives the
    barrier Event; the short settle delay lets every coroutine reach it
    on its warmed pooled connection before the release."""
    start = asyncio.Event()
    tasks = [asyncio.create_task(factory(start)) for factory in coro_factories]
    await asyncio.sleep(0.05)
    start.set()
    return list(await asyncio.gather(*tasks))


async def _committed_cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: list[UUID],
    task_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order; nothing rolls these rows
    back for us (the concurrency tests seed with real commits)."""
    async with factory() as session:
        for user_id in user_ids:
            await session.execute(
                delete(PointsLedger).where(PointsLedger.user_id == user_id)
            )
            await session.execute(
                delete(PointReservation).where(PointReservation.user_id == user_id)
            )
            await session.execute(
                delete(RewardRedemption).where(RewardRedemption.user_id == user_id)
            )
            await session.execute(
                delete(PointWallet).where(PointWallet.user_id == user_id)
            )
        for task_id in task_ids:
            await session.execute(
                delete(AssignmentClaim).where(AssignmentClaim.task_id == task_id)
            )
            await session.execute(
                delete(Assignment).where(Assignment.task_id == task_id)
            )
            await session.execute(delete(Task).where(Task.id == task_id))
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


# --- idempotent grant ----------------------------------------------------------------


@pytest.mark.integration
async def test_grant_assignment_reward_twice_posts_one_entry_and_one_increment(
    db_session: AsyncSession,
) -> None:
    """The §31.6 invariant through the service: a replayed grant returns
    the ORIGINAL row (even when the replay asks for a different amount)
    and the wallet reflects exactly one increment."""
    service = LedgerService()
    student = _student()
    await _flush(db_session, student)
    claim_id = uuid4()

    first = await _grant(service, db_session, student.id, claim_id=claim_id, amount=100)
    await db_session.commit()  # the realistic replay sees committed state

    second = await _grant(service, db_session, student.id, claim_id=claim_id, amount=80)
    await db_session.commit()

    assert second.id == first.id  # the original entry wins, never a second row
    assert second.amount == 100

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student.id)
        )
    ).all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.ledger_type == LedgerType.ASSIGNMENT_REWARD
    assert entry.source_type == "ASSIGNMENT_CLAIM"
    assert entry.source_id == claim_id
    assert entry.amount == 100
    assert entry.affects_balance is True
    assert entry.affects_ranking is True
    assert entry.ranking_effective_at == _LOCK_TIME  # the claim's lock time
    assert entry.reversal_of_id is None

    wallet = await db_session.get(PointWallet, student.id)
    assert wallet is not None
    assert wallet.available_points == 100  # incremented ONCE
    assert wallet.earned_points == 100


# --- same-transaction projection ------------------------------------------------------


@pytest.mark.integration
async def test_wallet_projection_failure_rolls_back_ledger_insert(
    db_session: AsyncSession,
) -> None:
    """Backend-engineering §5/§6: the ledger insert and the wallet update
    are ONE unit. An overspending redemption (-1000 against a 100-point
    wallet) trips the wallet CHECK after the ledger row flushed; the
    caller's rollback must remove that row too — the service never
    commits and never leaves a half-applied entry."""
    service = LedgerService()
    student = _student()
    await _flush(db_session, student)
    student_id = student.id  # captured now: rollback expires the instance
    await _grant(service, db_session, student.id)  # available = 100
    await db_session.commit()

    overspending = PostLedgerEntry(
        user_id=student_id,
        ledger_type=LedgerType.REWARD_REDEMPTION,
        amount=-1000,
        source_type="REWARD_REDEMPTION",
        source_id=uuid4(),
        affects_balance=True,
        affects_ranking=False,
    )
    with pytest.raises(IntegrityError, match="ck_point_wallets_available_points"):
        await service.post_entry(db_session, overspending)
    await db_session.rollback()  # the caller owns the transaction

    amounts = (
        await db_session.scalars(
            select(PointsLedger.amount).where(PointsLedger.user_id == student_id)
        )
    ).all()
    assert amounts == [100]  # the failed post left nothing behind
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 100


# --- concurrency (independent sessions and connections) -------------------------------


@pytest.mark.integration
async def test_concurrent_first_entries_create_one_wallet_row(
    db_engine: AsyncEngine,
) -> None:
    """Two parallel posts for a wallet-less user: the upsert-safe wallet
    creation (INSERT ... ON CONFLICT DO NOTHING, then FOR UPDATE) yields
    exactly one row and the summed balance — never a duplicate-row error,
    never a lost update."""
    factory = _factory(db_engine)
    service = LedgerService()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(username=f"2025{run}001")
            session.add(student)
            await session.flush()
            user_ids.append(student.id)
            await session.commit()

        async def post_first_reward(
            start: asyncio.Event, claim_id: UUID
        ) -> PointsLedger:
            async with factory() as session:
                # Warm the pooled connection BEFORE the barrier: asyncpg
                # connection setup otherwise dwarfs the critical section
                # and hides the interleaving under test.
                await session.execute(text("SELECT 1"))
                await start.wait()
                entry = await service.grant_assignment_reward(
                    session,
                    claim_id=claim_id,
                    user_id=user_ids[0],
                    amount=60,
                    ranking_effective_at=_LOCK_TIME,
                )
                await session.commit()
                return entry

        first, second = await _run_behind_barrier(
            [lambda start: post_first_reward(start, uuid4()) for _ in range(2)]
        )

        assert first.id != second.id  # different claims: two distinct entries
        async with factory() as check:
            wallets = (
                await check.scalars(
                    select(PointWallet).where(PointWallet.user_id == user_ids[0])
                )
            ).all()
            assert len(wallets) == 1
            assert wallets[0].available_points == 120
            assert wallets[0].earned_points == 120
            entries = (
                await check.scalars(
                    select(PointsLedger).where(PointsLedger.user_id == user_ids[0])
                )
            ).all()
            assert len(entries) == 2
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, task_ids=[])


@pytest.mark.integration
async def test_concurrent_duplicate_grant_posts_reward_once(
    db_engine: AsyncEngine,
) -> None:
    """The §14 double-grant race through the service: two transactions
    grant the SAME claim concurrently; the wallet lock serializes them,
    the loser's INSERT loses to the UNIQUE triple and recovers by
    returning the winner's row. Exactly one entry, one increment."""
    factory = _factory(db_engine)
    service = LedgerService()
    run = uuid4().hex[:8]
    claim_id = uuid4()
    user_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(username=f"2025{run}001")
            session.add(student)
            await session.flush()
            user_ids.append(student.id)
            await session.commit()

        async def grant_once(start: asyncio.Event) -> PointsLedger:
            async with factory() as session:
                await session.execute(text("SELECT 1"))
                await start.wait()
                entry = await service.grant_assignment_reward(
                    session,
                    claim_id=claim_id,
                    user_id=user_ids[0],
                    amount=100,
                    ranking_effective_at=_LOCK_TIME,
                )
                await session.commit()
                return entry

        first, second = await _run_behind_barrier(
            [lambda start: grant_once(start) for _ in range(2)]
        )
        assert first.id == second.id  # both callers hold the SAME row

        async with factory() as check:
            entries = (
                await check.scalars(
                    select(PointsLedger).where(
                        PointsLedger.user_id == user_ids[0],
                        PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD,
                    )
                )
            ).all()
            assert len(entries) == 1
            wallets = (
                await check.scalars(
                    select(PointWallet).where(PointWallet.user_id == user_ids[0])
                )
            ).all()
            assert len(wallets) == 1
            assert wallets[0].available_points == 100  # incremented ONCE
            assert wallets[0].earned_points == 100
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, task_ids=[])


# --- spendable math -------------------------------------------------------------------


@pytest.mark.integration
async def test_get_spendable_points_subtracts_active_reservations_only(
    db_session: AsyncSession,
) -> None:
    """spec §16.2: spendable = available - ACTIVE reservations; a
    RELEASED reservation holds nothing; a user without a wallet has 0."""
    service = LedgerService()
    student = _student()
    other = _student(username="20250010002")
    await _flush(db_session, student, other)
    item = _reward_item()
    await _flush(db_session, item)
    active_redemption = _redemption(student, item)
    released_redemption = _redemption(student, item)
    await _flush(db_session, active_redemption, released_redemption)
    await _flush(
        db_session,
        PointReservation(
            user_id=student.id,
            redemption_id=active_redemption.id,
            points=200,
            status=ReservationStatus.ACTIVE.value,
        ),
        PointReservation(
            user_id=student.id,
            redemption_id=released_redemption.id,
            points=50,
            status=ReservationStatus.RELEASED.value,
            released_at=_NOW,
        ),
    )
    await _grant(service, db_session, student.id, amount=500)

    assert await service.get_spendable_points(db_session, student.id) == 300
    assert await service.get_spendable_points(db_session, other.id) == 0


# --- wallet projection rules through post_entry ---------------------------------------


@pytest.mark.integration
async def test_post_entry_projects_wallet_columns(
    db_session: AsyncSession,
) -> None:
    """available_points moves with EVERY balance-affecting entry (both
    signs); earned_points moves only for POSITIVE ranking-affecting
    entries (spec §15.1 cumulative task contribution — the reversal
    repairs rankings via ledger aggregation, not by decrementing
    earned_points). An ADMIN_ADJUSTMENT without an explicit source gets
    a service-generated unique source (each adjustment is its own source
    event)."""
    service = LedgerService()
    student = _student()
    await _flush(db_session, student)

    await _grant(service, db_session, student.id, amount=100)
    await service.post_entry(
        db_session,
        PostLedgerEntry(
            user_id=student.id,
            ledger_type=LedgerType.ADMIN_ADJUSTMENT,
            amount=-30,
            source_type="ADMIN_ADJUSTMENT",
            affects_balance=True,
            affects_ranking=False,
            reason="更正此前多发的积分",
        ),
    )
    # A reversal-shape entry: ranking-affecting but negative.
    await service.post_entry(
        db_session,
        PostLedgerEntry(
            user_id=student.id,
            ledger_type=LedgerType.ASSIGNMENT_REWARD_REVERSAL,
            amount=-40,
            source_type="ASSIGNMENT_CLAIM",
            source_id=uuid4(),
            affects_balance=True,
            affects_ranking=True,
            ranking_effective_at=_LOCK_TIME,
        ),
    )
    await db_session.flush()

    wallet = await db_session.get(PointWallet, student.id)
    assert wallet is not None
    assert wallet.available_points == 100 - 30 - 40  # every balance entry
    assert wallet.earned_points == 100  # positive ranking entries only

    admin_rows = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student.id,
                PointsLedger.ledger_type == LedgerType.ADMIN_ADJUSTMENT,
            )
        )
    ).all()
    assert len(admin_rows) == 1
    assert admin_rows[0].source_id is not None  # service-generated source
    assert admin_rows[0].reason == "更正此前多发的积分"


# --- friendly service gates + the 0011 database CHECK ---------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("command", "field"),
    [
        (
            PostLedgerEntry(
                user_id=None,  # replaced per-case below
                ledger_type=LedgerType.ASSIGNMENT_REWARD,
                amount=0,
                source_type="ASSIGNMENT_CLAIM",
                source_id=uuid4(),
                affects_balance=True,
                affects_ranking=True,
                ranking_effective_at=_LOCK_TIME,
            ),
            "amount",
        ),
        (
            PostLedgerEntry(
                user_id=None,
                ledger_type=LedgerType.ADMIN_ADJUSTMENT,
                amount=5,
                source_type="ADMIN_ADJUSTMENT",
                affects_balance=True,
                affects_ranking=False,
                reason=None,
            ),
            "reason",
        ),
        (
            PostLedgerEntry(
                user_id=None,
                ledger_type=LedgerType.ASSIGNMENT_REWARD,
                amount=5,
                source_type="ASSIGNMENT_CLAIM",
                source_id=uuid4(),
                affects_balance=True,
                affects_ranking=True,
                ranking_effective_at=None,
            ),
            "ranking_effective_at",
        ),
        # The T2 review fold: a non-ADMIN_ADJUSTMENT entry without its
        # source object would silently mint a random UUID, breaking the
        # (source_type, source_id, ledger_type) idempotency mechanism
        # (spec §31.6) — both redemption-shaped and assignment-shaped
        # types must fail the friendly gate instead.
        (
            PostLedgerEntry(
                user_id=None,
                ledger_type=LedgerType.REWARD_REDEMPTION,
                amount=-200,
                source_type="REWARD_REDEMPTION",
                affects_balance=True,
                affects_ranking=False,
            ),
            "source_id",
        ),
        (
            PostLedgerEntry(
                user_id=None,
                ledger_type=LedgerType.ASSIGNMENT_REWARD,
                amount=100,
                source_type="ASSIGNMENT_CLAIM",
                affects_balance=True,
                affects_ranking=True,
                ranking_effective_at=_LOCK_TIME,
            ),
            "source_id",
        ),
    ],
)
async def test_post_entry_friendly_gates_reject_invalid_commands(
    db_session: AsyncSession,
    command: PostLedgerEntry,
    field: str,
) -> None:
    """The service raises its typed VALIDATION_ERROR ahead of the
    database (backend-engineering §6: friendly errors first) — zero
    amounts, reason-less admin adjustments, ranking rows without a
    period attribution, and source-less non-admin entries never reach
    PostgreSQL."""
    service = LedgerService()
    student = _student()
    await _flush(db_session, student)
    broken = replace(command, user_id=student.id)
    with pytest.raises(InvalidLedgerEntryError) as exc_info:
        await service.post_entry(db_session, broken)
    assert exc_info.value.details is not None
    assert exc_info.value.details.get("field") == field
    assert exc_info.value.status_code == 422


@pytest.mark.integration
async def test_admin_adjustment_without_reason_rejected_at_database(
    db_session: AsyncSession,
) -> None:
    """Migration 0011's CHECK holds even when the service forgets: an
    ADMIN_ADJUSTMENT row without a reason is unrepresentable (spec §15:
    Admin 调整必须有 reason)."""
    student = _student()
    await _flush(db_session, student)
    db_session.add(
        PointsLedger(
            user_id=student.id,
            ledger_type=LedgerType.ADMIN_ADJUSTMENT,
            amount=5,
            source_type="ADMIN_ADJUSTMENT",
            source_id=uuid4(),
            affects_balance=True,
            affects_ranking=False,
        )
    )
    with pytest.raises(IntegrityError, match="ck_points_ledger_admin_reason_required"):
        await db_session.flush()
    await db_session.rollback()


# --- the frozen port adapter ----------------------------------------------------------


@pytest.mark.integration
async def test_points_reward_port_adapter_matches_frozen_contract(
    db_session: AsyncSession,
) -> None:
    """``PointsRewardPortAdapter`` implements the interfaces.md frozen
    signature over LedgerService: grants ``locked_points`` (the
    fraction-adjusted basis), attributes the ranking period to the
    claim's lock time, ignores (never parses) ``idempotency_key`` — the
    UNIQUE source triple is the mechanism — and replays idempotently."""
    teacher = _teacher(username=f"t{uuid4().hex[:8]}")
    student = _student(username=f"2025{uuid4().hex[:8]}")
    await _flush(db_session, teacher, student)
    task = _task(teacher)
    await _flush(db_session, task)
    assignment = _assignment(task)
    await _flush(db_session, assignment)
    claim = _claim(assignment, student)
    await _flush(db_session, claim)

    port = PointsRewardPortAdapter(ledger=LedgerService(), db=db_session)
    assert isinstance(port, PointsRewardPort)  # runtime-checkable conformance

    result = await port.grant_assignment_reward(
        user_id=student.id,
        claim_id=claim.id,
        base_points=100,
        locked_points=80,
        idempotency_key=f"assignment_reward:{claim.id}",
    )
    assert result == GrantResult(
        user_id=student.id, claim_id=claim.id, points_granted=80
    )

    replay = await port.grant_assignment_reward(
        user_id=student.id,
        claim_id=claim.id,
        base_points=100,
        locked_points=80,
        idempotency_key=f"assignment_reward:{claim.id}",
    )
    assert replay == result

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student.id)
        )
    ).all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.ledger_type == LedgerType.ASSIGNMENT_REWARD
    assert entry.source_type == "ASSIGNMENT_CLAIM"
    assert entry.source_id == claim.id
    assert entry.amount == 80
    assert entry.ranking_effective_at == claim.reward_locked_at  # the lock time
    wallet = await db_session.get(PointWallet, student.id)
    assert wallet is not None
    assert wallet.available_points == 80
    assert wallet.earned_points == 80
