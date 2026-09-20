# backend/app/modules/points/ledger_service.py
"""Append-only ledger posting with the wallet projection kept in one
transaction (spec §15/§15.1; plan 05 task 2).

Design decisions:

- **Append-only discipline (spec §15).** The only write path this
  service exposes is INSERT: ``post_entry`` builds one ``PointsLedger``
  row, and no method updates or deletes ledger rows. Corrections are
  future reversal rows linked through ``reversal_of_id`` (a later task's
  ``reversal`` use case); the database deliberately does not enforce
  append-only (models.py ruling).
- **Transaction ownership (backend-engineering §5).** Nothing here
  commits or rolls back: ``post_entry`` flushes, and the CALLER — the
  review-approve transaction, the redemption service, the admin route —
  decides transaction lifetime. That is what makes the §14 ten-step
  approve atomic: the grant joins the caller's unit of work. The test
  suite proves it by forcing a wallet failure after the ledger insert
  flushed and rolling the caller's transaction back.
- **Wallet projection rules (spec §15.1).** ``available_points`` moves
  by ``amount`` for EVERY balance-affecting entry (both signs);
  ``earned_points`` (累计任务贡献, the total-board/honors figure) moves
  only for POSITIVE ranking-affecting entries. A reversal therefore
  repairs balances here but repairs RANKINGS through the ledger
  aggregation (task 6 sums signed ``affects_ranking`` rows by
  ``ranking_effective_at``), never by decrementing ``earned_points``;
  both projections stay rebuildable from the ledger — the earned rule
  is ``SUM(amount) WHERE affects_ranking AND amount > 0``.
- **Wallet row lifecycle under concurrency (backend-engineering §7).**
  Every balance- or earned-touching post locks the user's wallet row
  ``FOR UPDATE`` before mutating it, so concurrent posts compose instead
  of losing updates. The FIRST entry for a user may race another first
  entry: the creation path is ``INSERT ... ON CONFLICT DO NOTHING``
  followed by the lock. A loser's INSERT blocks on the winner's
  uncommitted row, resumes once that transaction commits (row stays,
  loser skips and locks it) or rolls back (loser's own insert stands) —
  no retry loop, no IntegrityError to translate, no duplicate wallet
  row (``user_id`` is the primary key).
- **Idempotent assignment reward (spec §31.6, interfaces.md).**
  ``grant_assignment_reward`` maps the claim to the source triple
  ('ASSIGNMENT_CLAIM', claim_id, 'ASSIGNMENT_REWARD') that the UNIQUE
  constraint in migration 0007 makes the idempotency key. The happy
  path is a plain read: an existing row is returned untouched. The race
  loser — its read missed the winner's uncommitted row — wraps its
  insert in a SAVEPOINT, takes the UNIQUE violation, rolls back to the
  savepoint (which also expunges the never-inserted pending row), and
  returns the winner's committed row. Never a second entry, never a
  second wallet increment. Only the expected constraint is caught;
  anything else re-raises (backend-engineering §7).
- **Friendly gates first (backend-engineering §6).** Zero amounts,
  reason-less admin adjustments (spec §15), and ranking rows without a
  period attribution (spec §17.2) are rejected as ``VALIDATION_ERROR``
  before PostgreSQL sees them; the database CHECKs — including 0011's
  admin-reason CHECK — remain the backstop.
- ``get_spendable_points`` is a lock-free point-in-time read
  (available minus ACTIVE reservations, spec §16.2). The redemption
  service must re-check spendability under the wallet lock before
  freezing points; this read feeds displays and advisory checks.
- **The frozen port adapter.** ``PointsRewardPortAdapter`` implements
  the interfaces.md ``PointsRewardPort`` signature over this service
  for the Plan-04 review-approve caller: it grants ``locked_points``
  (the fraction-adjusted basis, §31.1), attributes the ranking period
  to the claim's ``reward_locked_at`` (the submit instant the reward
  lock froze; ``terminal_at`` as a defensive fallback), and accepts
  ``idempotency_key`` without parsing it — the UNIQUE source triple
  over ``claim_id`` IS the mechanism, so the points module never
  couples to the review service's key grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.points.enums import LedgerType, ReservationStatus
from app.modules.points.models import PointReservation, PointsLedger, PointWallet
from app.modules.submissions.review_service import GrantResult
from app.modules.tasks.models import AssignmentClaim

__all__ = [
    "InvalidLedgerEntryError",
    "LedgerService",
    "PointsRewardPortAdapter",
    "PostLedgerEntry",
]

# The §31.6 idempotency mechanism's name (migration 0007): only this
# constraint's violation is translated into the idempotent grant path.
_SOURCE_TRIPLE_UQ = "uq_points_ledger_source_type_source_id_ledger_type"

# Polymorphic source vocabulary (models.py): an assignment reward's
# source is the claim it pays.
_ASSIGNMENT_CLAIM_SOURCE = "ASSIGNMENT_CLAIM"


# --- command and typed error ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostLedgerEntry:
    """One requested ledger posting; every insert states both effect
    flags explicitly (spec §15 — no defaults on the model)."""

    user_id: UUID
    ledger_type: LedgerType
    amount: int
    source_type: str
    affects_balance: bool
    affects_ranking: bool
    ranking_effective_at: datetime | None = None
    # None -> the service mints a fresh source id: each entry is its own
    # source event (the ADMIN_ADJUSTMENT shape, models.py).
    source_id: UUID | None = None
    reversal_of_id: UUID | None = None
    operator_id: UUID | None = None
    reason: str | None = None


class InvalidLedgerEntryError(BusinessError):
    """The command cannot become a ledger row: the friendly gate ahead
    of the database CHECKs (backend-engineering §6)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            message,
            status_code=422,
            details={"field": field},
        )


