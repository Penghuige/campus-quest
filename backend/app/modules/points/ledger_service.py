# backend/app/modules/points/ledger_service.py
"""Append-only ledger posting with the wallet projection kept in one
transaction (spec §15/§15.1; plan 05 tasks 2 and 5).

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
  reason-less admin adjustments (spec §15), ranking rows without a
  period attribution (spec §17.2), and — the T2 review fold —
  source-less non-ADMIN_ADJUSTMENT entries are rejected as
  ``VALIDATION_ERROR`` before PostgreSQL sees them; the database
  CHECKs — including 0011's admin-reason CHECK — remain the backstop.
  The source gate exists because a REWARD_REDEMPTION or ASSIGNMENT_*
  entry without its source object would silently mint a random UUID,
  breaking the (source_type, source_id, ledger_type) idempotency
  semantics (spec §31.6): only ADMIN_ADJUSTMENT rows are their own
  source event and may omit the id (models.py).
- ``get_spendable_points`` is a lock-free point-in-time read
  (available minus ACTIVE reservations, spec §16.2). The redemption
  service must re-check spendability under the wallet lock before
  freezing points; this read feeds displays and advisory checks.
- **Reward reversal is admin-grade and append-only (spec §17.2; task
  5).** ``reverse_assignment_reward`` NEVER touches the original row:
  it posts one new ``ASSIGNMENT_REWARD_REVERSAL`` entry with amount
  ``-original.amount``, ``reversal_of_id`` pointing back, the original's
  source triple with a different ledger_type — so UNIQUE(source_type,
  source_id, ledger_type) makes the reversal one-per-claim exactly as
  it makes the grant one-per-claim (§31.6) — ``affects_balance`` and
  ``affects_ranking`` both true (正常作弊冲销两者都影响), and the
  ORIGINAL's ``ranking_effective_at``: a September decision repairs the
  August period and all-time, never September. Reason is mandatory and
  the actor must be ADMIN (spec §15 人工积分调整/冲销 channel): a teacher
  cannot reverse a paid reward — the teacher's correction channel is
  the review-side INVALIDATE_REWARD_LOCK (§11.3); a reversal of points
  that were already spent OVERDRAFS ``available_points`` negative
  (migration 0012 controller ruling — see models.py; user veto point at
  PR). A second reversal — sequential replay or UNIQUE-race loser, both
  mapped through the savepoint recovery to the same typed error — is
  REJECTED, not silently idempotent: a reversal is a recorded admin
  decision (its reason and operator are the audit trail, §38.6 奖励冲销),
  so returning an existing row would hide that THIS request's reason
  was never recorded.
- **The ranking-projection seam is a post-commit port (task 6).** The
  grant AND the reversal hand the changed entry to the rankings
  module's ``RankingUpdateDispatcher`` — task 6's recompute-from-
  PostgreSQL channel (ZADD the absolute aggregate, never ZINCRBY) —
  carrying the user, the entry's ``ranking_effective_at`` (so the
  recompute addresses the period the entry was credited to: a
  September decision repairs August and all-time), and the caller's
  optional correlation ``request_id``. Because this service owns no
  transactions (the rule above), the enqueue is registered on the
  CALLER's session ``after_commit`` hook: a rollback never enqueues an
  entry that never landed, and the flush-only contract stays intact.
  The composition roots bind the workers' ``CeleryRankingDispatcher``
  (the real job publish) as the default dispatcher; tests inject a
  recording fake. The grant arms the hook only on the path that WROTE
  a new row — an idempotent replay changed nothing, so it enqueues
  nothing. The listener is BEST-EFFORT (final-review N1): it fires
  inside the caller's already-durable commit, so a publish failure
  (broker outage) logs and never surfaces out of the commit — a
  missed enqueue heals at the next rebuild, exactly like the honors
  seam's failure tolerance. Rank HONORS (DAILY_RANK/MONTHLY_RANK) are
  not this seam's business: their rank+period facts exist only at
  period close, whose producer is Plan 07/08's scheduled beat
  (honor_service's ruling).
- **The frozen port adapter.** ``PointsRewardPortAdapter`` implements
  the interfaces.md ``PointsRewardPort`` signature over this service
  for the Plan-04 review-approve caller: it grants ``locked_points``
  (the fraction-adjusted basis, §31.1), attributes the ranking period
  to the claim's ``reward_locked_at`` (the submit instant the reward
  lock froze; ``terminal_at`` as a defensive fallback), accepts
  ``idempotency_key`` without parsing it — the UNIQUE source triple
  over ``claim_id`` IS the mechanism, so the points module never
  couples to the review service's key grammar — and threads that key
  through as the projection job's ``request_id`` while handing the
  composition root's dispatcher to the grant (final-review C1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Select, event, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as OrmSession

from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.modules.identity.enums import Role
from app.modules.identity.events import Actor
from app.modules.points.enums import LedgerType, ReservationStatus
from app.modules.points.models import PointReservation, PointsLedger, PointWallet
from app.modules.rankings.redis_projection import RankingUpdateDispatcher
from app.modules.submissions.review_service import GrantResult
from app.modules.tasks.models import AssignmentClaim

logger = logging.getLogger(__name__)

__all__ = [
    "InvalidLedgerEntryError",
    "LedgerEntryNotFoundError",
    "LedgerEntryNotReversibleError",
    "LedgerService",
    "PointsRewardPortAdapter",
    "PostLedgerEntry",
    "RankingProjectionDispatcher",
    "RewardAlreadyReversedError",
    "RewardReversalPermissionDeniedError",
    "RewardReversalReasonRequiredError",
    "WalletSummary",
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
    # None -> the service mints a fresh source id: allowed ONLY for
    # ADMIN_ADJUSTMENT (each adjustment is its own source event,
    # models.py); every other type must point at its source object —
    # the T2 review fold gate in ``_validate`` enforces it.
    source_id: UUID | None = None
    reversal_of_id: UUID | None = None
    operator_id: UUID | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WalletSummary:
    """The wallet display read (spec §15.1/§16.2, plan 05 task 8): the
    projection's two figures plus the spendable derivation (available
    minus ACTIVE reservations). A user without a wallet reads all
    zeros — the projection row is created lazily by the first
    balance-affecting entry."""

    available_points: int
    earned_points: int
    spendable_points: int


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
    # The T2 review fold: only ADMIN_ADJUSTMENT is its own source event;
    # every other type's source triple IS the idempotency mechanism, so
    # a missing source_id must fail loudly instead of minting a random
    # UUID that can never be replayed against (spec §31.6; models.py).
    if command.source_id is None and command.ledger_type is not (
        LedgerType.ADMIN_ADJUSTMENT
    ):
        raise InvalidLedgerEntryError(
            "source_id", "该类型积分流水必须显式提供 source_id（指向其来源对象）"
        )


# --- reward reversal: the ranking port and typed errors (task 5) ----------------------


# SINGLE DEFINITION (final-review C1): the ranking-projection port is
# owned by the rankings module (``RankingUpdateDispatcher``, frozen in
# redis_projection.py; the workers-side binding is
# ``CeleryRankingDispatcher``). The historical points-side name stays
# exported as an alias so existing imports keep working — there is no
# second, divergent protocol shape to drift again.
RankingProjectionDispatcher = RankingUpdateDispatcher


class RewardReversalPermissionDeniedError(BusinessError):
    """The actor is not ADMIN — reversal is 人工积分调整/冲销, an
    admin-grade channel (spec §15); a teacher's correction channel is
    the review-side INVALIDATE_REWARD_LOCK (§11.3), never a reversal."""

    def __init__(self, actor_id: UUID, role: Role) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            "只有管理员可以冲销任务奖励",
            status_code=403,
            details={"actor_id": str(actor_id), "role": role.value},
        )


class RewardReversalReasonRequiredError(BusinessError):
    """A reversal without a non-blank reason: the correction IS its audit
    trail (spec §15 reason principle, §38.6 奖励冲销), so blank is not a
    reason."""

    def __init__(self, ledger_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "冲销任务奖励必须填写原因",
            status_code=422,
            details={"ledger_id": str(ledger_id), "field": "reason"},
        )


class LedgerEntryNotFoundError(BusinessError):
    """No PointsLedger row for the id (the claim-service 404 shape)."""

    def __init__(self, ledger_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            "积分流水不存在",
            status_code=404,
            details={"ledger_id": str(ledger_id)},
        )


class LedgerEntryNotReversibleError(BusinessError):
    """The target is not an ASSIGNMENT_REWARD row: a reversal itself
    cannot re-reverse, redemptions have their own future refund channel,
    and admin adjustments are their own correction channel (spec §15)."""

    def __init__(self, ledger_id: UUID, ledger_type: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "只有任务奖励流水可以被冲销",
            status_code=422,
            details={"ledger_id": str(ledger_id), "ledger_type": ledger_type},
        )


class RewardAlreadyReversedError(BusinessError):
    """A reversal of this claim's reward already exists — the UNIQUE
    triple ('ASSIGNMENT_CLAIM', claim, 'ASSIGNMENT_REWARD_REVERSAL')
    makes the reversal one-per-claim (spec §31.6 shape), and a second
    request is a typed rejection, not a silent replay: the recorded
    reason and operator are the decision's audit trail."""

    def __init__(
        self,
        *,
        ledger_id: UUID,
        source_id: UUID,
        reversal_ledger_id: UUID,
    ) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "该任务奖励已被冲销",
            status_code=409,
            details={
                "ledger_id": str(ledger_id),
                "source_id": str(source_id),
                "reversal_ledger_id": str(reversal_ledger_id),
            },
        )


