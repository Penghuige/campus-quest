# backend/app/modules/points/router.py
"""Points wallet, reward catalogue, redemption, and redemption-review
HTTP APIs: thin routes + the module's composition root (spec §15/§16/
§16.1/§16.2, §17.1, §28 URL shapes, §29 envelope, §32 Idempotency-Key,
§40 privacy; plan 05 task 8; backend-engineering §3/§9/§16).

Every route is thin — resolve the actor guard, call one service,
serialize an explicit DTO — and owns no persistence logic. The design
decisions that live here:

Endpoints
---------

Student surfaces (``require_active_student_actor`` — spec §4.1:
spending points and browsing rewards are Student capabilities):

===========  =========================================================
Method path  Purpose
===========  =========================================================
GET          ``/points/me`` — the wallet display strip: available /
             earned / spendable (available minus ACTIVE freezes).
GET          ``/rewards`` — the enabled reward catalogue with the
             redemption window's open/closed verdict computed
             SERVER-SIDE at the business clock (never trusted from
             the client).
POST         ``/rewards/{reward_id}/redeem`` — the atomic freeze
             (201); the item's stock/quota/spendability gates answer
             the frozen typed-error envelopes (409 family).
===========  =========================================================

Teacher/Admin review surfaces (``require_staff_management_actor`` —
spec §4.2-§4.3, §33.4; the tasks/submissions staff-surface precedent,
so the ``/teacher`` prefix is the module's management namespace):

===========  =========================================================
GET          ``/teacher/rewards/redemptions`` — the review queue:
             pending (REQUESTED/UNDER_REVIEW) oldest first,
             offset-paginated, enriched with the requester nickname
             (through the identity directory port) and the item name.
POST         ``/teacher/rewards/redemptions/{id}/approve`` — consume
             the freeze into one negative REWARD_REDEMPTION entry.
POST         ``/teacher/rewards/redemptions/{id}/reject`` — release
             the freeze; ``reason`` is mandatory at the transport.
POST         ``/teacher/rewards/redemptions/{id}/fulfill`` — record
             the physical delivery (optional note) of an APPROVED
             redemption.
===========  =========================================================

Other transport decisions
-------------------------

- **Idempotency-Key is ADVISORY in V1 (spec §32 / plan 05 task 8
  ruling).** The redeem route ACCEPTS the header so clients can send
  one from day one, but does not dedupe on it: the duplicate-side-
  effect protection is the database's — one reservation row per
  redemption (UNIQUE), the wallet-row lock serializing the freeze,
  and the one-entry-per-source ledger constraint. Two requests with
  the same key are therefore two redemptions; replay-stable dedupe
  keyed on the header arrives with the audited idempotency store.
- **Status-code mapping is the services' table.** Every typed
  exception below subclasses ``BusinessError`` with its frozen
  code/status, so the core envelope handler renders them unchanged —
  no module-specific handler registration is needed.
- **Privacy by DTO construction (spec §40).** The student redemption
  DTO carries no requester identity beyond what the caller already
  knows (it IS the requester); the staff queue enriches with the
  nickname through ``UserDirectory`` — never identity ORM models —
  and carries no contact field because the DTO has none.
- **The academic-term provider is the documented V1 stand-in.** The
  redemption service requires an ``AcademicTermProvider``; until Plan
  08 wires the audited CURRENT_ACADEMIC_TERM setting, this
  composition root binds ``StaticAcademicTermProvider`` with the
  module constant below (validated at construction, so a bad value
  fails at wiring time — the service's own ruling).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.db.session import get_db_session
from app.modules.identity.dependencies import (
    get_business_clock,
    require_active_student_actor,
    require_staff_management_actor,
)
from app.modules.identity.directory import SqlAlchemyUserDirectory
from app.modules.identity.events import Actor
from app.modules.points.ledger_service import LedgerService
from app.modules.points.models import RewardItem, RewardRedemption
from app.modules.points.redemption_service import (
    RedemptionService,
    StaticAcademicTermProvider,
    window_open,
)

# --- pagination bounds (the documented offset choice) --------------------------------

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50

# Spec §16.1's example key; the documented V1 stand-in the composition
# root binds until Plan 08's audited CURRENT_ACADEMIC_TERM setting.
DEFAULT_TERM_KEY = "2026-fall"

# --- transport DTOs (explicit field sets) ---------------------------------------------


class WalletResponse(BaseModel):
    """The wallet strip (spec §15.1/§16.2): the projection's figures
    plus the spendable derivation."""

    model_config = ConfigDict(extra="forbid")

    available_points: int
    earned_points: int
    spendable_points: int


class RewardItemResponse(BaseModel):
    """One catalogue row for the student listing (spec §16/§42). The
    admin-only ``fulfillment_instructions`` and the enabled flag (always
    true in this listing) stay server-side."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    description: str | None
    point_cost: int
    stock: int | None
    per_user_term_limit: int | None
    available_from: datetime | None
    available_until: datetime | None
    requires_manual_review: bool
    window_open: bool


class RewardsListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RewardItemResponse]


class RedemptionResponse(BaseModel):
    """The student's view of one redemption (spec §16.1): request-time
    snapshots plus lifecycle state."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    reward_item_id: UUID
    status: str
    points: int
    term_key: str
    created_at: datetime


class RedemptionReviewResponse(RedemptionResponse):
    """The staff view: the student fields plus the review/fulfillment
    trail and the display-name enrichment (nickname only — the directory
    port's shape, no contact fields to leak)."""

    requester_nickname: str | None
    item_name: str
    decided_at: datetime | None = None
    fulfilled_at: datetime | None = None
    fulfillment_note: str | None = None


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RedemptionReviewResponse]
    total: int
    limit: int
    offset: int


class RedemptionRejectRequest(BaseModel):
    """A rejection's mandatory context (spec §16.2): blank is not a
    reason — the service re-validates after the strip."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class RedemptionFulfillRequest(BaseModel):
    """The optional delivery note (spec §16.2: approval and delivery are
    separate transitions)."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


# --- provider dependencies (module composition root) ---------------------------------


def get_ledger_service() -> LedgerService:
    return LedgerService()


def get_academic_term_provider() -> StaticAcademicTermProvider:
    """The V1 term binding (see the module docstring): one static key,
    validated at construction so a misconfiguration fails at wiring
    time rather than at the first redemption."""
    return StaticAcademicTermProvider(DEFAULT_TERM_KEY)


def get_redemption_service(
    clock: Annotated[Clock, Depends(get_business_clock)],
    terms: Annotated[StaticAcademicTermProvider, Depends(get_academic_term_provider)],
) -> RedemptionService:
    return RedemptionService(clock=clock, terms=terms)


def get_user_directory() -> SqlAlchemyUserDirectory:
    return SqlAlchemyUserDirectory()


