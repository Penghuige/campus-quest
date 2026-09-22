# backend/tests/integration/points/test_reward_reversal.py
"""Admin reversal of an assignment reward (spec §15/§17.2, §31.6; plan 05
task 5).

What these tests prove, against real PostgreSQL:

- **Append-only correction (spec §17.2):** ``reverse_assignment_reward``
  posts a NEW ``ASSIGNMENT_REWARD_REVERSAL`` row — amount -200 against
  the original +200, linked through ``reversal_of_id``, sharing the
  original's source triple ('ASSIGNMENT_CLAIM', claim_id, ...) with a
  DIFFERENT ledger_type — and the ORIGINAL row is untouched
  field-for-field (no update path exists; §15: 原始 Ledger 行不可
  UPDATE/DELETE, 修正通过新增反向流水完成).
- **Wallet overdraft is legitimate (migration 0012 controller ruling —
  user veto point at PR).** A reversal of points the student already
  spent drives ``available_points`` NEGATIVE (available 50, reverse 200
  -> -150) instead of failing or clamping: spec §17.2 mandates the
  reversal entry exist (不应自动扣用户历史积分；错误发放通过反向流水冲销),
  and the ledger==wallet invariant forbids clamping. Spec §31.12 only
  forbids REDEMPTION making spendable negative — that protection moved
  to the redemption-service gate under the wallet lock (T4), NOT to a
  wallet CHECK; 0012 drops ``ck_point_wallets_available_points``
  (earned_points stays >= 0).
- **Ranking period preservation (spec §17.2, tested as the spec
  demands):** the reversal carries ``affects_ranking=True`` and the
  ORIGINAL's ``ranking_effective_at`` (an August instant), so the
  September decision repairs August and all-time — never September.
  ``earned_points`` is NOT decremented (§15.1: ranking repair happens
  through ledger aggregation).
- **One reversal per claim (spec §31.6 shape):** the UNIQUE triple
  ('ASSIGNMENT_CLAIM', claim, 'ASSIGNMENT_REWARD_REVERSAL') makes the
  reversal one-per-claim; a second sequential request raises the typed
  ``RewardAlreadyReversedError`` (a reversal is a recorded admin
  DECISION — silently returning an existing row would hide that THIS
  request's reason was never recorded), and two CONCURRENT reversals
  yield exactly one winner + one typed error through the savepoint
  recovery path.
- **Admin-grade guards:** reason mandatory (blank is not a reason) and
  the actor must be ADMIN — spec §15 routes 人工积分调整/冲销 through the
  admin channel; a teacher's correction channel is the review-side
  INVALIDATE_REWARD_LOCK (§11.3), never a reversal. Non-ASSIGNMENT_
  REWARD targets and missing rows are typed rejections.

Harness notes (backend-engineering §7): the concurrency scenario uses
independent sessions on the shared ``db_engine`` released on one
barrier, with seeding/cleanup on their own committed sessions (the
savepoint-wrapped ``db_session`` fixture is invisible to other
connections); every other scenario runs on ``db_session`` (its
session-level commit releases only a savepoint — rows never leak).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.error_codes import ErrorCode
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType
from app.modules.points.ledger_service import (
    AUDIT_ACTION_REWARD_REVERSAL,
    LedgerEntryNotFoundError,
    LedgerEntryNotReversibleError,
    LedgerService,
    PostLedgerEntry,
    RewardAlreadyReversedError,
    RewardReversalPermissionDeniedError,
    RewardReversalReasonRequiredError,
)
from app.modules.points.models import PointsLedger, PointWallet

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

# The ORIGINAL reward's ranking attribution (an August instant) and the
# reversal decision time (September): §17.2's worked example.
_AUGUST_LOCK = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
_SEPTEMBER_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)

_REVERSAL_REASON = "查实提交作弊，冲销已发放的任务奖励"

# The untouched-original battery compares these persisted columns.
_LEDGER_FIELDS = (
    "id",
    "user_id",
    "ledger_type",
    "amount",
    "source_type",
    "source_id",
    "affects_balance",
    "affects_ranking",
    "ranking_effective_at",
    "reversal_of_id",
    "operator_id",
    "reason",
    "created_at",
)


# --- row and actor helpers -----------------------------------------------------------


def _user(prefix: str, role: Role) -> User:
    suffix = uuid4().hex[:8]
    return User(
        username=f"{prefix}{suffix}",
        password_hash=_PASSWORD_HASH,
        nickname="测试用户",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=Role(user.role))


def _snapshot(entry: PointsLedger) -> dict[str, Any]:
    """Every persisted column of a ledger row."""
    return {field: getattr(entry, field) for field in _LEDGER_FIELDS}


@dataclass(frozen=True, slots=True)
class _EnqueuedRankingUpdate:
    """One captured ranking-projection trigger (the rankings
    ``RankingUpdateDispatcher`` payload shape)."""

    user_id: UUID
    ranking_effective_at: datetime
    request_id: str | None


class _RecordingRankingDispatcher:
    """Test fake for the ranking-projection port: a sync enqueue that
    records the FULL payload — the user, the changed entry's period
    attribution, and the correlation id — so the tests can pin that a
    September-posted reversal enqueues AUGUST's effective time, never
    the decision instant (spec §17.2)."""

    def __init__(self) -> None:
        self.updates: list[_EnqueuedRankingUpdate] = []

    def enqueue_ranking_update(
        self,
        user_id: UUID,
        ranking_effective_at: datetime,
        request_id: str | None = None,
    ) -> None:
        self.updates.append(
            _EnqueuedRankingUpdate(user_id, ranking_effective_at, request_id)
        )


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


async def _grant_reward(
    service: LedgerService,
    db_session: AsyncSession,
    user_id: UUID,
    *,
    amount: int = 200,
    ranking_effective_at: datetime = _AUGUST_LOCK,
) -> PointsLedger:
    """The original ASSIGNMENT_REWARD (+200 by default, effective in
    August) — posted through the real grant path so the source triple is
    the production shape."""
    return await service.grant_assignment_reward(
        db_session,
        claim_id=uuid4(),
        user_id=user_id,
        amount=amount,
        ranking_effective_at=ranking_effective_at,
    )


async def _reverse(
    service: LedgerService,
    db_session: AsyncSession,
    actor: Actor,
    ledger_id: UUID,
    reason: Any = _REVERSAL_REASON,
    ranking_dispatcher: Any | None = None,
) -> PointsLedger:
    return await service.reverse_assignment_reward(
        db_session,
        actor,
        ledger_id,
        reason,
        ranking_dispatcher=ranking_dispatcher,
    )


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _committed_cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order; nothing rolls these rows
    back for us (the concurrency test seeds with real commits)."""
    async with factory() as session:
        for user_id in user_ids:
            await session.execute(
                delete(PointsLedger).where(PointsLedger.user_id == user_id)
            )
            await session.execute(
                delete(PointWallet).where(PointWallet.user_id == user_id)
            )
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