def _validate(command: PostLedgerEntry) -> None:
    if command.amount == 0:
        raise InvalidLedgerEntryError("amount", "积分流水金额必须是非零整数")
    if command.ledger_type is LedgerType.ADMIN_ADJUSTMENT and not (
        command.reason and command.reason.strip()
    ):
        raise InvalidLedgerEntryError("reason", "管理员积分调整必须填写原因")
    if command.affects_ranking and command.ranking_effective_at is None:
        raise InvalidLedgerEntryError(
            "ranking_effective_at", "影响排名的积分流水必须提供排名生效时间"
        )
    if not command.affects_ranking and command.ranking_effective_at is not None:
        raise InvalidLedgerEntryError(
            "ranking_effective_at", "不影响排名的积分流水不应提供排名生效时间"
        )


# --- the service ----------------------------------------------------------------------


class LedgerService:
    """Posts immutable ledger entries and maintains the wallet
    projection in the caller's transaction (see module docstring)."""

    async def post_entry(
        self, db: AsyncSession, command: PostLedgerEntry
    ) -> PointsLedger:
        """INSERT one append-only ledger row and apply its wallet
        projection effects; flush only — the caller owns the commit
        (backend-engineering §5)."""
        _validate(command)
        touches_balance = command.affects_balance
        touches_earned = command.affects_ranking and command.amount > 0
        wallet: PointWallet | None = None
        if touches_balance or touches_earned:
            wallet = await self._locked_or_created_wallet(db, command.user_id)

        reason = command.reason.strip() if command.reason is not None else None
        reason_text = reason or None
        entry = PointsLedger(
            user_id=command.user_id,
            ledger_type=command.ledger_type.value,
            amount=command.amount,
            source_type=command.source_type,
            source_id=(command.source_id if command.source_id is not None else uuid4()),
            affects_balance=command.affects_balance,
            affects_ranking=command.affects_ranking,
            ranking_effective_at=command.ranking_effective_at,
            reversal_of_id=command.reversal_of_id,
            operator_id=command.operator_id,
            reason=reason_text,
        )
        db.add(entry)
        # The entry lands BEFORE the wallet mutates: a UNIQUE race or
        # constraint failure surfaces with the wallet untouched, and a
        # wallet CHECK failure (overspend) still rolls back through the
        # caller — the two writes share one transaction either way.
        await db.flush()

        if wallet is not None:
            if touches_balance:
                wallet.available_points += command.amount
            if touches_earned:
                # Spec §15.1: earned counts positive task contributions
                # only — reversals repair rankings via the ledger, not
                # by decrementing earned_points.
                wallet.earned_points += command.amount
            await db.flush()
        return entry

    async def get_spendable_points(self, db: AsyncSession, user_id: UUID) -> int:
        """Lock-free point-in-time spendable balance: wallet's
        available_points minus ACTIVE reservations (spec §16.2); a user
        without a wallet has 0. The redemption service re-checks under
        the wallet lock before freezing points."""
        available = await db.scalar(
            select(PointWallet.available_points).where(PointWallet.user_id == user_id)
        )
        if available is None:
            return 0
        frozen = await db.scalar(
            select(func.coalesce(func.sum(PointReservation.points), 0)).where(
                PointReservation.user_id == user_id,
                PointReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        assert frozen is not None
        return available - frozen

    async def grant_assignment_reward(
        self,
        db: AsyncSession,
        *,
        claim_id: UUID,
        user_id: UUID,
        amount: int,
        ranking_effective_at: datetime,
    ) -> PointsLedger:
        """Post the claim's ASSIGNMENT_REWARD exactly once (spec §31.6).

        A replay returns the ORIGINAL row — the entry that won the
        UNIQUE(source_type, source_id, ledger_type) race — with nothing
        written. The entry affects both the balance and the ranking and
        carries the caller-supplied lock time as its period attribution
        (spec §17.2).
        """
        if amount <= 0:
            raise InvalidLedgerEntryError("amount", "任务奖励必须为正数积分")
        if ranking_effective_at is None:
            raise InvalidLedgerEntryError(
                "ranking_effective_at",
                "任务奖励必须携带排名生效时间（认领的奖励锁时间）",
            )

        existing = cast(
            "PointsLedger | None",
            await db.scalar(self._claim_reward_filter(claim_id)),
        )
        if existing is not None:
            return existing  # idempotent replay: nothing written

        try:
            # The savepoint bounds the race loser's damage: the UNIQUE
            # violation aborts only this insert, leaving the caller's
            # transaction usable for the recovery read below.
            async with db.begin_nested():
                return await self.post_entry(
                    db,
                    PostLedgerEntry(
                        user_id=user_id,
                        ledger_type=LedgerType.ASSIGNMENT_REWARD,
                        amount=amount,
                        source_type=_ASSIGNMENT_CLAIM_SOURCE,
                        source_id=claim_id,
                        affects_balance=True,
                        affects_ranking=True,
                        ranking_effective_at=ranking_effective_at,
                    ),
                )
        except IntegrityError as exc:
            if _SOURCE_TRIPLE_UQ not in str(exc):
                raise  # unknown database failure, not our idempotency race
            # The wallet lock serialized us behind the winner's commit,
            # so the row is visible now. Expire savepoint-scoped state
            # (the never-inserted pending entry, the wallet snapshot)
            # before re-reading.
            db.expire_all()
            existing = cast(
                "PointsLedger | None",
                await db.scalar(self._claim_reward_filter(claim_id)),
            )
            if existing is None:
                raise
            return existing

    async def _locked_or_created_wallet(
        self, db: AsyncSession, user_id: UUID
    ) -> PointWallet:
        """The user's wallet row under FOR UPDATE, creating it on the
        first entry. Concurrency-safe by construction: see the module
        docstring's wallet-lifecycle ruling."""
        await db.execute(
            pg_insert(PointWallet)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=[PointWallet.user_id])
        )
        wallet = await db.scalar(
            select(PointWallet).where(PointWallet.user_id == user_id).with_for_update()
        )
        assert wallet is not None  # inserted above or already present
        return wallet

    @staticmethod
    def _claim_reward_filter(claim_id: UUID) -> Select[tuple[PointsLedger]]:
        return select(PointsLedger).where(
            PointsLedger.source_type == _ASSIGNMENT_CLAIM_SOURCE,
            PointsLedger.source_id == claim_id,
            PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD.value,
        )


# --- the frozen cross-module port (interfaces.md) -------------------------------------


class PointsRewardPortAdapter:
    """Concrete ``PointsRewardPort`` over ``LedgerService``.

    The signature is interfaces.md-frozen (Plan-04's review approve is
    the first caller): ``locked_points`` is the grant basis (the reward
    lock's fraction-adjusted amount, §31.1) and ``base_points`` is the
    claim's base snapshot carried for the record. ``idempotency_key``
    is accepted and deliberately NOT parsed — the UNIQUE source triple
    over ``claim_id`` is the idempotency mechanism, so this module never
    couples to the review service's key grammar.

    Constructed per request with the session the caller's transaction
    runs on: the grant joins that transaction (§14 step 8) and the
    caller commits.
    """

    def __init__(self, *, ledger: LedgerService, db: AsyncSession) -> None:
        self._ledger = ledger
        self._db = db

    async def grant_assignment_reward(
        self,
        *,
        user_id: UUID,
        claim_id: UUID,
        base_points: int,
        locked_points: int,
        idempotency_key: str,
    ) -> GrantResult:
        effective_at = await self._claim_lock_time(claim_id)
        entry = await self._ledger.grant_assignment_reward(
            self._db,
            claim_id=claim_id,
            user_id=user_id,
            amount=locked_points,
            ranking_effective_at=effective_at,
        )
        # The persisted row's amount is the truth: on an idempotent
        # replay the ORIGINAL grant is what the user received.
        return GrantResult(
            user_id=user_id, claim_id=claim_id, points_granted=entry.amount
        )

    async def _claim_lock_time(self, claim_id: UUID) -> datetime:
        """The claim's ``reward_locked_at`` (the submit instant the
        PROVISIONAL lock froze — the ranking period attribution, spec
        §17.2), read inside the caller's transaction where the review
        flow already holds the claim row lock. ``terminal_at`` is the
        defensive fallback; a claim with neither cannot be attributed
        and is rejected before any ledger write."""
        row = (
            await self._db.execute(
                select(
                    AssignmentClaim.reward_locked_at, AssignmentClaim.terminal_at
                ).where(AssignmentClaim.id == claim_id)
            )
        ).first()
        if row is None:
            raise InvalidLedgerEntryError("claim_id", "奖励发放的目标认领不存在")
        if row.reward_locked_at is None and row.terminal_at is None:
            raise InvalidLedgerEntryError(
                "ranking_effective_at", "认领缺少奖励锁时间，无法确定排名生效时间"
            )
        return cast(datetime, row.reward_locked_at or row.terminal_at)
