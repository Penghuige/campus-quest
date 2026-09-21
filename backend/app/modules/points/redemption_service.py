# backend/app/modules/points/redemption_service.py
"""Reward redemption reservations: the double-spend/oversell core
(spec §16/§16.1/§16.2/§16.3; plan 05 task 4).

Design decisions:

- **Lock order: wallet row FIRST, then the RewardItem row** (both FOR
  UPDATE). The wallet lock serializes same-user requests — the §16.3
  double-spend race — and it is the SAME lock every ledger post takes
  (``LedgerService.locked_or_created_wallet``), so a request also
  serializes against concurrent grants/reversals that move the balance
  it is about to check. The item lock serializes stock: occupancy is
  derived (COUNT of the item's redemptions in an occupying status —
  there is deliberately no counter, models.py), so the count is only
  sound inside the lock that serializes check-and-insert. Two requests
  for different items dead-lock-free: each holds its own user's wallet
  before touching any item, and cross-user orders never cycle because
  a transaction locks at most one wallet.
- **Spendability is recomputed under the wallet lock.**
  ``get_spendable_points`` is the lock-free advisory read (T2); the
  request path re-derives ``available - SUM(ACTIVE reservations)``
  while holding the wallet row, where the reservation sum is stable —
  every reservation lifecycle transition (creation, CONSUME, RELEASE)
  happens under that same lock. THIS gate is spec §31.12's entire
  enforcement since migration 0012 (task 5's overdraft ruling): the
  wallet's ``available_points >= 0`` CHECK is gone because a reversal
  of already-spent points legitimately overdrafts the projection, so
  redemption must never rely on the database refusing a negative
  balance — it refuses first, under the lock, with the friendly typed
  error.
- **Approve/reject lock redemption -> wallet -> reservation.** The
  redemption row lock serializes the lifecycle; the wallet lock (taken
  AFTER the redemption row) keeps the reservation-sum discipline
  above. No cycle against ``request`` (wallet -> item): request never
  locks an existing redemption row, and the decision paths never lock
  a RewardItem row.
- **Term keys are snapshots, never calendar guesses** (spec §16.1).
  ``AcademicTermProvider.current_term_key()`` is validated (non-blank,
  <= column width) at read time; an unusable key fails the request
  with the typed ``AcademicTermConfigurationError`` — a configuration
  failure, not a user input error, so it is a loud RuntimeError-shaped
  500 (the rbac-unwired precedent) rather than a §29 business code.
  V1's provider is the constructor-arg ``StaticAcademicTermProvider``;
  Plan 08 swaps in the audited CURRENT_ACADEMIC_TERM system setting.
- **Error codes reuse the frozen registry** (interfaces.md): the three
  redemption conflicts (INSUFFICIENT_POINTS / REWARD_OUT_OF_STOCK /
  REDEMPTION_LIMIT_REACHED, 409), NOT_FOUND for missing rows,
  PERMISSION_DENIED for the staff guard, VALIDATION_ERROR for the
  shape/state gates (disabled item, closed window, blank reject
  reason, non-reviewable states). No new code was needed.
- **The review guard is Admin-only until scoped delegation** (PR #2
  hardening ruling on the P0-4 global-staff finding): spec §16.1 routes
  review through Admin-授权-Teacher, but RewardItem carries no owner and
  the授权 model belongs to Plan 08's admin operations — until that
  scoped delegation lands, every ACTIVE+TOTP TEACHER being able to
  decide ANY redemption was judged too broad, so the review decisions
  (approve/reject/fulfill) admit ADMIN only, enforced at BOTH the
  transport guard and this service gate (the community hard-hide
  precedent: a wiring slip cannot widen the surface). Scoping re-widens
  the family here without touching the redemption state machine.
- **Transaction ownership (backend-engineering §5).** Each use case
  commits exactly once on success; typed rejections raised while
  holding locks roll back FIRST (releasing the locks promptly, writing
  nothing), with attribute values captured before the rollback
  expires the ORM instances — the review-service discipline. The
  ledger write inside approve joins THIS transaction through
  ``LedgerService.post_entry`` (flush-only), which is what makes
  "release reservation + post entry + status flip" one unit.
- **Idempotency.** Approve on APPROVED / reject on REJECTED / fulfill
  on FULFILLED return the existing row untouched (the read-only
  transaction still commits to release the row lock); the other
  terminal states are typed 409 rejections — a terminal redemption is
  never resurrected. The UNIQUE(source_type, source_id, ledger_type)
  triple over the redemption id is the database backstop should two
  approvals ever race past the row lock.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.rbac import is_admin, role_value
from app.modules.identity.events import Actor
from app.modules.points.enums import LedgerType, RedemptionStatus, ReservationStatus
from app.modules.points.ledger_service import LedgerService, PostLedgerEntry
from app.modules.points.models import (
    PointReservation,
    RewardItem,
    RewardRedemption,
)

__all__ = [
    "AcademicTermConfigurationError",
    "AcademicTermProvider",
    "InsufficientPointsError",
    "RedemptionLimitReachedError",
    "RedemptionNotFulfillableError",
    "RedemptionNotReviewableError",
    "RedemptionPermissionDeniedError",
    "RedemptionRejectReasonRequiredError",
    "RedemptionService",
    "RedemptionWindowClosedError",
    "RewardItemDisabledError",
    "RewardItemNotFoundError",
    "RewardOutOfStockError",
    "RewardRedemptionNotFoundError",
    "SettingsAcademicTermProvider",
    "StaticAcademicTermProvider",
    "window_open",
]

# The §16.1 occupancy member set: exactly the statuses that hold one
# stock unit and one per-term quota slot (spec §16.1; models.py ruling).
# REJECTED is the only status outside it — the flip alone releases both.
_OCCUPYING_STATUSES: Final[tuple[str, ...]] = (
    RedemptionStatus.REQUESTED.value,
    RedemptionStatus.UNDER_REVIEW.value,
    RedemptionStatus.APPROVED.value,
    RedemptionStatus.FULFILLED.value,
)

# Polymorphic source vocabulary (models.py): a redemption consumption
# entry's source is the redemption itself — the idempotency triple.
_REDEMPTION_SOURCE: Final[str] = "REWARD_REDEMPTION"

# RewardRedemption.term_key column width (models.py): a longer key is a
# configuration failure, not a truncation candidate.
_TERM_KEY_MAX_LENGTH: Final[int] = 64

_PERMISSION_DENIED_MESSAGE = "只有管理员可以审核与发放兑换"


# --- the academic-term port -----------------------------------------------------------


class AcademicTermProvider(Protocol):
    """The current admin-configured academic term key (spec §16.1).

    Sync and side-effect free: the key is configuration state, read
    once per request and snapshotted onto the Redemption. Plan 08
    binds the production implementation to the audited
    CURRENT_ACADEMIC_TERM system setting.
    """

    def current_term_key(self) -> str: ...


class AcademicTermConfigurationError(RuntimeError):
    """CURRENT_ACADEMIC_TERM is unusable (blank or over the column
    width). A deployment/configuration failure, not user input: the
    request fails loudly (500 through the generic handler, the
    rbac-unwired precedent) instead of guessing a calendar term
    (spec §16.1 MUST; plan 05 task 4)."""


def _validated_term_key(term_key: str) -> str:
    """The stripped term key, or the typed configuration error."""
    stripped = term_key.strip() if isinstance(term_key, str) else ""
    if not stripped or len(stripped) > _TERM_KEY_MAX_LENGTH:
        raise AcademicTermConfigurationError(
            f"CURRENT_ACADEMIC_TERM 配置不可用：{term_key!r}（必须是非空白字符串且"
            f"不超过 {_TERM_KEY_MAX_LENGTH} 个字符）；拒绝回退到日历推断的学期"
        )
    return stripped


class StaticAcademicTermProvider:
    """V1 ``AcademicTermProvider``: one fixed term key from the
    constructor (no audited setting exists yet — plan 05 task 4
    ruling; Plan 08 wires the setting-backed provider). An unusable
    key is rejected at construction, so a misconfigured deployment
    fails at wiring time rather than at the first redemption."""

    def __init__(self, term_key: str) -> None:
        self._term_key = _validated_term_key(term_key)

    def current_term_key(self) -> str:
        return self._term_key


class SettingsAcademicTermProvider:
    """Settings-driven ``AcademicTermProvider`` (PR #2 hardening slice
    of plan 08's term configuration): reads ``Settings
    .current_academic_term`` — the dev default is "2026-fall", and a
    deployment turns the term with the CURRENT_ACADEMIC_TERM env var
    until Plan 08 replaces the field with the audited, admin-configurable
    system setting. An unusable key is rejected at construction (the
    StaticAcademicTermProvider wiring-time ruling), so a misconfigured
    deployment fails when the provider is built, not at the first
    redemption. The snapshot semantics are unchanged: a redemption keeps
    the term it was created under even after the setting moves on."""

    def __init__(self, settings: Settings) -> None:
        self._term_key = _validated_term_key(settings.current_academic_term)

    def current_term_key(self) -> str:
        return self._term_key


# --- typed exceptions (router-mapped) ------------------------------------------------


class RewardItemNotFoundError(BusinessError):
    """No RewardItem row for the id (the claim-service 404 shape)."""

    def __init__(self, reward_item_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            "兑换奖品不存在",
            status_code=404,
            details={"reward_item_id": str(reward_item_id)},
        )


class RewardRedemptionNotFoundError(BusinessError):
    """No RewardRedemption row for the id."""

    def __init__(self, redemption_id: UUID) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            "兑换申请不存在",
            status_code=404,
            details={"redemption_id": str(redemption_id)},
        )


class RewardItemDisabledError(BusinessError):
    """The item is switched off (spec §16.1: 检查 enabled)."""

    def __init__(self, reward_item_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "该奖品当前不可兑换",
            status_code=422,
            details={"reward_item_id": str(reward_item_id), "reason": "disabled"},
        )


class RedemptionWindowClosedError(BusinessError):
    """``now`` is outside the half-open window
    available_from <= now < available_until (spec §16.1)."""

    def __init__(
        self,
        reward_item_id: UUID,
        *,
        available_from: datetime | None,
        available_until: datetime | None,
    ) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "该奖品不在可兑换时间窗口内",
            status_code=422,
            details={
                "reward_item_id": str(reward_item_id),
                "reason": "window_closed",
                "available_from": available_from.isoformat()
                if available_from is not None
                else None,
                "available_until": available_until.isoformat()
                if available_until is not None
                else None,
            },
        )


class RewardOutOfStockError(BusinessError):
    """Derived occupancy already equals the configured stock (spec
    §16.1/§16.3: 有限库存防 oversell)."""

    def __init__(self, reward_item_id: UUID, stock: int) -> None:
        super().__init__(
            ErrorCode.REWARD_OUT_OF_STOCK,
            "该奖品库存不足",
            status_code=409,
            details={"reward_item_id": str(reward_item_id), "stock": stock},
        )


class RedemptionLimitReachedError(BusinessError):
    """The user hit per_user_term_limit for this item under the
    snapshotted term key (spec §16.1)."""

    def __init__(self, reward_item_id: UUID, limit: int, term_key: str) -> None:
        super().__init__(
            ErrorCode.REDEMPTION_LIMIT_REACHED,
            "已达到该奖品本学期的兑换上限",
            status_code=409,
            details={
                "reward_item_id": str(reward_item_id),
                "limit": limit,
                "term_key": term_key,
            },
        )


class InsufficientPointsError(BusinessError):
    """Spendable balance (available - ACTIVE reservations) cannot cover
    the cost (spec §16.1/§16.2/§16.3). The deciding comparison runs on
    the RAW spendable (a reward reversal can overdraft it negative),
    but the public detail clamps at 0 — user-facing surfaces never
    render a negative spendable (spec §15.1 display rule, PR #2
    closure review)."""

    def __init__(self, *, required: int, spendable: int) -> None:
        super().__init__(
            ErrorCode.INSUFFICIENT_POINTS,
            "可用积分不足",
            status_code=409,
            details={"required": required, "spendable": max(spendable, 0)},
        )


class RedemptionPermissionDeniedError(BusinessError):
    """The actor is not ADMIN — the review guard until scoped delegation
    lands (PR #2 hardening ruling; see the module docstring)."""

    def __init__(self, actor_id: UUID, role: str | Enum) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            _PERMISSION_DENIED_MESSAGE,
            status_code=403,
            details={"actor_id": str(actor_id), "role": role_value(role)},
        )


class RedemptionRejectReasonRequiredError(BusinessError):
    """A rejection without a non-blank reason (the §16.2 review
    context; blank is not a reason)."""

    def __init__(self, redemption_id: UUID) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "拒绝兑换申请必须填写原因",
            status_code=422,
            details={"redemption_id": str(redemption_id), "field": "reason"},
        )