# --- the append-only correction ------------------------------------------------------


@pytest.mark.integration
async def test_reverse_posts_negative_entry_linked_to_untouched_original(
    db_session: AsyncSession,
) -> None:
    """The §17.2 correction: one admin reversal of an August +200 reward
    decided in September posts exactly one new -200 ASSIGNMENT_REWARD_
    REVERSAL row linked by reversal_of_id and attributed to the ORIGINAL
    period; the original row is byte-for-byte unchanged; the wallet
    available drops to 0 while earned stays 200 (ranking repair belongs
    to the ledger aggregation); the ranking-projection trigger fires on
    the CALLER's commit — never before it — carrying the affected user
    and the ORIGINAL's period attribution (the §17.2 payload)."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    original = await _grant_reward(service, db_session, student.id)
    await db_session.commit()  # the reward is paid history before the reversal
    before = _snapshot(original)

    ranking = _RecordingRankingDispatcher()
    reversal = await _reverse(
        service,
        db_session,
        _actor(admin),
        original.id,
        ranking_dispatcher=ranking,
    )
    assert ranking.updates == []  # pre-commit: the row is not visible yet

    assert reversal.user_id == student.id
    assert reversal.ledger_type == LedgerType.ASSIGNMENT_REWARD_REVERSAL
    assert reversal.amount == -200
    assert reversal.source_type == "ASSIGNMENT_CLAIM"  # the one-per-claim triple
    assert reversal.source_id == original.source_id
    assert reversal.reversal_of_id == original.id
    assert reversal.operator_id == admin.id
    assert reversal.reason == _REVERSAL_REASON
    # Cheating reversal affects BOTH projections (spec §17.2) and repairs
    # the ORIGINAL period: August's instant, not September's decision.
    assert reversal.affects_balance is True
    assert reversal.affects_ranking is True
    assert reversal.ranking_effective_at == original.ranking_effective_at
    assert reversal.ranking_effective_at == _AUGUST_LOCK
    assert reversal.ranking_effective_at != _SEPTEMBER_NOW

    await db_session.commit()
    # The post-commit seam fired ONCE with the full §17.2 payload: the
    # affected user, the ORIGINAL reward's August attribution (a
    # September decision repairs August's boards, never September's),
    # and no caller correlation id (the dispatcher generates one).
    assert ranking.updates == [
        _EnqueuedRankingUpdate(
            user_id=student.id, ranking_effective_at=_AUGUST_LOCK, request_id=None
        )
    ]

    # The ORIGINAL row is untouched: no field moved (spec §15 append-only).
    reread = await db_session.get(PointsLedger, original.id)
    assert reread is not None
    assert _snapshot(reread) == before

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student.id)
        )
    ).all()
    assert len(entries) == 2  # original + reversal, never a rewrite
    assert sum(e.amount for e in entries if e.affects_balance) == 0

    wallet = await db_session.get(PointWallet, student.id)
    assert wallet is not None
    assert wallet.available_points == 0  # 200 - 200
    assert wallet.earned_points == 200  # §15.1: earned never decrements


# --- one reversal per claim -----------------------------------------------------------


@pytest.mark.integration
async def test_second_reversal_rejected_typed(db_session: AsyncSession) -> None:
    """A claim's reward reverses at most once: the sequential replay
    raises the typed already-reversed error (a reversal is a recorded
    admin DECISION — this request's reason would never be recorded) and
    writes nothing: still two rows, wallet unchanged."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    student_id = student.id  # captured: the rollback below expires instances
    original = await _grant_reward(service, db_session, student_id)
    await _reverse(service, db_session, _actor(admin), original.id)
    await db_session.commit()

    with pytest.raises(RewardAlreadyReversedError) as exc_info:
        await _reverse(
            service, db_session, _actor(admin), original.id, reason="再次冲销尝试"
        )
    error = exc_info.value
    assert error.code == ErrorCode.VALIDATION_ERROR
    assert error.status_code == 409
    assert error.details is not None
    assert error.details.get("ledger_id") == str(original.id)
    assert error.details.get("source_id") == str(original.source_id)
    await db_session.rollback()  # the caller owns the transaction

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student_id)
        )
    ).all()
    assert len(entries) == 2  # no third row
    reversals = [
        e
        for e in entries
        if e.ledger_type == LedgerType.ASSIGNMENT_REWARD_REVERSAL.value
    ]
    assert len(reversals) == 1
    assert reversals[0].reason == _REVERSAL_REASON  # the ORIGINAL decision stands
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 0


