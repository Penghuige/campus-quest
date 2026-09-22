# backend/tests/integration/points/test_points_constraints.py
"""Database-level points and reward constraints (spec §15/§16, §31.6/12/13).

Service code checks these first for friendly errors; these tests prove
PostgreSQL itself rejects the violations even when the application forgets:

- UNIQUE(source_type, source_id, ledger_type) on points_ledger: one ORIGINAL
  entry per source and ledger type — the §31.6 "one ASSIGNMENT_REWARD per
  Claim" idempotency key (a claim's reward and its reversal differ in
  ledger_type, so both fit the same source);
- amount is a non-zero signed integer (spec §15/§31.14): zero is
  unrepresentable, negative amounts are the redemption/refund/reversal shape;
- a ranking-affecting row must carry ranking_effective_at (spec §17.2: a
  reversal repairs the ORIGINAL ranking period, so the period attribution is
  mandatory exactly when the row counts);
- wallet ``earned_points`` cannot go negative (spec §15.1) and one
  wallet row exists per user (user_id primary key); ``available_points``
  MAY go negative as a reversal overdraft (migration 0012 controller
  ruling — spec §17.2 mandates the reversal of spent points; §31.12's
  no-negative rule binds only REDEMPTION, gated by the redemption
  service under the wallet lock);
- RewardItem stock is null (unlimited) or non-negative and point_cost is
  positive (spec §16/§31.13/14);
- redemption status is the frozen five-member set (interfaces.md, spec
  §16.1) and the term_key snapshot is mandatory (spec §16.1: per-term limits
  count by the snapshot, never by the current term);
- one reservation row per redemption (UNIQUE(redemption_id)): the ACTIVE
  row transitions to RELEASED/CONSUMED in place, and released_at is set
  exactly when the reservation leaves ACTIVE;
- ledger rows are append-only by convention: no onupdate columns and no
  UPDATE path (spec §15; same discipline as the submission audit tables).

Rows are built after their parents flush: ids are server-generated, so a
transient parent's id is still None at construction time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType, RedemptionStatus
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)


def _student(username: str = "20250010001") -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _ledger(user: User, **overrides: Any) -> PointsLedger:
    fields: dict[str, Any] = {
        "user_id": user.id,
        "ledger_type": LedgerType.ASSIGNMENT_REWARD,
        "amount": 100,
        "source_type": "ASSIGNMENT_CLAIM",
        "source_id": uuid4(),
        "affects_balance": True,
        "affects_ranking": True,
        "ranking_effective_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return PointsLedger(**fields)


def _reward_item(**overrides: Any) -> RewardItem:
    fields: dict[str, Any] = {
        "name": "平时成绩 +1",
        "description": "在参与课程的平时成绩中加一分。",
        "point_cost": 1000,
        "stock": 10,
        "per_user_term_limit": 1,
        "fulfillment_instructions": "由教师在成绩单中录入。",
    }
    fields.update(overrides)
    return RewardItem(**fields)


def _redemption(user: User, item: RewardItem, **overrides: Any) -> RewardRedemption:
    fields: dict[str, Any] = {
        "user_id": user.id,
        "reward_item_id": item.id,
        "term_key": "2026-fall",
        "points": item.point_cost,
    }
    fields.update(overrides)
    return RewardRedemption(**fields)


def _reservation(
    user: User, redemption: RewardRedemption, **overrides: Any
) -> PointReservation:
    fields: dict[str, Any] = {
        "user_id": user.id,
        "redemption_id": redemption.id,
        "points": redemption.points,
    }
    fields.update(overrides)
    return PointReservation(**fields)


async def _flush(db_session: AsyncSession, *objects: Any) -> None:
    db_session.add_all(objects)
    await db_session.flush()


@pytest.mark.integration
async def test_duplicate_source_triple_rejected(db_session: AsyncSession) -> None:
    """(source_type, source_id, ledger_type) is unique (spec §31.6): the
    claim's ASSIGNMENT_REWARD cannot be duplicated even with a different
    amount, while the reversal of the same claim fits because its
    ledger_type differs. Valid rows are inserted first because a mid-test
    rollback would take the flushed parent user down with the savepoint."""
    student = _student()
    await _flush(db_session, student)
    source_id = uuid4()
    original = _ledger(student, source_id=source_id, amount=100)
    await _flush(db_session, original)
    # The reversal of the same claim is a different ledger_type, not a
    # duplicate: -100 with reversal_of pointing at the original.
    await _flush(
        db_session,
        _ledger(
            student,
            ledger_type=LedgerType.ASSIGNMENT_REWARD_REVERSAL,
            amount=-100,
            source_id=source_id,
            affects_balance=True,
            affects_ranking=True,
            reversal_of_id=original.id,
        ),
    )

    db_session.add(
        _ledger(student, source_id=source_id, amount=80)  # duplicate triple
    )
    with pytest.raises(
        IntegrityError,
        match="uq_points_ledger_source_type_source_id_ledger_type",
    ):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_ledger_amount_zero_rejected(db_session: AsyncSession) -> None:
    """amount is a non-zero signed integer (spec §15/§31.14): zero rows are
    meaningless ledger noise; negative amounts (the redemption shape) are
    the normal sign convention. The valid row is inserted first because a
    mid-test rollback would take the flushed parent user down with the
    savepoint."""
    student = _student()
    await _flush(db_session, student)

    await _flush(
        db_session,
        _ledger(
            student,
            ledger_type=LedgerType.REWARD_REDEMPTION,
            amount=-1000,
            source_type="REWARD_REDEMPTION",
            affects_balance=True,
            affects_ranking=False,
            ranking_effective_at=None,
        ),
    )

    db_session.add(_ledger(student, amount=0))
    with pytest.raises(IntegrityError, match="ck_points_ledger_amount"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"ledger_type": "BOGUS_TYPE"}, "ck_points_ledger_ledger_type"),
        (
            {"ranking_effective_at": None},
            "ck_points_ledger_ranking_effective_required",
        ),
    ],
)
async def test_ledger_closed_set_and_ranking_coherence_reject_invalid(
    db_session: AsyncSession,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    """ledger_type is the frozen five-member set (interfaces.md), and a
    ranking-affecting row must carry ranking_effective_at (spec §17.2): the
    ranking projection cannot attribute a row to a period it does not name."""
    student = _student()
    await _flush(db_session, student)

    db_session.add(_ledger(student, **overrides))
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"earned_points": -1}, "ck_point_wallets_earned_points"),
    ],
)
async def test_wallet_earned_negative_rejected(
    db_session: AsyncSession,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    """Wallet ``earned_points`` never goes negative (spec §15.1): it only
    accumulates positive task contributions, so a negative value would be
    corruption, not policy. (``available_points`` lost this CHECK in
    migration 0012 — see the overdraft test below.)"""
    student = _student()
    await _flush(db_session, student)

    db_session.add(PointWallet(user_id=student.id, **overrides))
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_wallet_available_overdraft_persists(
    db_session: AsyncSession,
) -> None:
    """Migration 0012's controller ruling (user veto point at PR):
    ``available_points`` may go NEGATIVE — a reversal of already-spent
    points (spec §17.2: the reversal entry must exist even then; the
    ledger==wallet projection invariant forbids clamping) overdrafts the
    wallet instead of being rejected. Spec §31.12 binds only REDEMPTION,
    which the redemption service gates under the wallet row lock — so a
    negative balance cannot be spent from."""
    student = _student()
    await _flush(db_session, student)

    wallet = PointWallet(user_id=student.id, available_points=-150)
    await _flush(db_session, wallet)
    assert wallet.available_points == -150
    assert wallet.earned_points == 0


@pytest.mark.integration
async def test_wallet_zero_balances_and_single_row_per_user(
    db_session: AsyncSession,
) -> None:
    """Zero balances are a valid fresh wallet, and user_id is the primary
    key: a second wallet row for the same user is unrepresentable."""
    student = _student()
    await _flush(db_session, student)

    wallet = PointWallet(user_id=student.id)
    await _flush(db_session, wallet)
    assert wallet.available_points == 0
    assert wallet.earned_points == 0
    assert wallet.updated_at is not None

    db_session.add(PointWallet(user_id=student.id))
    with pytest.raises(IntegrityError, match="pk_point_wallets"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"stock": -1}, "ck_reward_items_stock"),
        ({"point_cost": 0}, "ck_reward_items_point_cost"),
        ({"per_user_term_limit": -1}, "ck_reward_items_per_user_term_limit"),
        (
            {
                "available_from": datetime(2026, 9, 1, tzinfo=UTC),
                "available_until": datetime(2026, 9, 1, tzinfo=UTC),
            },
            "ck_reward_items_window_ordering",
        ),
    ],
)
async def test_reward_item_domain_checks_reject_invalid(
    db_session: AsyncSession,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    """RewardItem stock is null (unlimited) or non-negative (spec §16,
    §31.13), point_cost is positive (§31.14 integers, never free), the
    per-term limit is non-negative, and the availability window is either
    unbounded on a side or a real half-open interval."""
    db_session.add(_reward_item(**overrides))
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_reward_item_unlimited_and_zero_stock_persist(
    db_session: AsyncSession,
) -> None:
    """Null stock means unlimited and zero stock means sold-out-but-listed
    (spec §16); both are representable, only negative stock is not."""
    unlimited = _reward_item(
        stock=None,
        per_user_term_limit=None,
        available_from=None,
        available_until=None,
    )
    sold_out = _reward_item(name="无理由请假条 x1", stock=0, point_cost=500)
    await _flush(db_session, unlimited, sold_out)
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(RewardItem).where(RewardItem.id == unlimited.id)
    )
    assert loaded is not None
    assert loaded.stock is None
    assert loaded.enabled is True
    assert loaded.requires_manual_review is False
    assert loaded.created_at is not None


@pytest.mark.integration
async def test_redemption_defaults_present_and_term_key_required(
    db_session: AsyncSession,
) -> None:
    """A redemption created without a status starts at the frozen initial
    member REQUESTED (spec §16.1), while the term_key snapshot is always
    mandatory: per-term limits count by the snapshot (spec §16.1), never by
    whatever the current term happens to be later."""
    student = _student()
    await _flush(db_session, student)
    item = _reward_item()
    await _flush(db_session, item)

    redemption = _redemption(student, item)
    await _flush(db_session, redemption)
    db_session.expunge_all()

    loaded = await db_session.scalar(
        select(RewardRedemption).where(RewardRedemption.id == redemption.id)
    )
    assert loaded is not None
    assert loaded.status == RedemptionStatus.REQUESTED
    assert loaded.term_key == "2026-fall"
    assert loaded.created_at is not None
    assert loaded.decided_at is None
    assert loaded.fulfilled_at is None

    db_session.add(_redemption(student, item, term_key=None))
    with pytest.raises(IntegrityError, match="term_key"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_redemption_status_closed_set_rejects_unknown(
    db_session: AsyncSession,
) -> None:
    """Redemption status is the frozen five-member set (interfaces.md, spec
    §16.1): REQUESTED/UNDER_REVIEW/APPROVED/FULFILLED/REJECTED — a misspelled
    or aspirational state cannot be persisted."""
    student = _student()
    await _flush(db_session, student)
    item = _reward_item()
    await _flush(db_session, item)

    db_session.add(_redemption(student, item, status="CANCELLED"))
    with pytest.raises(IntegrityError, match="ck_reward_redemptions_status"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_second_reservation_per_redemption_rejected(
    db_session: AsyncSession,
) -> None:
    """UNIQUE(redemption_id): exactly one reservation row per redemption —
    the ACTIVE row transitions to RELEASED/CONSUMED in place, so a second
    row for the same redemption can never appear (spec §16.2)."""
    student = _student()
    await _flush(db_session, student)
    item = _reward_item()
    await _flush(db_session, item)
    redemption = _redemption(student, item)
    await _flush(db_session, redemption)

    await _flush(db_session, _reservation(student, redemption))

    db_session.add(_reservation(student, redemption))
    with pytest.raises(IntegrityError, match="uq_point_reservations_redemption_id"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        # An unknown status breaks both the member set and the release
        # coherence (a non-member cannot satisfy either coherence arm);
        # PostgreSQL does not guarantee CHECK evaluation order, so either
        # rejection proves the database boundary refuses the row.
        (
            {"status": "EXPIRED"},
            r"ck_point_reservations_(status|release_coherence)",
        ),
        (
            {"status": "RELEASED"},
            "ck_point_reservations_release_coherence",
        ),
        (
            {"status": "ACTIVE", "released_at": datetime.now(UTC)},
            "ck_point_reservations_release_coherence",
        ),
    ],
)
async def test_reservation_closed_set_and_release_coherence_reject_invalid(
    db_session: AsyncSession,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    """Reservation status is ACTIVE/RELEASED/CONSUMED, and released_at is
    set exactly when the reservation leaves ACTIVE (spec §16.2): an ACTIVE
    row with a release stamp, or a terminal row without one, is
    unrepresentable."""
    student = _student()
    await _flush(db_session, student)
    item = _reward_item()
    await _flush(db_session, item)
    redemption = _redemption(student, item)
    await _flush(db_session, redemption)

    db_session.add(_reservation(student, redemption, **overrides))
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_ledger_rows_are_append_only_by_convention() -> None:
    """PointsLedger is immutable audit history (spec §15: no UPDATE of
    amounts, no DELETE; corrections are new reversal rows), so the model
    declares no onupdate columns and documents append-only semantics; the
    points services of later plan tasks expose INSERT-only paths. The
    database deliberately does not enforce append-only — a trigger would put
    runtime policy into the schema (same ruling as the submission audit
    tables)."""
    assert PointsLedger.__doc__ is not None
    assert "append-only" in PointsLedger.__doc__.lower()
    for column in PointsLedger.__table__.columns:
        assert column.onupdate is None, (
            f"PointsLedger.{column.name} must not auto-update"
        )
