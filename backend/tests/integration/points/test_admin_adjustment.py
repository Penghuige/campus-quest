# backend/tests/integration/points/test_admin_adjustment.py
"""The Admin manual points adjustment channel against real PostgreSQL
(Plan 08 T6; spec §15 人工积分调整, §17.1 ranking isolation).

Scenario map (the plan's steps, verbatim semantics):

- **Ranking isolation (step 1 / review focus 4):** a +500
  ADMIN_ADJUSTMENT raises the wallet by 500 while the daily, monthly,
  and all-time ranking scores stay EXACTLY as they were — asserted
  against the authoritative PostgreSQL aggregates
  (``RankingRepository``: SUM(amount) WHERE affects_ranking, G7) after
  a real ranking-affecting grant seeded the boards, and against the
  ranking-projection seam (the dispatcher records no enqueue). Spec
  §15/§17.1 define NO ranking-affecting adjustment operation, so none
  can be chosen here — the isolation is structural, not configured.
- **Gates (step 2):** non-Admin actors are the typed 403; a blank
  reason is the typed 400; a zero amount is the ledger's typed 422
  (answered friendly-first, before any lock); an unknown target is the
  typed 404. Refused calls write no ledger row, no wallet, no audit.
- **Ledger discipline (step 3):** the adjustment lands as exactly one
  ADMIN_ADJUSTMENT row through ``LedgerService.post_entry`` (never a
  direct wallet UPDATE), with the admin as operator and the reason on
  the row; the wallet projection moves in the same transaction.
- **Same-transaction audit (the golden pattern):** one
  ADMIN_POINTS_ADJUSTED row commits WITH the entry — before/after
  carry the wallet-balance migration read under the wallet row lock,
  plus the amount and the entry id; the correlation pair rides the
  AuditContext.
- **Overdraft legality:** a downward adjustment past zero leaves the
  wallet NEGATIVE (the migration-0012 ruling — the manual correction
  channel is the case that rule was written for).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import hash_password
from app.modules.audit.context import AuditContext
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.points.admin_service import (
    AUDIT_ADMIN_POINTS_ADJUSTED,
    AdminReasonRequiredError,
    PointsAdjustmentTargetNotFoundError,
    PointsAdminService,
)
from app.modules.points.enums import LedgerType
from app.modules.points.ledger_service import (
    InvalidLedgerEntryError,
    LedgerService,
)
from app.modules.points.models import PointsLedger, PointWallet
from app.modules.rankings.periods import (
    business_day,
    business_month,
    day_bounds,
    month_bounds,
)
from app.modules.rankings.repository import RankingRepository

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"


class _RecordingDispatcher:
    """The ranking-projection seam's spy: records every enqueue so the
    tests can pin that a ranking-neutral adjustment arms nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, datetime, str | None]] = []

    def enqueue_ranking_update(
        self, user_id: UUID, ranking_effective_at: datetime, request_id: str | None
    ) -> None:
        self.calls.append((user_id, ranking_effective_at, request_id))


async def _seed_user(db: AsyncSession, *, username: str, role: Role) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"用户{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _actor(user: User) -> Actor:
    return Actor(user_id=user.id, role=user.role)


def _service(dispatcher: _RecordingDispatcher) -> PointsAdminService:
    return PointsAdminService(ledger=LedgerService(ranking_dispatcher=dispatcher))


async def _board_scores(db: AsyncSession, user_id: UUID) -> dict[str, int]:
    """The user's three board scores from the authoritative PostgreSQL
    aggregates (G7): today's business day, this business month, and
    all-time."""
    tz = ZoneInfo(get_settings().business_timezone)
    repository = RankingRepository()
    day_start, day_end = day_bounds(business_day(_T0, tz), tz)
    month_start, month_end = month_bounds(business_month(_T0, tz), tz)
    return {
        "daily": await repository.user_score(db, user_id, day_start, day_end),
        "monthly": await repository.user_score(db, user_id, month_start, month_end),
        "all": await repository.user_score(db, user_id, None, None),
    }


async def _wallet(db: AsyncSession, user_id: UUID) -> PointWallet | None:
    return await db.get(PointWallet, user_id)


# --- ranking isolation (plan step 1)


async def test_adjustment_moves_wallet_but_never_the_boards(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="adj-rank-adm-0001", role=Role.ADMIN)
    student = await _seed_user(
        db_session, username="adj-rank-stu-0002", role=Role.STUDENT
    )

    # Seed the boards with a REAL ranking-affecting grant (+300,
    # attributed to the frozen instant's business day/month).
    await LedgerService().grant_assignment_reward(
        db_session,
        claim_id=uuid4(),
        user_id=student.id,
        amount=300,
        ranking_effective_at=_T0,
    )
    await db_session.commit()
    boards_before = await _board_scores(db_session, student.id)
    assert boards_before == {"daily": 300, "monthly": 300, "all": 300}

    dispatcher = _RecordingDispatcher()
    entry = await _service(dispatcher).admin_adjust_points(
        db_session,
        _actor(admin),
        student.id,
        500,
        reason="活动补偿",
        audit_context=AuditContext(request_id="req-adj-1", ip_address="10.0.0.9"),
    )

    # The wallet moved by exactly the amount; earned (the ranking-side
    # projection) did not.
    wallet = await _wallet(db_session, student.id)
    assert wallet is not None
    assert wallet.available_points == 800
    assert wallet.earned_points == 300

    # THE three boards are unchanged (review focus 4).
    assert await _board_scores(db_session, student.id) == boards_before

    # One ledger row, shaped exactly as the manual channel's contract.
    assert entry.ledger_type == LedgerType.ADMIN_ADJUSTMENT.value
    assert entry.amount == 500
    assert entry.user_id == student.id
    assert entry.affects_balance is True
    assert entry.affects_ranking is False
    assert entry.ranking_effective_at is None  # the coherence pair
    assert entry.operator_id == admin.id
    assert entry.reason == "活动补偿"
    assert entry.source_type == LedgerType.ADMIN_ADJUSTMENT.value
    assert entry.source_id is not None  # its own source event
    rows = (
        await db_session.scalars(
            select(PointsLedger).where(
                PointsLedger.user_id == student.id,
                PointsLedger.ledger_type == LedgerType.ADMIN_ADJUSTMENT.value,
            )
        )
    ).all()
    assert len(rows) == 1

    # The ranking-projection seam armed nothing (the adjustment is
    # ranking-neutral; a silent enqueue would drift the boards).
    assert dispatcher.calls == []