# --- mandatory reason and the admin-grade actor guard ---------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("reason", [None, "", "   \t "])
async def test_reversal_requires_reason(
    db_session: AsyncSession, reason: str | None
) -> None:
    """Blank is not a reason (spec §15 makes 冲销 reason-carrying): the
    typed 422 fires before any lock or write."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    student_id = student.id  # captured: the rollback below expires instances
    original = await _grant_reward(service, db_session, student_id)
    await db_session.commit()

    with pytest.raises(RewardReversalReasonRequiredError) as exc_info:
        await _reverse(service, db_session, _actor(admin), original.id, reason)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert exc_info.value.status_code == 422
    assert exc_info.value.details is not None
    assert exc_info.value.details.get("field") == "reason"
    await db_session.rollback()

    reversals = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student_id,
                PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD_REVERSAL.value,
            )
        )
    ).all()
    assert reversals == []
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 200


@pytest.mark.integration
@pytest.mark.parametrize("role", [Role.TEACHER, Role.STUDENT])
async def test_reversal_requires_admin_actor(
    db_session: AsyncSession, role: Role
) -> None:
    """Reversal is admin-grade (spec §15 人工积分调整/冲销 channel): a
    teacher cannot reverse a paid reward (the teacher channel is the
    review-side INVALIDATE_REWARD_LOCK, §11.3), and a student certainly
    cannot. 403, nothing written."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    other = _user("tchr", role)
    await _flush(db_session, student, other)
    student_id = student.id  # captured: the rollback below expires instances
    original = await _grant_reward(service, db_session, student_id)
    await db_session.commit()

    with pytest.raises(RewardReversalPermissionDeniedError) as exc_info:
        await _reverse(service, db_session, _actor(other), original.id)
    assert exc_info.value.code == ErrorCode.PERMISSION_DENIED
    assert exc_info.value.status_code == 403
    await db_session.rollback()

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student_id)
        )
    ).all()
    assert len(entries) == 1  # only the original reward
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 200