ClockDep = Annotated[Clock, Depends(get_business_clock)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
StudentActor = Annotated[Actor, Depends(require_active_student_actor)]
StaffActor = Annotated[Actor, Depends(require_staff_management_actor)]
LedgerServiceDep = Annotated[LedgerService, Depends(get_ledger_service)]
RedemptionServiceDep = Annotated[RedemptionService, Depends(get_redemption_service)]
DirectoryDep = Annotated[SqlAlchemyUserDirectory, Depends(get_user_directory)]

PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)]
PageOffset = Annotated[int, Query(ge=0)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

router = APIRouter()


# --- student surfaces (spec §16, §28, §42) -------------------------------------------


@router.get("/points/me", response_model=WalletResponse)
async def my_wallet(
    actor: StudentActor,
    db: DbSession,
    ledger: LedgerServiceDep,
) -> WalletResponse:
    """The caller's wallet strip: available (spendable-balance
    projection), earned (cumulative task contribution — spending never
    touches it, spec §17.1), and spendable (available minus ACTIVE
    freezes, spec §16.2)."""
    summary = await ledger.get_wallet_summary(db, actor.user_id)
    return WalletResponse(
        available_points=summary.available_points,
        earned_points=summary.earned_points,
        spendable_points=summary.spendable_points,
    )


@router.get("/rewards", response_model=RewardsListResponse)
async def list_rewards(
    actor: StudentActor,
    db: DbSession,
    redemptions: RedemptionServiceDep,
    clock: ClockDep,
) -> RewardsListResponse:
    """The enabled reward catalogue with the server-side window verdict
    (spec §16.1): ``window_open`` is computed HERE at the business
    clock, the same half-open rule the redeem gate enforces, so the
    shelf can never advertise an item the gate would refuse."""
    items = await redemptions.list_reward_items(db)
    now = clock.now()
    return RewardsListResponse(
        items=[_item_response(item, window_open(item, now)) for item in items]
    )


def _item_response(item: RewardItem, open_now: bool) -> RewardItemResponse:
    return RewardItemResponse(
        id=item.id,
        name=item.name,
        description=item.description,
        point_cost=item.point_cost,
        stock=item.stock,
        per_user_term_limit=item.per_user_term_limit,
        available_from=item.available_from,
        available_until=item.available_until,
        requires_manual_review=item.requires_manual_review,
        window_open=open_now,
    )


@router.post(
    "/rewards/{reward_id}/redeem", response_model=RedemptionResponse, status_code=201
)
async def redeem_reward(
    reward_id: UUID,
    actor: StudentActor,
    db: DbSession,
    redemptions: RedemptionServiceDep,
    idempotency_key: IdempotencyKey = None,
) -> RedemptionResponse:
    """Request one redemption: the service freezes the points and
    pre-occupies the stock unit atomically (spec §16.1) and answers the
    typed conflict envelopes on any gate failure.

    ``Idempotency-Key`` is ADVISORY in V1 (spec §32; see the module
    docstring): accepted, never deduped on — the database constraints
    are the duplicate-side-effect protection.
    """
    redemption = await redemptions.request_redemption(db, actor.user_id, reward_id)
    # ``created_at`` is a server default the INSERT did not return, so
    # the row is refreshed before serialization (the tasks-router
    # create precedent).
    await db.refresh(redemption)
    return _redemption_response(redemption)


def _redemption_response(redemption: RewardRedemption) -> RedemptionResponse:
    return RedemptionResponse(
        id=redemption.id,
        reward_item_id=redemption.reward_item_id,
        status=redemption.status,
        points=redemption.points,
        term_key=redemption.term_key,
        created_at=redemption.created_at,
    )


# --- teacher/admin review surfaces (spec §16.2, §28, §33.4) ---------------------------


@router.get("/teacher/rewards/redemptions", response_model=ReviewQueueResponse)
async def list_redemption_queue(
    actor: StaffActor,
    db: DbSession,
    redemptions: RedemptionServiceDep,
    directory: DirectoryDep,
    limit: PageLimit = DEFAULT_PAGE_LIMIT,
    offset: PageOffset = 0,
) -> ReviewQueueResponse:
    """The review queue: pending redemptions (REQUESTED/UNDER_REVIEW)
    oldest first, with the requester's display nickname through the
    identity directory port and the item name."""
    rows, total = await redemptions.list_redemptions(db, limit=limit, offset=offset)
    items: list[RedemptionReviewResponse] = []
    for redemption, item_name in rows:
        profile = await directory.get_display_profile(db, redemption.user_id)
        items.append(
            RedemptionReviewResponse(
                id=redemption.id,
                reward_item_id=redemption.reward_item_id,
                status=redemption.status,
                points=redemption.points,
                term_key=redemption.term_key,
                created_at=redemption.created_at,
                requester_nickname=profile.nickname if profile else None,
                item_name=item_name,
            )
        )
    return ReviewQueueResponse(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/teacher/rewards/redemptions/{redemption_id}/approve",
    response_model=RedemptionReviewResponse,
)
async def approve_redemption(
    redemption_id: UUID,
    actor: StaffActor,
    db: DbSession,
    redemptions: RedemptionServiceDep,
    directory: DirectoryDep,
) -> RedemptionReviewResponse:
    """Approve: the freeze becomes one negative REWARD_REDEMPTION entry
    and the status flips to APPROVED (spec §16.2); a replay on an
    already-approved row is the idempotent no-op that returns it."""
    redemption = await redemptions.approve_redemption(db, actor, redemption_id)
    return await _review_response(db, redemptions, directory, redemption)


@router.post(
    "/teacher/rewards/redemptions/{redemption_id}/reject",
    response_model=RedemptionReviewResponse,
)
async def reject_redemption(
    redemption_id: UUID,
    body: RedemptionRejectRequest,
    actor: StaffActor,
    db: DbSession,
    redemptions: RedemptionServiceDep,
    directory: DirectoryDep,
) -> RedemptionReviewResponse:
    """Reject with a mandatory reason: the freeze is released and NO
    consumption entry is written (spec §16.2)."""
    redemption = await redemptions.reject_redemption(
        db, actor, redemption_id, body.reason
    )
    return await _review_response(db, redemptions, directory, redemption)


@router.post(
    "/teacher/rewards/redemptions/{redemption_id}/fulfill",
    response_model=RedemptionReviewResponse,
)
async def fulfill_redemption(
    redemption_id: UUID,
    body: RedemptionFulfillRequest,
    actor: StaffActor,
    db: DbSession,
    redemptions: RedemptionServiceDep,
    directory: DirectoryDep,
) -> RedemptionReviewResponse:
    """Record the physical delivery of an APPROVED redemption (spec
    §16.2: approval and delivery are separate transitions)."""
    redemption = await redemptions.fulfill_redemption(
        db, actor, redemption_id, body.note
    )
    return await _review_response(db, redemptions, directory, redemption)


async def _review_response(
    db: AsyncSession,
    redemptions: RedemptionService,
    directory: SqlAlchemyUserDirectory,
    redemption: RewardRedemption,
) -> RedemptionReviewResponse:
    """The staff DTO for one redemption: the row's own fields plus the
    item name (re-read from the catalogue) and the requester's display
    nickname through the directory port."""
    item = await db.get(RewardItem, redemption.reward_item_id)
    profile = await directory.get_display_profile(db, redemption.user_id)
    return RedemptionReviewResponse(
        id=redemption.id,
        reward_item_id=redemption.reward_item_id,
        status=redemption.status,
        points=redemption.points,
        term_key=redemption.term_key,
        created_at=redemption.created_at,
        requester_nickname=profile.nickname if profile else None,
        item_name=item.name if item is not None else "",
        decided_at=redemption.decided_at,
        fulfilled_at=redemption.fulfilled_at,
        fulfillment_note=redemption.fulfillment_note,
    )