# --- gates (plan step 2)


async def test_adjustment_gates_refuse_before_anything_is_written(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="adj-gate-adm-0003", role=Role.ADMIN)
    teacher = await _seed_user(
        db_session, username="adj-gate-tch-0004", role=Role.TEACHER
    )
    student = await _seed_user(
        db_session, username="adj-gate-stu-0005", role=Role.STUDENT
    )
    service = _service(_RecordingDispatcher())

    with pytest.raises(BusinessError) as role_denied:
        await service.admin_adjust_points(
            db_session, _actor(teacher), student.id, 500, reason="教师越权"
        )
    assert role_denied.value.code == ErrorCode.PERMISSION_DENIED
    assert role_denied.value.status_code == 403

    with pytest.raises(AdminReasonRequiredError) as blank:
        await service.admin_adjust_points(
            db_session, _actor(admin), student.id, 500, reason="\t "
        )
    assert blank.value.status_code == 400

    with pytest.raises(PointsAdjustmentTargetNotFoundError) as unknown:
        await service.admin_adjust_points(
            db_session, _actor(admin), UUID(int=55), 500, reason="未知账号"
        )
    assert unknown.value.status_code == 404

    # Zero amount: the ledger's typed 422, answered BEFORE the wallet
    # lock (validate before touch).
    with pytest.raises(InvalidLedgerEntryError) as zero:
        await service.admin_adjust_points(
            db_session, _actor(admin), student.id, 0, reason="零金额"
        )
    assert zero.value.status_code == 422

    # Nothing was written by any refused call.
    ledger_rows = (
        await db_session.execute(select(func.count()).select_from(PointsLedger))
    ).scalar_one()
    assert ledger_rows == 0
    assert await _wallet(db_session, student.id) is None
    audit_rows = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AUDIT_ADMIN_POINTS_ADJUSTED)
        )
    ).scalar_one()
    assert audit_rows == 0


# --- same-transaction audit + ledger discipline (plan step 3)


async def test_adjustment_commits_its_audit_row_in_the_same_transaction(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="adj-aud-adm-0006", role=Role.ADMIN)
    student = await _seed_user(
        db_session, username="adj-aud-stu-0007", role=Role.STUDENT
    )
    await LedgerService().grant_assignment_reward(
        db_session,
        claim_id=uuid4(),
        user_id=student.id,
        amount=300,
        ranking_effective_at=_T0,
    )
    await db_session.commit()

    entry = await _service(_RecordingDispatcher()).admin_adjust_points(
        db_session,
        _actor(admin),
        student.id,
        500,
        reason="活动补偿",
        audit_context=AuditContext(request_id="req-adj-2", ip_address="10.0.0.9"),
    )

    rows = (
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.actor_user_id == admin.id,
                AuditLog.action == AUDIT_ADMIN_POINTS_ADJUSTED,
            )
        )
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == admin.id
    assert row.actor_role == Role.ADMIN.value
    assert row.target_type == "user"
    assert row.target_id == str(student.id)
    assert row.reason == "活动补偿"
    assert row.request_id == "req-adj-2"
    assert row.ip_address == "10.0.0.9"
    # The wallet-balance migration under the lock (the reversal's §30
    # discipline), plus the amount and the entry it produced.
    assert row.before_snapshot == {"available_points": 300}
    assert row.after_snapshot == {
        "available_points": 800,
        "amount": 500,
        "ledger_entry_id": str(entry.id),
    }
    assert row.created_at is not None


async def test_downward_adjustment_may_overdraft_the_wallet(
    db_session: AsyncSession,
) -> None:
    """The manual correction channel may drive the spendable projection
    negative (migration 0012): the ledger keeps the true figure, the
    ranking aggregates do not move, and the entry is the ordinary
    negative-amount shape."""
    admin = await _seed_user(db_session, username="adj-ovr-adm-0008", role=Role.ADMIN)
    student = await _seed_user(
        db_session, username="adj-ovr-stu-0009", role=Role.STUDENT
    )
    await LedgerService().grant_assignment_reward(
        db_session,
        claim_id=uuid4(),
        user_id=student.id,
        amount=100,
        ranking_effective_at=_T0,
    )
    await db_session.commit()

    entry = await _service(_RecordingDispatcher()).admin_adjust_points(
        db_session, _actor(admin), student.id, -300, reason="误发冲减"
    )

    assert entry.amount == -300
    wallet = await _wallet(db_session, student.id)
    assert wallet is not None
    assert wallet.available_points == -200
    assert wallet.earned_points == 100  # ranking side untouched
    assert await _board_scores(db_session, student.id) == {
        "daily": 100,
        "monthly": 100,
        "all": 100,
    }