# --- target gates ---------------------------------------------------------------------


@pytest.mark.integration
async def test_reversal_target_must_be_assignment_reward(
    db_session: AsyncSession,
) -> None:
    """Only an ASSIGNMENT_REWARD row reverses: a reversal row itself (no
    re-reversal), an ADMIN_ADJUSTMENT row (its own correction channel),
    and a missing id are typed rejections — nothing written."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    # AFTER the flush: building the Actor earlier snapshots user_id=None
    # (ids exist only once flushed) — latent until the §30 audit row made
    # actor_user_id NOT NULL observable (PR #2 hardening pass 4a).
    admin_actor = _actor(admin)
    student_id = student.id  # captured: the rollbacks below expire instances
    original = await _grant_reward(service, db_session, student_id)
    reversal = await _reverse(service, db_session, admin_actor, original.id)
    adjustment = await service.post_entry(
        db_session,
        PostLedgerEntry(
            user_id=student_id,
            ledger_type=LedgerType.ADMIN_ADJUSTMENT,
            amount=5,
            source_type="ADMIN_ADJUSTMENT",
            affects_balance=True,
            affects_ranking=False,
            reason="开学活动补偿",
        ),
    )
    await db_session.commit()

    for target_id, ledger_type in (
        (reversal.id, LedgerType.ASSIGNMENT_REWARD_REVERSAL.value),
        (adjustment.id, LedgerType.ADMIN_ADJUSTMENT.value),
    ):
        with pytest.raises(LedgerEntryNotReversibleError) as exc_info:
            await _reverse(service, db_session, admin_actor, target_id)
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        assert exc_info.value.details is not None
        assert exc_info.value.details.get("ledger_type") == ledger_type
        await db_session.rollback()

    with pytest.raises(LedgerEntryNotFoundError) as exc_info:
        await _reverse(service, db_session, admin_actor, uuid4())
    assert exc_info.value.code == ErrorCode.NOT_FOUND
    assert exc_info.value.status_code == 404
    await db_session.rollback()

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student_id)
        )
    ).all()
    assert len(entries) == 3  # reward + one reversal + one adjustment, no more


# --- the overdraft ruling (migration 0012) --------------------------------------------


@pytest.mark.integration
async def test_reversal_overdraft_drives_available_negative(
    db_session: AsyncSession,
) -> None:
    """Migration 0012's ruling: the student spent 150 of the 200 reward
    (available 50); reversing the full 200 posts the ledger entry anyway
    (spec §17.2: the reversal MUST exist) and the wallet follows to -150
    — never clamped, never rejected — while the ledger==wallet invariant
    holds (the projection is SUM(affects_balance)) and earned stays 200.
    Redemption cannot exploit this: spendability is gated at request time
    under the wallet lock (T4), and §31.12 only forbids REDEMPTION going
    negative."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    original = await _grant_reward(service, db_session, student.id)
    await service.post_entry(
        db_session,
        PostLedgerEntry(
            user_id=student.id,
            ledger_type=LedgerType.REWARD_REDEMPTION,
            amount=-150,
            source_type="REWARD_REDEMPTION",
            source_id=uuid4(),
            affects_balance=True,
            affects_ranking=False,
        ),
    )
    await db_session.commit()
    wallet = await db_session.get(PointWallet, student.id)
    assert wallet is not None
    assert wallet.available_points == 50  # the spent-points precondition

    reversal = await _reverse(service, db_session, _actor(admin), original.id)
    await db_session.commit()

    assert reversal.amount == -200
    wallet = await db_session.get(PointWallet, student.id)
    assert wallet is not None
    assert wallet.available_points == -150  # overdraft: 50 - 200
    assert wallet.earned_points == 200
    balance_sum = (
        await db_session.scalars(
            select(PointsLedger.amount).where(
                PointsLedger.user_id == student.id,
                PointsLedger.affects_balance,
            )
        )
    ).all()
    assert sum(balance_sum) == -150 == wallet.available_points  # rebuildable