class RedemptionNotReviewableError(BusinessError):
    """The redemption is in a terminal state this decision cannot be
    applied to (e.g. reject after approve, decide after fulfill) — no
    review action may resurrect a terminal redemption."""

    def __init__(self, redemption_id: UUID, status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "该兑换申请已处于终态，无法执行该操作",
            status_code=409,
            details={"redemption_id": str(redemption_id), "status": status},
        )


class RedemptionNotFulfillableError(BusinessError):
    """Only an APPROVED redemption can be fulfilled (spec §16.2:
    approval and delivery are separate transitions, in that order)."""

    def __init__(self, redemption_id: UUID, status: str) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "只有已批准的兑换申请可以发放",
            status_code=409,
            details={"redemption_id": str(redemption_id), "status": status},
        )


def window_open(item: RewardItem, now: datetime) -> bool:
    """The half-open window available_from <= now < available_until;
    either bound NULL = unbounded in that direction (spec §16.1).

    Public since plan 05 task 8: the student rewards listing computes
    the same server-side verdict for display, and the verdict must be
    ONE rule — a listing that disagreed with the request gate would
    advertise an unredeemable item."""
    after_start = item.available_from is None or now >= item.available_from
    before_end = item.available_until is None or now < item.available_until
    return after_start and before_end