def _enqueue_ranking_update_after_commit(
    db: AsyncSession,
    dispatcher: RankingProjectionDispatcher,
    user_id: UUID,
    ranking_effective_at: datetime,
    request_id: str | None = None,
) -> None:
    """Register the ranking-projection trigger on the CALLER's commit.

    This service owns no transactions, so the only correct moment it can
    observe is the caller's ``after_commit`` session hook: a rollback
    leaves the listener unfired (no phantom enqueue for an entry that
    never landed) and ``once=True`` keeps the listener from leaking
    across later commits. The port itself stays a sync publish, so the
    production adapter (the workers' ``CeleryRankingDispatcher``) decides
    durability. The payload is the full worker contract: the user, the
    CHANGED ENTRY's ``ranking_effective_at`` (the worker recomputes
    exactly that instant's business day, its business month, and
    all-time — never every period), and the caller's correlation id
    (None means the dispatcher generates one).

    The listener is BEST-EFFORT (final-review N1): it fires INSIDE the
    caller's ``commit()``, at which point the business write is already
    durable, so a publish failure (broker outage) is swallowed with a
    warning instead of surfacing out of the commit as a 500 — and on
    the approve path, instead of skipping the honors trigger that runs
    after commit() returns. A missed enqueue heals at the next rebuild
    or any later trigger (spec §32; the outbox rule the honors seam
    follows too).
    """

    def _fire(session: OrmSession) -> None:
        try:
            dispatcher.enqueue_ranking_update(user_id, ranking_effective_at, request_id)
        except Exception:
            logger.warning(
                "ranking_projection.enqueue_failed",
                extra={
                    "user_id": str(user_id),
                    "ranking_effective_at": ranking_effective_at.isoformat(),
                    "request_id": request_id,
                },
                exc_info=True,
            )

    event.listen(db.sync_session, "after_commit", _fire, once=True)