# --- the concurrent double reversal --------------------------------------------------


@pytest.mark.integration
async def test_concurrent_double_reversal_yields_exactly_one_reversal(
    db_engine: AsyncEngine,
) -> None:
    """Two admins reverse the same reward concurrently (independent
    sessions, one barrier): the wallet FOR UPDATE lock serializes them,
    the UNIQUE triple ('ASSIGNMENT_CLAIM', claim, 'ASSIGNMENT_REWARD_
    REVERSAL') makes exactly one -200 row land, and the loser's savepoint
    recovery maps the violation to the typed already-reversed error —
    never a second entry, never a double decrement."""
    factory = _factory(db_engine)
    service = LedgerService()
    user_ids: list[UUID] = []
    original_id: UUID | None = None
    try:
        student = _user("2025s", Role.STUDENT)
        admin = _user("admr", Role.ADMIN)
        async with factory() as session:
            session.add_all((student, admin))
            await session.flush()
            user_ids.extend((student.id, admin.id))
            original = await _grant_reward(service, session, student.id)
            await session.commit()
            original_id = original.id
        assert original_id is not None

        async def reverse_once(start: asyncio.Event) -> PointsLedger | Exception:
            async with factory() as session:
                # Warm the pooled connection BEFORE the barrier: asyncpg
                # connection setup otherwise dwarfs the critical section
                # and hides the interleaving under test.
                await session.execute(text("SELECT 1"))
                await start.wait()
                try:
                    entry = await _reverse(service, session, _actor(admin), original_id)
                    await session.commit()
                    return entry
                except Exception as exc:  # typed errors return for judging
                    await session.rollback()  # release the wallet lock promptly
                    return exc

        start = asyncio.Event()
        tasks = [asyncio.create_task(reverse_once(start)) for _ in range(2)]
        await asyncio.sleep(0.05)  # let both park on the barrier
        start.set()
        first, second = await asyncio.gather(*tasks)

        outcomes = sorted(type(result).__name__ for result in (first, second))
        assert outcomes == ["PointsLedger", "RewardAlreadyReversedError"]
        winner = first if isinstance(first, PointsLedger) else second
        assert isinstance(winner, PointsLedger)
        assert winner.amount == -200

        async with factory() as check:
            reversals = (
                await check.scalars(
                    select(PointsLedger).where(
                        PointsLedger.user_id == student.id,
                        PointsLedger.ledger_type
                        == LedgerType.ASSIGNMENT_REWARD_REVERSAL.value,
                    )
                )
            ).all()
            assert len(reversals) == 1
            entries = (
                await check.scalars(
                    select(PointsLedger).where(PointsLedger.user_id == student.id)
                )
            ).all()
            assert len(entries) == 2  # original + exactly one reversal
            wallet = await check.get(PointWallet, student.id)
            assert wallet is not None
            assert wallet.available_points == 0  # decremented ONCE
            assert wallet.earned_points == 200
    finally:
        await _committed_cleanup(factory, user_ids=user_ids)


