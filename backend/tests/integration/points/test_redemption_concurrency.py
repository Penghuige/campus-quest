# backend/tests/integration/points/test_redemption_concurrency.py
"""RedemptionService: the double-spend / oversell concurrency core
(spec §16/§16.1/§16.2/§16.3; plan 05 task 4).

What these tests prove, against real PostgreSQL:

- **Double-spend (spec §16.3):** a user with 1500 points firing two
  concurrent 1000-point requests (two items, and the same item twice)
  ends with AT MOST one ACTIVE reservation; the loser gets the typed
  ``INSUFFICIENT_POINTS``; available_points is never negative and
  spendable = available - ACTIVE reservations never dips below zero.
  The wallet row lock (taken FIRST, before the item lock) serializes
  same-user requests.
- **Last stock (spec §16.1 库存语义):** stock=1 with two users racing
  yields exactly one winner (``REWARD_OUT_OF_STOCK`` for the loser) —
  occupancy is the derived count of REQUESTED/UNDER_REVIEW/APPROVED/
  FULFILLED redemptions computed under the RewardItem row lock.
- **Half-open window (spec §16.1):** available_from <= now <
  available_until — a request at the exact start instant succeeds, one
  at the exact end instant fails.
- **Per-term limit by snapshot (spec §16.1):** the limit counts
  REQUESTED/UNDER_REVIEW/APPROVED/FULFILLED rows under
  (user, item, snapshotted term_key); REJECTED frees the quota;
  switching the provider's term gives an independent quota and never
  rewrites historical snapshots. An empty/oversized term key fails
  with the typed configuration error — never a calendar fallback.
- **Approve (spec §16.2/§17.1):** releases the reservation
  (CONSUMED), posts ONE negative REWARD_REDEMPTION ledger entry
  (affects_balance=true, affects_ranking=false, source triple over the
  redemption id), decrements the wallet, KEEPS the stock occupied, and
  replays idempotently.
- **Reject (spec §16.2):** mandatory reason, reservation RELEASED,
  stock freed by derivation, wallet untouched, and NO consumption
  ledger entry.
- **Fulfill (spec §16.2):** APPROVED -> FULFILLED records
  fulfilled_at/note; a replay is a no-op; fulfilling an unapproved
  request is a typed rejection.
- **Staff guard:** only TEACHER/ADMIN may decide or fulfill — V1's
  stand-in for spec §16.1's Admin-授权-Teacher review channel (Plan 08
  owns the authorization model; RewardItem has no owner to check).

Harness notes (backend-engineering §7, same shape as the ledger
suite): concurrency scenarios use independent sessions released on one
barrier; seeding and cleanup run on their own sessions with REAL
commits, and committed rows are removed by explicit committed DELETEs
in ``finally`` (ledger -> reservations/redemptions -> wallet ->
reward items -> users, the FK order). Usernames embed a per-run token.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import Clock, FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType, ReservationStatus
from app.modules.points.ledger_service import LedgerService
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)
from app.modules.points.redemption_service import (
    AcademicTermConfigurationError,
    AcademicTermProvider,
    InsufficientPointsError,
    RedemptionLimitReachedError,
    RedemptionNotFulfillableError,
    RedemptionPermissionDeniedError,
    RedemptionRejectReasonRequiredError,
    RedemptionService,
    RedemptionWindowClosedError,
    RewardItemDisabledError,
    RewardOutOfStockError,
    StaticAcademicTermProvider,
)

# Direct-insert password stub (argon2 hash of an unguessable test secret);
# the registration service is deliberately not exercised here.
_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
_TERM = "2026-fall"


# --- row helpers --------------------------------------------------------------------


def _student(username: str) -> User:
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


def _admin(username: str) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试管理员",
        phone_e164=None,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )


def _reward_item(**overrides: Any) -> RewardItem:
    """Neutral defaults: unlimited stock, no per-term limit, no window —
    each test narrows exactly the dimension it exercises."""
    fields: dict[str, Any] = {
        "name": "平时成绩 +1",
        "description": "在参与课程的平时成绩中加一分。",
        "point_cost": 200,
        "stock": None,
        "per_user_term_limit": None,
    }
    fields.update(overrides)
    return RewardItem(**fields)


def _service(
    term: str = _TERM,
    *,
    clock: Clock | None = None,
    terms: AcademicTermProvider | None = None,
) -> RedemptionService:
    return RedemptionService(
        clock=clock or FrozenClock(_NOW),
        terms=terms or StaticAcademicTermProvider(term),
    )


def _actor(user_id: UUID, role: Role) -> Actor:
    """Built from captured ids: service rollbacks expire ORM instances,
    so actor construction must not read them lazily."""
    return Actor(user_id=user_id, role=role)


async def _fund(db: AsyncSession, user_id: UUID, amount: int) -> None:
    """Grant wallet balance through the real ledger path: one
    ASSIGNMENT_REWARD per fresh (unFK'd) claim id — the grant is the
    only sanctioned way a wallet ever grows."""
    await LedgerService().grant_assignment_reward(
        db,
        claim_id=uuid4(),
        user_id=user_id,
        amount=amount,
        ranking_effective_at=_NOW,
    )


async def _flush(db: AsyncSession, *objects: Any) -> None:
    db.add_all(objects)
    await db.flush()


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


class _BrokenTermProvider:
    """A provider that returns an unusable key — the shape Plan 08's
    setting adapter would have before the admin configures it."""

    def __init__(self, key: str) -> None:
        self._key = key

    def current_term_key(self) -> str:
        return self._key


async def _run_behind_barrier(coro_factories: list[Any]) -> list[Any]:
    """Park every concurrent call on one barrier, then release them
    together so the transactions genuinely contend for the same rows."""
    start = asyncio.Event()
    tasks = [asyncio.create_task(factory(start)) for factory in coro_factories]
    await asyncio.sleep(0.05)
    start.set()
    return list(await asyncio.gather(*tasks))


async def _committed_cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: list[UUID],
    item_ids: list[UUID],
) -> None:
    """Explicit committed cleanup in FK order; the concurrency tests
    seed with real commits, so nothing rolls these rows back for us."""
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
        for item_id in item_ids:
            await session.execute(
                delete(RewardRedemption).where(
                    RewardRedemption.reward_item_id == item_id
                )
            )
        if item_ids:
            await session.execute(delete(RewardItem).where(RewardItem.id.in_(item_ids)))
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


async def _request_on_barrier(
    factory: async_sessionmaker[AsyncSession],
    service: RedemptionService,
    user_id: UUID,
    reward_item_id: UUID,
    start: asyncio.Event,
) -> Any:
    async with factory() as session:
        await session.execute(text("SELECT 1"))
        await start.wait()
        try:
            return await service.request_redemption(session, user_id, reward_item_id)
        except BusinessError as exc:
            return exc


def _split(results: list[Any]) -> tuple[list[Any], list[BusinessError]]:
    wins = [result for result in results if not isinstance(result, BusinessError)]
    errors = [result for result in results if isinstance(result, BusinessError)]
    return wins, errors


# --- spec §16.3: the double-spend race -----------------------------------------------


@pytest.mark.integration
async def test_concurrent_double_spend_freezes_at_most_one_reservation(
    db_engine: AsyncEngine,
) -> None:
    """The §16.3 scenario: 1500 points, two concurrent 1000-point
    requests (two different items). Exactly one reservation freezes;
    the loser gets INSUFFICIENT_POINTS; the wallet row is never negative
    and spendable never dips below zero."""
    factory = _factory(db_engine)
    service = _service()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    item_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(f"2025{run}001")
            item_a = _reward_item(name="成绩 +1（甲）", point_cost=1000)
            item_b = _reward_item(name="成绩 +1（乙）", point_cost=1000)
            session.add_all([student, item_a, item_b])
            await session.flush()
            user_ids.append(student.id)
            item_ids.extend([item_a.id, item_b.id])
            await _fund(session, student.id, 1500)
            await session.commit()

        results = await _run_behind_barrier(
            [
                lambda start: _request_on_barrier(
                    factory, service, user_ids[0], item_ids[0], start
                ),
                lambda start: _request_on_barrier(
                    factory, service, user_ids[0], item_ids[1], start
                ),
            ]
        )
        wins, errors = _split(results)
        assert len(wins) == 1
        assert len(errors) == 1
        assert errors[0].code == ErrorCode.INSUFFICIENT_POINTS

        async with factory() as check:
            redemptions = (
                await check.scalars(
                    select(RewardRedemption).where(
                        RewardRedemption.user_id == user_ids[0]
                    )
                )
            ).all()
            assert len(redemptions) == 1
            reservations = (
                await check.scalars(
                    select(PointReservation).where(
                        PointReservation.user_id == user_ids[0]
                    )
                )
            ).all()
            assert len(reservations) == 1  # at most one active freeze
            assert reservations[0].status == ReservationStatus.ACTIVE.value
            assert reservations[0].points == 1000
            wallet = await check.get(PointWallet, user_ids[0])
            assert wallet is not None
            assert wallet.available_points == 1500  # the freeze touches no wallet
            assert wallet.available_points >= 0  # never -500 (spec §16.3)
            spendable = await LedgerService().get_spendable_points(check, user_ids[0])
            assert spendable == 500
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, item_ids=item_ids)


@pytest.mark.integration
async def test_concurrent_same_user_same_item_requests_serialize(
    db_engine: AsyncEngine,
) -> None:
    """The wallet-row lock serializes same-user requests even for the
    SAME item: two concurrent 1000-point requests against a 1500-point
    wallet leave one redemption, one ACTIVE reservation, and one typed
    INSUFFICIENT_POINTS — never two freezes (which would overspend the
    moment both were approved)."""
    factory = _factory(db_engine)
    service = _service()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    item_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(f"2025{run}001")
            item = _reward_item(point_cost=1000)
            session.add_all([student, item])
            await session.flush()
            user_ids.append(student.id)
            item_ids.append(item.id)
            await _fund(session, student.id, 1500)
            await session.commit()

        results = await _run_behind_barrier(
            [
                lambda start: _request_on_barrier(
                    factory, service, user_ids[0], item_ids[0], start
                )
                for _ in range(2)
            ]
        )
        wins, errors = _split(results)
        assert len(wins) == 1
        assert len(errors) == 1
        assert errors[0].code == ErrorCode.INSUFFICIENT_POINTS

        async with factory() as check:
            redemptions = (
                await check.scalars(
                    select(RewardRedemption).where(
                        RewardRedemption.user_id == user_ids[0],
                        RewardRedemption.reward_item_id == item_ids[0],
                    )
                )
            ).all()
            assert len(redemptions) == 1
            reservations = (
                await check.scalars(
                    select(PointReservation).where(
                        PointReservation.user_id == user_ids[0]
                    )
                )
            ).all()
            assert len(reservations) == 1
            assert reservations[0].status == ReservationStatus.ACTIVE.value
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, item_ids=item_ids)


# --- spec §16.1/§16.3: the last-stock race -------------------------------------------


@pytest.mark.integration
async def test_concurrent_last_stock_yields_exactly_one_winner(
    db_engine: AsyncEngine,
) -> None:
    """stock=1, two funded users race: exactly one redemption occupies
    the unit (derived occupancy == 1), the loser gets
    REWARD_OUT_OF_STOCK — never an oversell."""
    factory = _factory(db_engine)
    service = _service()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    item_ids: list[UUID] = []
    try:
        async with factory() as session:
            first = _student(f"2025{run}001")
            second = _student(f"2025{run}002")
            item = _reward_item(point_cost=500, stock=1)
            session.add_all([first, second, item])
            await session.flush()
            user_ids.extend([first.id, second.id])
            item_ids.append(item.id)
            await _fund(session, first.id, 1000)
            await _fund(session, second.id, 1000)
            await session.commit()

        results = await _run_behind_barrier(
            [
                lambda start: _request_on_barrier(
                    factory, service, user_ids[0], item_ids[0], start
                ),
                lambda start: _request_on_barrier(
                    factory, service, user_ids[1], item_ids[0], start
                ),
            ]
        )
        wins, errors = _split(results)
        assert len(wins) == 1
        assert len(errors) == 1
        assert errors[0].code == ErrorCode.REWARD_OUT_OF_STOCK

        async with factory() as check:
            occupying = (
                await check.scalars(
                    select(RewardRedemption).where(
                        RewardRedemption.reward_item_id == item_ids[0],
                        RewardRedemption.status.in_(
                            ("REQUESTED", "UNDER_REVIEW", "APPROVED", "FULFILLED")
                        ),
                    )
                )
            ).all()
            assert len(occupying) == 1  # exactly one unit taken
            assert occupying[0].status == "REQUESTED"
            reservations = (
                await check.scalars(
                    select(PointReservation).where(
                        PointReservation.redemption_id == occupying[0].id
                    )
                )
            ).all()
            assert len(reservations) == 1
            assert reservations[0].status == ReservationStatus.ACTIVE.value
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, item_ids=item_ids)


# --- spec §16.1: the half-open availability window -----------------------------------


@pytest.mark.integration
async def test_window_exact_start_accepted_exact_end_rejected(
    db_session: AsyncSession,
) -> None:
    """available_from <= now < available_until: the exact start instant
    is inside the window, the exact end instant is outside."""
    service = _service()  # FrozenClock at _NOW
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    opening = _reward_item(
        name="整点开抢",
        point_cost=100,
        available_from=_NOW,
        available_until=_NOW + timedelta(hours=1),
    )
    closing = _reward_item(
        name="整点结束",
        point_cost=100,
        available_from=_NOW - timedelta(hours=1),
        available_until=_NOW,
    )
    await _flush(db_session, student, opening, closing)
    student_id, closing_id = student.id, closing.id  # rollbacks expire instances
    await _fund(db_session, student_id, 500)
    await db_session.commit()

    accepted = await service.request_redemption(db_session, student_id, opening.id)
    assert accepted.status == "REQUESTED"

    with pytest.raises(RedemptionWindowClosedError) as exc_info:
        await service.request_redemption(db_session, student_id, closing_id)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    rejected_rows = (
        await db_session.scalars(
            select(RewardRedemption).where(
                RewardRedemption.reward_item_id == closing_id
            )
        )
    ).all()
    assert rejected_rows == []


# --- spec §16.1: the exact-sufficiency boundary ---------------------------------------


@pytest.mark.integration
async def test_wallet_funded_exactly_the_cost_redeems(
    db_session: AsyncSession,
) -> None:
    """Final-review minor (the ``spendable < point_cost`` boundary): a
    wallet funded EXACTLY the cost succeeds — 200 spendable against a
    200-cost item freezes cleanly and leaves spendable 0; only a further
    request is the typed INSUFFICIENT_POINTS."""
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    exact = _reward_item(name="正好 200 分", point_cost=200)
    again = _reward_item(name="又一个 200 分", point_cost=200)
    await _flush(db_session, student, exact, again)
    student_id, exact_id, again_id = student.id, exact.id, again.id
    await _fund(db_session, student_id, 200)
    await db_session.commit()
    service = _service()

    redemption = await service.request_redemption(db_session, student_id, exact_id)
    assert redemption.status == "REQUESTED"
    assert redemption.points == 200
    assert await LedgerService().get_spendable_points(db_session, student_id) == 0
    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 200  # the freeze holds, the wallet holds

    with pytest.raises(InsufficientPointsError) as exc_info:
        await service.request_redemption(db_session, student_id, again_id)
    assert exc_info.value.code == ErrorCode.INSUFFICIENT_POINTS
    assert exc_info.value.details == {"required": 200, "spendable": 0}


# --- spec §16.1: the per-user term limit ---------------------------------------------


@pytest.mark.integration
async def test_term_limit_counts_snapshot_releases_on_reject_and_switches(
    db_session: AsyncSession,
) -> None:
    """The per-term quota counts occupying statuses under the
    snapshotted (user, item, term_key); REJECTED frees it; a provider
    term switch opens an independent quota without rewriting history."""
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    admin = _admin(f"a{run}001")
    item = _reward_item(point_cost=100, per_user_term_limit=1)
    await _flush(db_session, student, admin, item)
    student_id, admin_id, item_id = student.id, admin.id, item.id
    await _fund(db_session, student_id, 1000)
    await db_session.commit()
    fall = _service("2026-fall")
    staff_actor = _actor(admin_id, Role.ADMIN)

    first = await fall.request_redemption(db_session, student_id, item_id)
    first_id = first.id  # the limit-gate rollback below expires instances
    assert first.term_key == "2026-fall"  # the snapshot

    with pytest.raises(RedemptionLimitReachedError) as exc_info:
        await fall.request_redemption(db_session, student_id, item_id)
    assert exc_info.value.code == ErrorCode.REDEMPTION_LIMIT_REACHED

    # REJECTED releases the quota for the same term.
    rejected = await fall.reject_redemption(
        db_session, staff_actor, first_id, "不符合兑换条件"
    )
    assert rejected.status == "REJECTED"
    third = await fall.request_redemption(db_session, student_id, item_id)
    assert third.term_key == "2026-fall"

    # A switched term is an independent quota (the ACTIVE fall request
    # still holds one), and historical snapshots never move.
    spring = _service("2027-spring")
    fourth = await spring.request_redemption(db_session, student_id, item_id)
    assert fourth.term_key == "2027-spring"

    historical = (
        await db_session.scalars(
            select(RewardRedemption)
            .where(RewardRedemption.id == first_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    assert len(historical) == 1
    assert historical[0].term_key == "2026-fall"


@pytest.mark.integration
async def test_invalid_term_key_fails_loudly_never_calendar_fallback(
    db_session: AsyncSession,
) -> None:
    """An unusable CURRENT_ACADEMIC_TERM is a configuration failure:
    the static provider rejects it at construction, and a provider that
    returns it anyway fails the request with the typed error — no
    calendar-date guessing, and nothing is written."""
    run = uuid4().hex[:8]
    with pytest.raises(AcademicTermConfigurationError):
        StaticAcademicTermProvider("   ")
    with pytest.raises(AcademicTermConfigurationError):
        StaticAcademicTermProvider("x" * 65)

    student = _student(f"2025{run}001")
    item = _reward_item(point_cost=100)
    await _flush(db_session, student, item)
    await _fund(db_session, student.id, 500)
    await db_session.commit()
    service = _service(terms=_BrokenTermProvider(""))

    with pytest.raises(AcademicTermConfigurationError):
        await service.request_redemption(db_session, student.id, item.id)
    rows = (
        await db_session.scalars(
            select(RewardRedemption).where(RewardRedemption.user_id == student.id)
        )
    ).all()
    assert rows == []


# --- spec §16: the enabled gate -------------------------------------------------------


@pytest.mark.integration
async def test_disabled_item_is_not_redeemable(db_session: AsyncSession) -> None:
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    item = _reward_item(enabled=False)
    await _flush(db_session, student, item)
    student_id, item_id = student.id, item.id
    await _fund(db_session, student_id, 500)
    await db_session.commit()

    with pytest.raises(RewardItemDisabledError) as exc_info:
        await _service().request_redemption(db_session, student_id, item_id)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    rows = (
        await db_session.scalars(
            select(RewardRedemption).where(RewardRedemption.reward_item_id == item_id)
        )
    ).all()
    assert rows == []


# --- spec §16.2: approve --------------------------------------------------------------


@pytest.mark.integration
async def test_approve_consumes_reservation_posts_entry_and_holds_stock(
    db_session: AsyncSession,
) -> None:
    """Approve: reservation CONSUMED, ONE negative REWARD_REDEMPTION
    entry (balance-affecting, ranking-neutral, source triple over the
    redemption id), wallet decremented, stock still occupied, replay
    idempotent."""
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    admin = _admin(f"a{run}001")
    other = _student(f"2025{run}002")
    item = _reward_item(point_cost=200, stock=1)
    await _flush(db_session, student, admin, other, item)
    student_id, admin_id, other_id, item_id = (
        student.id,
        admin.id,
        other.id,
        item.id,
    )
    await _fund(db_session, student_id, 500)
    await db_session.commit()
    service = _service()
    actor = _actor(admin_id, Role.ADMIN)

    redemption = await service.request_redemption(db_session, student_id, item_id)
    redemption_id = redemption.id  # rollbacks expire instances
    approved = await service.approve_redemption(db_session, actor, redemption_id)

    assert approved.status == "APPROVED"
    assert approved.decided_by == admin_id
    assert approved.decided_at == _NOW

    entries = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student_id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION,
            )
        )
    ).all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.amount == -200
    assert entry.source_type == "REWARD_REDEMPTION"
    assert entry.source_id == redemption_id
    assert entry.affects_balance is True
    assert entry.affects_ranking is False  # spec §17.1: spending never ranks
    assert entry.ranking_effective_at is None
    assert entry.operator_id == admin_id

    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 300  # 500 - 200
    assert wallet.earned_points == 500  # spending never touches earned

    reservation = (
        await db_session.scalars(
            select(PointReservation).where(
                PointReservation.redemption_id == redemption_id
            )
        )
    ).one()
    assert reservation.status == ReservationStatus.CONSUMED.value
    assert reservation.released_at is not None

    # Stock stays occupied: the second user cannot take the last unit.
    await _fund(db_session, other_id, 500)
    await db_session.commit()
    with pytest.raises(RewardOutOfStockError):
        await service.request_redemption(db_session, other_id, item_id)

    # Idempotent replay: same row, no second entry, no second decrement.
    replay = await service.approve_redemption(db_session, actor, redemption_id)
    assert replay.id == redemption_id
    replay_entries = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student_id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION,
            )
        )
    ).all()
    assert len(replay_entries) == 1
    replay_wallet = await db_session.get(PointWallet, student_id)
    assert replay_wallet is not None
    assert replay_wallet.available_points == 300


# --- spec §16.2: the concurrent double approve ----------------------------------------


@pytest.mark.integration
async def test_concurrent_double_approve_posts_one_consumption_entry(
    db_engine: AsyncEngine,
) -> None:
    """Final-review minor (spec §16.2/§31.6): two staff approve the same
    REQUESTED redemption concurrently (independent sessions, one
    barrier). The redemption row lock serializes them — the winner
    posts the ONE negative REWARD_REDEMPTION entry and flips the
    reservation CONSUMED and the status APPROVED; the loser re-reads the
    APPROVED row under the lock and returns it untouched (the idempotent
    replay). Never a second entry, never a double decrement."""
    factory = _factory(db_engine)
    service = _service()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    item_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(f"2025{run}001")
            admin = _admin(f"a{run}001")
            item = _reward_item(point_cost=200)
            session.add_all([student, admin, item])
            await session.flush()
            user_ids.extend([student.id, admin.id])
            item_ids.append(item.id)
            await _fund(session, student.id, 500)
            await session.commit()
            redemption = await service.request_redemption(session, student.id, item.id)
            redemption_id = redemption.id

        actor = _actor(user_ids[1], Role.ADMIN)

        async def approve_once(start: asyncio.Event) -> Any:
            async with factory() as session:
                await session.execute(text("SELECT 1"))  # warm the connection
                await start.wait()
                return await service.approve_redemption(session, actor, redemption_id)

        results = await _run_behind_barrier(
            [lambda start: approve_once(start), lambda start: approve_once(start)]
        )
        # Both callers answer with the SAME approved row: one writer, one
        # idempotent replay — no typed error, no exception.
        assert all(
            isinstance(result, RewardRedemption) and result.status == "APPROVED"
            for result in results
        )
        assert {result.id for result in results} == {redemption_id}

        async with factory() as check:
            entries = (
                await check.scalars(
                    select(PointsLedger).where(
                        PointsLedger.user_id == user_ids[0],
                        PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION,
                    )
                )
            ).all()
            assert len(entries) == 1  # exactly one consumption entry
            assert entries[0].amount == -200
            assert entries[0].source_id == redemption_id
            reservation = (
                await check.scalars(
                    select(PointReservation).where(
                        PointReservation.redemption_id == redemption_id
                    )
                )
            ).one()
            assert reservation.status == ReservationStatus.CONSUMED.value
            wallet = await check.get(PointWallet, user_ids[0])
            assert wallet is not None
            assert wallet.available_points == 300  # 500 - 200, decremented ONCE
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, item_ids=item_ids)


# --- spec §16.2: reject ---------------------------------------------------------------


@pytest.mark.integration
async def test_reject_releases_points_and_stock_without_consumption_entry(
    db_session: AsyncSession,
) -> None:
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    admin = _admin(f"a{run}001")
    item = _reward_item(point_cost=200, stock=1)
    await _flush(db_session, student, admin, item)
    student_id, admin_id, item_id = student.id, admin.id, item.id
    await _fund(db_session, student_id, 500)
    await db_session.commit()
    service = _service()
    actor = _actor(admin_id, Role.ADMIN)

    redemption = await service.request_redemption(db_session, student_id, item_id)

    # The reason is mandatory (spec §16.1 review context; blank is not a
    # reason) and is checked before anything is locked or written.
    with pytest.raises(RedemptionRejectReasonRequiredError):
        await service.reject_redemption(db_session, actor, redemption.id, "   ")

    rejected = await service.reject_redemption(
        db_session, actor, redemption.id, "库存渠道异常"
    )
    assert rejected.status == "REJECTED"
    assert rejected.decided_by == admin_id
    assert rejected.decided_at == _NOW

    reservation = (
        await db_session.scalars(
            select(PointReservation).where(
                PointReservation.redemption_id == redemption.id
            )
        )
    ).one()
    assert reservation.status == ReservationStatus.RELEASED.value
    assert reservation.released_at is not None

    consumption = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student_id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION,
            )
        )
    ).all()
    assert consumption == []  # spec §16.2: 拒绝不产生消费负流水

    wallet = await db_session.get(PointWallet, student_id)
    assert wallet is not None
    assert wallet.available_points == 500  # untouched by request and reject
    assert await LedgerService().get_spendable_points(db_session, student_id) == 500

    # Stock freed by derivation: the released unit is immediately
    # redeemable again.
    again = await service.request_redemption(db_session, student_id, item_id)
    assert again.status == "REQUESTED"


# --- spec §16.2: fulfill --------------------------------------------------------------


@pytest.mark.integration
async def test_fulfill_is_idempotent_and_records_metadata(
    db_session: AsyncSession,
) -> None:
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    admin = _admin(f"a{run}001")
    item = _reward_item(point_cost=200)
    await _flush(db_session, student, admin, item)
    student_id, admin_id, item_id = (
        student.id,
        admin.id,
        item.id,
    )
    await _fund(db_session, student_id, 500)
    await db_session.commit()
    service = _service()

    redemption = await service.request_redemption(db_session, student_id, item_id)
    await service.approve_redemption(
        db_session, _actor(admin_id, Role.ADMIN), redemption.id
    )

    admin_actor = _actor(admin_id, Role.ADMIN)
    fulfilled = await service.fulfill_redemption(
        db_session, admin_actor, redemption.id, note="平时分已录入"
    )
    assert fulfilled.status == "FULFILLED"
    assert fulfilled.fulfilled_at == _NOW
    assert fulfilled.fulfillment_note == "平时分已录入"
    first_fulfilled_at = fulfilled.fulfilled_at

    # Replay is a no-op: original metadata survives a second click.
    replay = await service.fulfill_redemption(
        db_session, admin_actor, redemption.id, note="重复点击"
    )
    assert replay.status == "FULFILLED"
    assert replay.fulfilled_at == first_fulfilled_at
    assert replay.fulfillment_note == "平时分已录入"

    # Only APPROVED redemptions can be fulfilled.
    fresh = await service.request_redemption(db_session, student_id, item_id)
    with pytest.raises(RedemptionNotFulfillableError):
        await service.fulfill_redemption(db_session, admin_actor, fresh.id)


# --- the review guard (Admin-only until scoped delegation) ----------------------------


@pytest.mark.integration
async def test_review_guard_rejects_student_and_teacher_actors(
    db_session: AsyncSession,
) -> None:
    """Spec §16.1 routes review through the Admin-授权-Teacher channel;
    the PR #2 hardening ruling narrows the interim guard to ADMIN only
    (any ACTIVE+TOTP teacher deciding ANY redemption was P0-4 — RewardItem
    has no owner to scope by, and Plan 08 owns the delegation model). A
    student AND a teacher actor get PERMISSION_DENIED and nothing
    changes."""
    run = uuid4().hex[:8]
    student = _student(f"2025{run}001")
    teacher = _teacher(f"t{run}001")
    item = _reward_item(point_cost=200)
    await _flush(db_session, student, teacher, item)
    student_id, item_id = student.id, item.id
    await _fund(db_session, student_id, 500)
    await db_session.commit()
    service = _service()
    redemption = await service.request_redemption(db_session, student_id, item_id)
    student_actor = _actor(student_id, Role.STUDENT)
    teacher_actor = _actor(teacher.id, Role.TEACHER)

    with pytest.raises(RedemptionPermissionDeniedError) as approve_denied:
        await service.approve_redemption(db_session, student_actor, redemption.id)
    with pytest.raises(RedemptionPermissionDeniedError):
        await service.reject_redemption(
            db_session, student_actor, redemption.id, "越权"
        )
    with pytest.raises(RedemptionPermissionDeniedError):
        await service.fulfill_redemption(db_session, student_actor, redemption.id)
    assert approve_denied.value.code == ErrorCode.PERMISSION_DENIED
    assert approve_denied.value.status_code == 403

    # The hardening flip: the TEACHER actor is refused on all three
    # decision paths too — Admin-only until scoped delegation lands.
    with pytest.raises(RedemptionPermissionDeniedError) as teacher_denied:
        await service.approve_redemption(db_session, teacher_actor, redemption.id)
    with pytest.raises(RedemptionPermissionDeniedError):
        await service.reject_redemption(
            db_session, teacher_actor, redemption.id, "越权"
        )
    with pytest.raises(RedemptionPermissionDeniedError):
        await service.fulfill_redemption(db_session, teacher_actor, redemption.id)
    assert teacher_denied.value.code == ErrorCode.PERMISSION_DENIED

    row = (
        await db_session.scalars(
            select(RewardRedemption)
            .where(RewardRedemption.id == redemption.id)
            .execution_options(populate_existing=True)
        )
    ).one()
    assert row.status == "REQUESTED"  # untouched
    entries = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student_id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION,
            )
        )
    ).all()
    assert entries == []