# --- the service ----------------------------------------------------------------------


class RedemptionService:
    """Owns the redemption lifecycle: atomic point/stock reservation
    (spec §16/§16.1/§16.3) and the review transitions (spec §16.2).

    ``clock`` is the only time source (backend-engineering §11);
    ``terms`` supplies the snapshotted term key; ``ledger`` defaults
    to a fresh ``LedgerService`` (stateless) and exists for tests and
    future wiring symmetry.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        terms: AcademicTermProvider,
        ledger: LedgerService | None = None,
    ) -> None:
        self._clock = clock
        self._terms = terms
        self._ledger = ledger if ledger is not None else LedgerService()

    # -- request: the atomic reservation (§16.1 申请时 checklist) -------------------

    async def request_redemption(
        self,
        db: AsyncSession,
        user_id: UUID,
        reward_item_id: UUID,
    ) -> RewardRedemption:
        """Freeze the points and pre-occupy one stock unit atomically.

        Gates in order (spec §16.1): term key usable, item exists and
        enabled, window open, stock available, per-term quota open,
        spendable covers the cost — then the Redemption (REQUESTED,
        term/price snapshotted) and its ACTIVE PointReservation land
        in ONE transaction. Commits exactly once.
        """
        term_key = _validated_term_key(self._terms.current_term_key())

        # Lock order WHY: the wallet row first serializes same-user
        # requests (§16.3) against redemptions AND balance-moving
        # ledger posts; the item row second serializes stock for the
        # derived occupancy count below. See the module docstring for
        # the no-deadlock argument.
        wallet = await self._ledger.locked_or_created_wallet(db, user_id)
        item = await db.scalar(
            select(RewardItem)
            .where(RewardItem.id == reward_item_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if item is None:
            await db.rollback()  # release the wallet lock, write nothing
            raise RewardItemNotFoundError(reward_item_id)
        if not item.enabled:
            item_id = item.id
            await db.rollback()
            raise RewardItemDisabledError(item_id)

        # Sampled after every lock, before every consumer (the
        # review-service discipline): the window verdict belongs to
        # one instant.
        now = self._clock.now()
        if not window_open(item, now):
            item_id, available_from, available_until = (
                item.id,
                item.available_from,
                item.available_until,
            )
            await db.rollback()
            raise RedemptionWindowClosedError(
                item_id,
                available_from=available_from,
                available_until=available_until,
            )

        if item.stock is not None:
            occupancy = await db.scalar(
                select(func.count())
                .select_from(RewardRedemption)
                .where(
                    RewardRedemption.reward_item_id == item.id,
                    RewardRedemption.status.in_(_OCCUPYING_STATUSES),
                )
            )
            assert occupancy is not None
            if occupancy >= item.stock:
                item_id, stock = item.id, item.stock
                await db.rollback()
                raise RewardOutOfStockError(item_id, stock)

        if item.per_user_term_limit is not None:
            used = await db.scalar(
                select(func.count())
                .select_from(RewardRedemption)
                .where(
                    RewardRedemption.user_id == user_id,
                    RewardRedemption.reward_item_id == item.id,
                    RewardRedemption.term_key == term_key,
                    RewardRedemption.status.in_(_OCCUPYING_STATUSES),
                )
            )
            assert used is not None
            if used >= item.per_user_term_limit:
                item_id, limit = item.id, item.per_user_term_limit
                await db.rollback()
                raise RedemptionLimitReachedError(item_id, limit, term_key)

        # Spendability recomputed UNDER the wallet lock: get_spendable
        # is the advisory read (T2), this is the deciding one. The
        # ACTIVE sum is stable here — every reservation transition
        # holds this lock.
        frozen = await db.scalar(
            select(func.coalesce(func.sum(PointReservation.points), 0)).where(
                PointReservation.user_id == user_id,
                PointReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        assert frozen is not None
        spendable = wallet.available_points - int(frozen)
        if spendable < item.point_cost:
            required = item.point_cost
            await db.rollback()
            raise InsufficientPointsError(required=required, spendable=spendable)

        redemption = RewardRedemption(
            user_id=user_id,
            reward_item_id=item.id,
            status=RedemptionStatus.REQUESTED.value,
            term_key=term_key,
            points=item.point_cost,
        )
        db.add(redemption)
        await db.flush()  # server-generated id for the reservation's FK
        db.add(
            PointReservation(
                user_id=user_id,
                redemption_id=redemption.id,
                points=item.point_cost,
                status=ReservationStatus.ACTIVE.value,
            )
        )
        await db.commit()
        return redemption

    # -- approve (§16.2 审核通过) ----------------------------------------------------

    async def approve_redemption(
        self,
        db: AsyncSession,
        actor: Actor,
        redemption_id: UUID,
    ) -> RewardRedemption:
        """Consume the freeze into ONE negative REWARD_REDEMPTION entry.

        Releases the reservation (CONSUMED), posts the ledger entry
        (affects_balance=true, affects_ranking=false — spec §17.1:
        spending never ranks) with the source triple over the
        redemption id, flips the status to APPROVED, and leaves the
        stock unit occupied (FULFILLED makes it permanent later). An
        already-APPROVED replay returns the row untouched; any other
        terminal state is a typed rejection.
        """
        self._require_staff(actor)
        redemption = await self._locked_redemption(db, redemption_id)
        status = RedemptionStatus(redemption.status)
        if status is RedemptionStatus.APPROVED:
            # Idempotent replay: write nothing, still commit to release
            # the row lock (the review-service discipline).
            await db.commit()
            return redemption
        if status not in (RedemptionStatus.REQUESTED, RedemptionStatus.UNDER_REVIEW):
            observed = redemption.status
            await db.rollback()
            raise RedemptionNotReviewableError(redemption_id, observed)

        now = self._clock.now()
        # Lock order: redemption -> wallet -> reservation. The wallet
        # lock keeps the ACTIVE-sum discipline (see request); taking it
        # after the redemption row cannot cycle with request's
        # wallet -> item order (request never locks a redemption row).
        await self._ledger.locked_or_created_wallet(db, redemption.user_id)
        reservation = await self._locked_reservation(db, redemption.id)

        # The flush-only post joins THIS transaction (backend-
        # engineering §5): entry + wallet decrement + status flips are
        # one unit. The UNIQUE source triple is the replay backstop.
        await self._ledger.post_entry(
            db,
            PostLedgerEntry(
                user_id=redemption.user_id,
                ledger_type=LedgerType.REWARD_REDEMPTION,
                amount=-redemption.points,
                source_type=_REDEMPTION_SOURCE,
                source_id=redemption.id,
                affects_balance=True,
                affects_ranking=False,
                operator_id=actor.user_id,
            ),
        )
        reservation.status = ReservationStatus.CONSUMED.value
        reservation.released_at = now
        redemption.status = RedemptionStatus.APPROVED.value
        redemption.decided_at = now
        redemption.decided_by = actor.user_id
        await db.flush()
        await db.commit()
        return redemption

    # -- reject (§16.2 审核拒绝) ----------------------------------------------------

    async def reject_redemption(
        self,
        db: AsyncSession,
        actor: Actor,
        redemption_id: UUID,
        reason: str,
    ) -> RewardRedemption:
        """Release the points and the stock unit without consuming
        anything: reservation RELEASED, status REJECTED (the derivation
        drops it from occupancy and quota), and deliberately NO ledger
        entry (spec §16.2: 拒绝不产生消费负流水). The reason is
        mandatory. An already-REJECTED replay returns the row.
        """
        reason_text = reason.strip() if isinstance(reason, str) else ""
        if not reason_text:
            # Before any lock: a shape error, not a state conflict.
            raise RedemptionRejectReasonRequiredError(redemption_id)
        self._require_staff(actor)
        redemption = await self._locked_redemption(db, redemption_id)
        status = RedemptionStatus(redemption.status)
        if status is RedemptionStatus.REJECTED:
            await db.commit()  # idempotent replay: release the lock
            return redemption
        if status not in (RedemptionStatus.REQUESTED, RedemptionStatus.UNDER_REVIEW):
            observed = redemption.status
            await db.rollback()
            raise RedemptionNotReviewableError(redemption_id, observed)

        now = self._clock.now()
        await self._ledger.locked_or_created_wallet(db, redemption.user_id)
        reservation = await self._locked_reservation(db, redemption.id)
        reservation.status = ReservationStatus.RELEASED.value
        reservation.released_at = now
        redemption.status = RedemptionStatus.REJECTED.value
        redemption.decided_at = now
        redemption.decided_by = actor.user_id
        await db.flush()
        await db.commit()
        return redemption

    # -- fulfill (§16.2 发放) ---------------------------------------------------------

    async def fulfill_redemption(
        self,
        db: AsyncSession,
        actor: Actor,
        redemption_id: UUID,
        note: str | None = None,
    ) -> RewardRedemption:
        """Record the physical delivery of an APPROVED redemption
        (spec §16.2: approval and delivery are separate transitions).
        Idempotent: a replay on a FULFILLED row is a no-op that keeps
        the original fulfilled_at/note; anything not APPROVED (or
        already FULFILLED by a different path) is a typed rejection.
        Touches no points — fulfillment never re-opens the wallet.
        """
        self._require_staff(actor)
        redemption = await self._locked_redemption(db, redemption_id)
        status = RedemptionStatus(redemption.status)
        if status is RedemptionStatus.FULFILLED:
            await db.commit()  # idempotent replay: release the lock
            return redemption
        if status is not RedemptionStatus.APPROVED:
            observed = redemption.status
            await db.rollback()
            raise RedemptionNotFulfillableError(redemption_id, observed)

        note_text = note.strip() if note is not None and note.strip() else None
        redemption.status = RedemptionStatus.FULFILLED.value
        redemption.fulfilled_at = self._clock.now()
        redemption.fulfillment_note = note_text
        await db.flush()
        await db.commit()
        return redemption

    # -- reads (plan 05 task 8: the listing surfaces) --------------------------------

    async def list_reward_items(
        self, db: AsyncSession, *, include_disabled: bool = False
    ) -> list[RewardItem]:
        """The redeemable catalogue for the student listing (spec §16):
        enabled items only by default (``include_disabled`` serves the
        future admin catalogue view), stable by name then id so the
        shelf does not reshuffle between requests. Read-only; the
        window verdict is the caller's to compute with ``window_open``
        at its own clock instant."""
        stmt = select(RewardItem)
        if not include_disabled:
            stmt = stmt.where(RewardItem.enabled.is_(True))
        stmt = stmt.order_by(RewardItem.name, RewardItem.id)
        return list((await db.scalars(stmt)).all())

    async def list_redemptions(
        self,
        db: AsyncSession,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[RewardRedemption, str]], int]:
        """The staff review queue: ``(redemption, item_name)`` rows,
        OLDEST FIRST (the fair review order), offset-paginated with the
        total.

        ``statuses=None`` (the default) is the PENDING set — REQUESTED
        and UNDER_REVIEW, the decisions that still await a reviewer
        (spec §16.1); an explicit tuple lets the surface find, e.g.,
        APPROVED-but-unfulfilled rows for the fulfillment pass. A
        REJECTED row never appears in the default queue — the flip
        itself released it (spec §16.1)."""
        if statuses is None:
            statuses = (
                RedemptionStatus.REQUESTED.value,
                RedemptionStatus.UNDER_REVIEW.value,
            )
        filters = (RewardRedemption.status.in_(statuses),)
        total = cast(
            "int",
            await db.scalar(
                select(func.count()).select_from(RewardRedemption).where(*filters)
            ),
        )
        rows = (
            await db.execute(
                select(RewardRedemption, RewardItem.name)
                .join(RewardItem, RewardRedemption.reward_item_id == RewardItem.id)
                .where(*filters)
                .order_by(RewardRedemption.created_at, RewardRedemption.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return [(redemption, item_name) for redemption, item_name in rows], total

    # -- shared helpers ----------------------------------------------------------------

    @staticmethod
    def _require_staff(actor: Actor) -> None:
        """The review guard: ADMIN only (PR #2 hardening ruling — see the
        module docstring for why the staff family narrows before scoped
        delegation arrives; the transport mounts the matching
        ``require_admin_actor`` composition)."""
        if not is_admin(actor.role):
            raise RedemptionPermissionDeniedError(actor.user_id, actor.role)

    @staticmethod
    async def _locked_redemption(
        db: AsyncSession, redemption_id: UUID
    ) -> RewardRedemption:
        """The redemption row under FOR UPDATE — the lifecycle
        serializer every decision path enters through."""
        redemption = await db.scalar(
            select(RewardRedemption)
            .where(RewardRedemption.id == redemption_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if redemption is None:
            raise RewardRedemptionNotFoundError(redemption_id)
        return redemption

    @staticmethod
    async def _locked_reservation(
        db: AsyncSession, redemption_id: UUID
    ) -> PointReservation:
        """The redemption's exactly-one reservation row (UNIQUE
        constraint, models.py) under FOR UPDATE, inside the caller's
        redemption-row lock."""
        reservation = await db.scalar(
            select(PointReservation)
            .where(PointReservation.redemption_id == redemption_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reservation is None:
            # Unreachable while every accepted request writes the pair
            # in one transaction; fail loudly instead of fabricating a
            # freeze (the validation-service unreachable precedent).
            raise RuntimeError(
                f"redemption {redemption_id} has no reservation row; "
                "the request transaction that created it was incomplete"
            )
        return reservation