# --- the service ----------------------------------------------------------------------


class LedgerService:
    """Posts immutable ledger entries and maintains the wallet
    projection in the caller's transaction (see module docstring).

    ``ranking_dispatcher`` is the DEFAULT ranking-projection trigger the
    composition root binds (the workers' ``CeleryRankingDispatcher``):
    ranking-affecting writes enqueue the recompute job on the caller's
    commit unless the call site passes its own dispatcher. ``None``
    (tests, or a caller that only posts ranking-neutral rows) simply
    never enqueues.
    """

    def __init__(
        self,
        ranking_dispatcher: RankingProjectionDispatcher | None = None,
    ) -> None:
        self._ranking_dispatcher = ranking_dispatcher

    def _resolve_dispatcher(
        self, override: RankingProjectionDispatcher | None
    ) -> RankingProjectionDispatcher | None:
        """Call-site dispatcher wins; else the constructor default."""
        return override if override is not None else self._ranking_dispatcher

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
            wallet = await self.locked_or_created_wallet(db, command.user_id)

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
        # wallet-update failure still rolls back through the caller —
        # the two writes share one transaction either way. (Overspend
        # is no longer a database CHECK failure: migration 0012 lets a
        # reversal drive available_points negative; redemption overspend
        # is gated by the redemption service under this same wallet
        # lock, models.py's 0012 ruling.)
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

    async def get_wallet_summary(
        self, db: AsyncSession, user_id: UUID
    ) -> WalletSummary:
        """The wallet display read (spec §15.1/§16.2): the projection's
        available/earned figures plus the spendable derivation in one
        place, so the ``/points/me`` strip and any future surface quote
        identical numbers. Lock-free like ``get_spendable_points``; a
        user without a wallet row reads all zeros."""
        row = (
            await db.execute(
                select(PointWallet.available_points, PointWallet.earned_points).where(
                    PointWallet.user_id == user_id
                )
            )
        ).first()
        if row is None:
            return WalletSummary(
                available_points=0, earned_points=0, spendable_points=0
            )
        frozen = await db.scalar(
            select(func.coalesce(func.sum(PointReservation.points), 0)).where(
                PointReservation.user_id == user_id,
                PointReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        assert frozen is not None
        return WalletSummary(
            available_points=int(row.available_points),
            earned_points=int(row.earned_points),
            spendable_points=int(row.available_points) - int(frozen),
        )

    async def grant_assignment_reward(
        self,
        db: AsyncSession,
        *,
        claim_id: UUID,
        user_id: UUID,
        amount: int,
        ranking_effective_at: datetime,
        ranking_dispatcher: RankingProjectionDispatcher | None = None,
        request_id: str | None = None,
    ) -> PointsLedger:
        """Post the claim's ASSIGNMENT_REWARD exactly once (spec §31.6).

        A replay returns the ORIGINAL row — the entry that won the
        UNIQUE(source_type, source_id, ledger_type) race — with nothing
        written. The entry affects both the balance and the ranking and
        carries the caller-supplied lock time as its period attribution
        (spec §17.2).

        The ranking-projection trigger (final-review C1): a provided or
        constructor-default dispatcher is armed on the CALLER's commit
        with the NEWLY written entry's attribution — the review approve
        that lands this grant makes the boards recompute. The idempotent
        replay arms nothing: it wrote nothing, and the original grant's
        own trigger already fired on its commit.
        """
        if amount <= 0:
            raise InvalidLedgerEntryError("amount", "任务奖励必须为正数积分")
        if ranking_effective_at is None:
            raise InvalidLedgerEntryError(
                "ranking_effective_at",
                "任务奖励必须携带排名生效时间（认领的奖励锁时间）",
            )
        dispatcher = self._resolve_dispatcher(ranking_dispatcher)

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
                entry = await self.post_entry(
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
        if dispatcher is not None:
            # Armed only on the success path: this call wrote the entry,
            # so its period needs the recompute. The validated command
            # argument IS the entry's attribution (post_entry stores it
            # verbatim).
            _enqueue_ranking_update_after_commit(
                db,
                dispatcher,
                user_id,
                ranking_effective_at,
                request_id,
            )
        return entry

    async def reverse_assignment_reward(
        self,
        db: AsyncSession,
        actor: Actor,
        ledger_id: UUID,
        reason: str | None,
        *,
        ranking_dispatcher: RankingProjectionDispatcher | None = None,
        request_id: str | None = None,
    ) -> PointsLedger:
        """Post the admin reversal of one ASSIGNMENT_REWARD (spec §17.2).

        The ORIGINAL row is never touched: this writes exactly one new
        ``ASSIGNMENT_REWARD_REVERSAL`` entry — amount ``-original.amount``,
        linked through ``reversal_of_id``, the original's source triple
        under a different ledger_type (UNIQUE makes it one-per-claim),
        both effect flags true (正常作弊冲销), the ORIGINAL's
        ``ranking_effective_at`` (a September decision repairs August and
        all-time, never the current month), the admin as operator, and
        the mandatory reason. The wallet lock (inside ``post_entry``)
        serializes concurrent reversals; an already-spent reward
        overdrafts ``available_points`` negative (migration 0012 ruling,
        models.py). Flush only — the caller owns the transaction, and a
        provided or constructor-default ``ranking_dispatcher`` fires on
        that commit carrying the ORIGINAL's attribution (the §17.2
        payload: the boards repaired are the period the reward was
        credited to, never the decision's period).

        Raises the typed gates in order — reason, actor, target row,
        target type, already-reversed — before anything is written; the
        UNIQUE-race loser maps its violation to the same
        already-reversed error through the savepoint recovery.
        """
        reason_text = reason.strip() if isinstance(reason, str) else ""
        if not reason_text:
            raise RewardReversalReasonRequiredError(ledger_id)
        if actor.role is not Role.ADMIN:
            raise RewardReversalPermissionDeniedError(actor.user_id, actor.role)

        original = await db.get(PointsLedger, ledger_id)
        if original is None:
            raise LedgerEntryNotFoundError(ledger_id)
        if original.ledger_type != LedgerType.ASSIGNMENT_REWARD.value:
            raise LedgerEntryNotReversibleError(ledger_id, original.ledger_type)
        # Captured before any savepoint work: the race-recovery path
        # expires session state, and both the recovery read and the
        # typed error must not depend on refreshing the original to
        # answer (a lazy refresh outside the greenlet would raise
        # MissingGreenlet — the grant path's discipline).
        source_type = original.source_type
        source_id = original.source_id
        user_id = original.user_id
        ranking_effective_at = original.ranking_effective_at
        # An ASSIGNMENT_REWARD is always ranking-affecting, so the
        # ledger's coherence CHECK (ranking_effective_at NOT NULL exactly
        # when affects_ranking, models.py) makes this non-None; the
        # assert documents the invariant the enqueue relies on.
        assert ranking_effective_at is not None

        existing = await db.scalar(self._claim_reversal_filter(source_type, source_id))
        if existing is not None:
            raise RewardAlreadyReversedError(
                ledger_id=ledger_id,
                source_id=source_id,
                reversal_ledger_id=existing.id,
            )

        try:
            # The savepoint bounds the race loser's damage: the UNIQUE
            # violation aborts only this insert, leaving the caller's
            # transaction usable for the recovery read below.
            async with db.begin_nested():
                reversal = await self.post_entry(
                    db,
                    PostLedgerEntry(
                        user_id=user_id,
                        ledger_type=LedgerType.ASSIGNMENT_REWARD_REVERSAL,
                        amount=-original.amount,
                        source_type=source_type,
                        source_id=source_id,
                        affects_balance=True,
                        affects_ranking=True,
                        ranking_effective_at=ranking_effective_at,
                        reversal_of_id=ledger_id,
                        operator_id=actor.user_id,
                        reason=reason_text,
                    ),
                )
        except IntegrityError as exc:
            if _SOURCE_TRIPLE_UQ not in str(exc):
                raise  # unknown database failure, not our one-per-claim race
            # The wallet lock serialized us behind the winner's commit,
            # so the row is visible now. Expire savepoint-scoped state
            # before re-reading (the captured locals above are expiry-
            # proof), then answer with the typed rejection.
            db.expire_all()
            winner = await db.scalar(
                self._claim_reversal_filter(source_type, source_id)
            )
            if winner is None:
                raise
            raise RewardAlreadyReversedError(
                ledger_id=ledger_id,
                source_id=source_id,
                reversal_ledger_id=winner.id,
            ) from exc
        # The post-commit seam, armed only on the success path: a typed
        # rejection or a caller rollback never enqueues an entry that
        # never landed. The payload carries the ORIGINAL's attribution —
        # the captured local is expiry-proof and the closure evaluates
        # it now, before commit.
        dispatcher = self._resolve_dispatcher(ranking_dispatcher)
        if dispatcher is not None:
            _enqueue_ranking_update_after_commit(
                db,
                dispatcher,
                user_id,
                ranking_effective_at,
                request_id,
            )
        return reversal

    async def locked_or_created_wallet(
        self, db: AsyncSession, user_id: UUID
    ) -> PointWallet:
        """The user's wallet row under FOR UPDATE, creating it on the
        first entry. Concurrency-safe by construction: see the module
        docstring's wallet-lifecycle ruling.

        Public because it is the module's ONE wallet-row lock: the
        redemption service takes the same lock (in the same order every
        ledger post does) before recomputing spendability under it, so
        same-user requests serialize against both redemptions and
        balance-changing posts (spec §16.3).
        """
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

    @staticmethod
    def _claim_reversal_filter(
        source_type: str, source_id: UUID
    ) -> Select[tuple[PointsLedger]]:
        """The one-per-claim reversal lookup: the ORIGINAL's source
        triple under the reversal's ledger_type — the same UNIQUE
        constraint's read-side twin (spec §31.6 shape)."""
        return select(PointsLedger).where(
            PointsLedger.source_type == source_type,
            PointsLedger.source_id == source_id,
            PointsLedger.ledger_type == LedgerType.ASSIGNMENT_REWARD_REVERSAL.value,
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
    couples to the review service's key grammar. It IS threaded through
    as the ranking-projection job's ``request_id`` (final-review C1):
    the approve's stable key becomes the recompute job's correlation id
    in the logs, at zero new surface.

    Constructed per request with the session the caller's transaction
    runs on: the grant joins that transaction (§14 step 8) and the
    caller commits. ``ranking_dispatcher`` (final-review C1) is the
    post-commit trigger the composition root binds — the workers'
    ``CeleryRankingDispatcher`` in production, a recording fake in
    tests; ``None`` never enqueues.
    """

    def __init__(
        self,
        *,
        ledger: LedgerService,
        db: AsyncSession,
        ranking_dispatcher: RankingProjectionDispatcher | None = None,
    ) -> None:
        self._ledger = ledger
        self._db = db
        self._ranking_dispatcher = ranking_dispatcher

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
            ranking_dispatcher=self._ranking_dispatcher,
            request_id=idempotency_key,
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