# --- the audited before is the lock-time balance (round-5 P1) ------------------------


@pytest.mark.integration
async def test_reversal_before_snapshot_reads_the_lock_time_balance(
    db_engine: AsyncEngine,
) -> None:
    """Round-5 P1: the audited ``wallet_available_points`` before is
    the balance AT LOCK TIME, never a pre-lock read. A concurrent
    balance-changing post (session B) holds the wallet lock with an
    uncommitted -60 when the reversal (session A) starts: A parks on
    the wallet FOR UPDATE, and once B commits, A's audited before is
    B's committed 140 — the pre-lock read (the old shape) snapshotted
    the stale 200 and audited a FALSE migration (200→-60) while the
    wallet really moved 140→-60 under the lock (quality-gates §16/G12).
    Deterministic interleave: B signals AFTER taking the lock, A starts
    only then, and B commits only after A has had time to park."""
    factory = _factory(db_engine)
    service = LedgerService()
    user_ids: list[UUID] = []
    original_id: UUID | None = None
    try:
        student = _user("2025s", Role.STUDENT)
        admin = _user("admr", Role.ADMIN)
        async with factory() as session:
            session.add_all((student, admin))
            await session.flush()
            user_ids.extend((student.id, admin.id))
            original = await _grant_reward(service, session, student.id)
            await session.commit()
            original_id = original.id
        assert original_id is not None

        # B: an unrelated admin adjustment (-60) that HOLDS the wallet
        # lock uncommitted while A starts its reversal.
        b_locked = asyncio.Event()
        release_b = asyncio.Event()

        async def hold_lock_then_commit() -> None:
            async with factory() as session:
                await service.post_entry(
                    session,
                    PostLedgerEntry(
                        user_id=student.id,
                        ledger_type=LedgerType.ADMIN_ADJUSTMENT,
                        amount=-60,
                        source_type="ADMIN_ADJUSTMENT",
                        affects_balance=True,
                        affects_ranking=False,
                        reason="并发穿插的余额变更",
                    ),
                )
                b_locked.set()
                await release_b.wait()
                await session.commit()

        async def reverse_under_the_held_lock() -> PointsLedger:
            async with factory() as session:
                # Warm the pooled connection BEFORE parking (the
                # double-reversal test's discipline).
                await session.execute(text("SELECT 1"))
                await b_locked.wait()
                entry = await _reverse(service, session, _actor(admin), original_id)
                await session.commit()
                return entry

        b_task = asyncio.create_task(hold_lock_then_commit())
        a_task = asyncio.create_task(reverse_under_the_held_lock())
        await b_locked.wait()
        # A now parks on the wallet FOR UPDATE — it cannot pass B's lock
        # before B's commit, whichever end of the method blocks.
        await asyncio.sleep(0.1)
        release_b.set()
        reversal = await a_task
        await b_task

        assert reversal.amount == -200
        async with factory() as check:
            wallet = await check.get(PointWallet, student.id)
            assert wallet is not None
            assert wallet.available_points == -60  # 200 - 60 - 200
            audit = await check.scalar(
                select(AuditLog).where(
                    AuditLog.action == AUDIT_ACTION_REWARD_REVERSAL,
                    AuditLog.target_id == str(original_id),
                )
            )
            assert audit is not None
            # THE round-5 assertion: before is the LOCK-TIME value B
            # committed (140), never the stale pre-lock 200; the after
            # and the committed wallet agree with the same migration.
            assert audit.before_snapshot == {"wallet_available_points": 140}
            assert audit.after_snapshot is not None
            assert audit.after_snapshot["wallet_available_points"] == -60
    finally:
        await _committed_cleanup(factory, user_ids=user_ids)


# --- the enqueue is best-effort (final-review N1) -------------------------------------


class _ExplodingRankingDispatcher:
    """A ranking-projection port whose publish always fails — the
    broker-outage shape behind final-review N1."""

    def __init__(self) -> None:
        self.calls = 0

    def enqueue_ranking_update(
        self,
        user_id: UUID,
        ranking_effective_at: datetime,
        request_id: str | None = None,
    ) -> None:
        self.calls += 1
        raise RuntimeError("injected broker outage")


@pytest.mark.integration
async def test_reversal_survives_ranking_dispatcher_failure_after_commit(
    db_session: AsyncSession,
) -> None:
    """Final-review N1 on the reversal path: the after-commit listener
    fires inside the caller's ``commit()`` with the transaction already
    durable, so a dispatcher failure must not surface out of the
    commit — the reversal row and the wallet repair stand, the typed
    answer is returned, and the missed projection heals at the next
    rebuild (spec §32: a failed enqueue is never compensated by
    failing the business write)."""
    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    student_id = student.id
    original = await _grant_reward(service, db_session, student_id)
    await db_session.commit()

    dispatcher = _ExplodingRankingDispatcher()
    reversal = await _reverse(
        service,
        db_session,
        _actor(admin),
        original.id,
        ranking_dispatcher=dispatcher,
    )
    assert dispatcher.calls == 0  # pre-commit: the listener has not fired
    # The commit — with the failing listener inside it — returns instead
    # of raising; nothing about the typed flow changed.
    await db_session.commit()
    assert dispatcher.calls == 1  # the trigger ran (and failed) exactly once
    assert reversal.amount == -200

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == student_id)
        )
    ).all()
    assert len(entries) == 2  # original + reversal: durable despite the outage
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 0  # the repair stands


# --- the durable audit row (spec §30 reward reversal; PR #2 hardening 4a) -------------


@pytest.mark.integration
async def test_reversal_writes_one_durable_audit_row(db_session: AsyncSession) -> None:
    """§30 reward reversal (use-case contract ahead of the HTTP face):
    the ACTUAL reversal lands exactly one ``REWARD_REVERSAL`` audit row
    in the same transaction — before/after carry the wallet-balance
    migration plus the reversal amount and the ORIGINAL's source triple
    (business facts only, G11) — and the rejected second reversal
    writes NO second row (the recorded decision stands)."""
    import json

    service = LedgerService()
    student = _user("2025s", Role.STUDENT)
    admin = _user("admr", Role.ADMIN)
    await _flush(db_session, student, admin)
    original = await _grant_reward(service, db_session, student.id)
    original_id = original.id  # captured: the rollback below expires instances

    reversal = await _reverse(service, db_session, _actor(admin), original_id)
    reversal_amount = reversal.amount  # captured: the rollback below expires
    await db_session.commit()

    rows = list(
        (
            await db_session.scalars(
                select(AuditLog).where(AuditLog.target_id == str(original.id))
            )
        ).all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AUDIT_ACTION_REWARD_REVERSAL
    assert row.target_type == "points_ledger"
    assert row.target_id == str(original.id)
    assert row.actor_user_id == admin.id
    assert row.actor_role == Role.ADMIN.value
    assert row.reason == _REVERSAL_REASON
    assert row.before_snapshot == {"wallet_available_points": 200}
    assert row.after_snapshot == {
        "wallet_available_points": 0,
        "reversal_amount": -200,
        "original_ledger_id": str(original.id),
        "source_type": "ASSIGNMENT_CLAIM",
        "source_id": str(original.source_id),
    }
    assert row.details == {"user_id": str(student.id)}
    # No HTTP face: the correlation pair keeps its NULL default.
    assert row.ip_address is None
    assert row.request_id is None
    assert row.created_at is not None
    # G11 negative assertion: no nickname / no username(学号-like) anywhere.
    for column in ("before_snapshot", "after_snapshot", "details"):
        blob = json.dumps(getattr(row, column) or {}, ensure_ascii=False)
        assert student.nickname not in blob
        assert student.username not in blob

    # The rejected replay (typed already-reversed) writes no second row.
    with pytest.raises(RewardAlreadyReversedError):
        await _reverse(
            service, db_session, _actor(admin), original_id, reason="再次冲销尝试"
        )
    await db_session.rollback()
    rows_after = list(
        (
            await db_session.scalars(
                select(AuditLog).where(AuditLog.target_id == str(original_id))
            )
        ).all()
    )
    assert len(rows_after) == 1
    assert reversal_amount == -200
