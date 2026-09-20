# backend/app/modules/points/models.py
"""Points module persistence models (spec §15/§16; invariants §31.6/12/13).

Design decisions:

- Enum-like columns are VARCHAR with explicitly named CHECK constraints,
  not PostgreSQL native enums — same rationale as the tasks and submissions
  modules. The short CHECK names compose with the naming convention in
  `app.db.base` into e.g. `ck_points_ledger_ledger_type` (the 0003 gotcha).
- `PointsLedger` is the auditable source of truth for economic history
  (spec §15) and APPEND-ONLY: rows are never UPDATEd or DELETEd, and every
  correction is a new row linked through `reversal_of_id`. The database
  deliberately does not enforce append-only — no trigger, no rule —
  immutability is a service convention (INSERT-only code paths, no UPDATE
  methods on the ledger service) plus code-review discipline, the same
  ruling as the submission audit tables. The table accordingly declares no
  onupdate columns.
- UNIQUE(source_type, source_id, ledger_type) is the idempotency key for
  ORIGINAL entries (spec §31.6: one ASSIGNMENT_REWARD per Claim). The
  reversal of a claim's reward has a different ledger_type, so original and
  correction fit the same source without colliding; the pair (REWARD_-
  REDEMPTION, REWARD_REDEMPTION_REFUND) works the same way per Redemption.
  ADMIN_ADJUSTMENT rows get a service-generated unique source (each
  adjustment is its own source event).
- `affects_balance` / `affects_ranking` carry no server defaults: every
  insert must state both explicitly (spec §15 — redemption entries hit the
  balance but never the ranking; admin adjustments default
  affects_ranking=false at the service boundary).
- `ranking_effective_at` is mandatory exactly when `affects_ranking` is
  true (the coherence CHECK): the ranking projection attributes a row to a
  period by this timestamp (spec §17.2 — a September reversal of an August
  reward carries August's effective time so it repairs August and all-time
  without touching September). Non-ranking rows leave it NULL.
- `PointWallet` (spec §15.1) is a rebuildable projection, not a second
  source of truth: available_points (spendable balance) and earned_points
  (cumulative task contribution for the total board and honors) are always
  derivable from the ledger, updated in the same transaction as the ledger
  insert, and never negative (spendable-balance invariant §31.12 enforced
  as a CHECK at the database boundary — the reservation service's job is
  only to keep friendly errors ahead of it). `user_id` is the primary key:
  exactly one wallet row per user. Redemption decreases available_points
  only; earned_points and historical ranking contribution are untouched by
  spending (spec §15.1).
- `PointReservation` (spec §16.2) freezes points for an open redemption
  request: spendable = ledger balance - active reservations. The lifecycle
  is ACTIVE -> CONSUMED (approved; the freeze becomes a negative
  REWARD_REDEMPTION ledger row) or ACTIVE -> RELEASED (rejected or timed
  out), transitioned in place — hence UNIQUE(redemption_id): exactly one
  reservation row per redemption, and the coherence CHECK pins
  released_at to exactly the non-ACTIVE states. `points` is positive; the
  sign lives in the ledger entry the reservation eventually produces.
- `RewardItem.stock` is nullable = unlimited (spec §16); zero is a valid
  sold-out state, only negative is rejected (§31.13). The availability
  window is the half-open interval available_from <= now < available_until
  with either side nullable = unbounded in that direction, and the CHECK
  keeps the interval non-degenerate (from < until) so an empty window
  cannot be configured by mistake.
- Reserved stock is DERIVED, not denormalized (controller ruling): there
  is no reserved_stock counter on RewardItem. Occupancy is the count of
  the item's redemptions with status IN (REQUESTED, UNDER_REVIEW,
  APPROVED, FULFILLED) — exactly the member set spec §16.1 counts against
  stock and per-term quota — computed under the RewardItem row lock
  (FOR UPDATE) that serializes redemption, so check-and-insert cannot
  race. REJECTED rows drop out of the count and release their occupancy
  by the status flip alone; FULFILLED converts a pre-occupation into a
  permanent one without a counter to maintain.
- `RewardRedemption.term_key` is the per-request snapshot of the
  admin-configured CURRENT_ACADEMIC_TERM (spec §16.1, e.g. "2026-fall"):
  per-user-term limits count by the snapshot, so switching the current
  term never rewrites history. `points` snapshots the item's point_cost at
  request time for the same reason.
- `decided_at`/`decided_by` record the review decision (either APPROVED or
  REJECTED), `fulfilled_at`/`fulfillment_note` the later physical
  fulfillment (spec §16.2: approving and delivering are separate
  transitions); richer state/coherence rules arrive with the redemption
  service, which owns the transitions.
- No ORM relationships are declared yet; navigation joins arrive with the
  services that need them (backend-engineering §8).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class PointsLedger(Base):
    """One immutable points accounting entry (spec §15).

    Append-only audit history: no UPDATE path exists, no column carries
    onupdate, and corrections are new reversal rows pointing back through
    ``reversal_of_id``. The database deliberately does not enforce
    append-only.
    """

    __tablename__ = "points_ledger"
    __table_args__ = (
        CheckConstraint(
            "ledger_type IN "
            "("
            "'ASSIGNMENT_REWARD', "
            "'ASSIGNMENT_REWARD_REVERSAL', "
            "'REWARD_REDEMPTION', "
            "'REWARD_REDEMPTION_REFUND', "
            "'ADMIN_ADJUSTMENT'"
            ")",
            name="ledger_type",
        ),
        # Signed non-zero integer (spec §15/§31.14): negative amounts are
        # the redemption/reversal/refund shape; zero is ledger noise.
        CheckConstraint("amount <> 0", name="amount"),
        CheckConstraint(
            "NOT affects_ranking OR ranking_effective_at IS NOT NULL",
            name="ranking_effective_required",
        ),
        # Idempotency key for ORIGINAL entries (spec §31.6): one row per
        # source per ledger_type, so a Claim's reward (and, separately, its
        # reversal) can be written at most once.
        UniqueConstraint(
            "source_type",
            "source_id",
            "ledger_type",
            name="uq_points_ledger_source_type_source_id_ledger_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    ledger_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column(BigInteger)
    # Polymorphic source reference (spec §15): 'ASSIGNMENT_CLAIM' -> claim
    # id, 'REWARD_REDEMPTION' -> redemption id, or a service-generated id
    # for ADMIN_ADJUSTMENT; no FK because the targets live in several
    # tables.
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[UUID] = mapped_column()
    # Explicitly set on every insert (no defaults): the entry's effect on
    # the spendable balance and on rankings are independent facts (spec
    # §15/§15.1 — spending never touches historical ranking contribution).
    affects_balance: Mapped[bool] = mapped_column(Boolean)
    affects_ranking: Mapped[bool] = mapped_column(Boolean)
    # Ranking period attribution (spec §17.2); mandatory exactly when
    # affects_ranking is true — see the coherence CHECK above.
    ranking_effective_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Reversal link: this row corrects the row it points to (spec §15).
    reversal_of_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("points_ledger.id"), index=True
    )
    # Who caused the entry beyond the source itself (admin adjustment
    # operator, teacher triggering a reversal); NULL for system flow.
    operator_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    # Mandatory free text for ADMIN_ADJUSTMENT (service-enforced, spec
    # §15); optional context for other types.
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class PointWallet(Base):
    """Spendable-balance projection per user (spec §15.1).

    Rebuildable from the ledger (affects_balance rows sum to
    available_points, ranking-affecting rows to earned_points); updated in
    the same transaction as the ledger insert. ``user_id`` is the primary
    key — exactly one row per user. A mutable projection, so updated_at
    carries onupdate unlike the append-only audit tables.
    """

    __tablename__ = "point_wallets"
    __table_args__ = (
        # Spendable-balance invariant (spec §31.12) at the database
        # boundary: the wallet can never be persisted negative.
        CheckConstraint("available_points >= 0", name="available_points"),
        CheckConstraint("earned_points >= 0", name="earned_points"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    available_points: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    earned_points: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )


class RewardItem(Base):
    """One admin-configured redeemable reward (spec §16).

    Stock is derived-occupancy territory: no reserved counter lives here —
    see the module docstring for the redemption-count derivation.
    """

    __tablename__ = "reward_items"
    __table_args__ = (
        CheckConstraint("point_cost > 0", name="point_cost"),
        # NULL = unlimited (spec §16); zero = sold out; negative never.
        CheckConstraint("stock IS NULL OR stock >= 0", name="stock"),
        CheckConstraint(
            "per_user_term_limit IS NULL OR per_user_term_limit >= 0",
            name="per_user_term_limit",
        ),
        # Half-open window available_from <= now < available_until (spec
        # §16.1); either side NULL = unbounded that direction. A degenerate
        # window (from >= until) can never admit a request, so it is
        # rejected as a configuration error.
        CheckConstraint(
            "available_from IS NULL "
            "OR available_until IS NULL "
            "OR available_from < available_until",
            name="window_ordering",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    point_cost: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int | None] = mapped_column(Integer)
    # NULL = no per-term limit; counted per user + item + term_key snapshot.
    per_user_term_limit: Mapped[int | None] = mapped_column(Integer)
    available_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    requires_manual_review: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    # How staff deliver the reward (e.g. grade-entry instructions).
    fulfillment_instructions: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=func.now()
    )


class RewardRedemption(Base):
    """One user request to redeem a RewardItem (spec §16/§16.1).

    ``term_key`` and ``points`` are request-time snapshots: per-term limits
    count by the snapshot even after the admin switches the current term,
    and later item price edits never rewrite an open request. Stock and
    quota occupancy is this row's status — REQUESTED/UNDER_REVIEW/APPROVED/
    FULFILLED count, REJECTED does not (spec §16.1) — derived under the
    RewardItem row lock that serializes redemption.
    """

    __tablename__ = "reward_redemptions"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('REQUESTED', 'UNDER_REVIEW', 'APPROVED', 'FULFILLED', 'REJECTED')",
            name="status",
        ),
        CheckConstraint("points > 0", name="points"),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    reward_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("reward_items.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), server_default=text("'REQUESTED'"))
    # Snapshot of the admin-configured CURRENT_ACADEMIC_TERM at request
    # time (spec §16.1), e.g. "2026-fall".
    term_key: Mapped[str] = mapped_column(String(64))
    # Snapshot of RewardItem.point_cost at request time.
    points: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # Review decision (APPROVED or REJECTED) and its author.
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    # Physical fulfillment (spec §16.2: approval and delivery are separate
    # transitions — FULFILLED comes later, by staff).
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_note: Mapped[str | None] = mapped_column(Text)


class PointReservation(Base):
    """Points frozen for one open redemption request (spec §16.2).

    Exactly one row per redemption (UNIQUE(redemption_id)): the row is
    created ACTIVE when the request is accepted and transitions in place —
    CONSUMED on approval (the freeze becomes a negative REWARD_REDEMPTION
    ledger row), RELEASED on rejection or timeout. ``released_at`` is set
    exactly when the row leaves ACTIVE (the coherence CHECK).
    """

    __tablename__ = "point_reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'RELEASED', 'CONSUMED')",
            name="status",
        ),
        CheckConstraint("points > 0", name="points"),
        CheckConstraint(
            "(status = 'ACTIVE' AND released_at IS NULL) "
            "OR (status IN ('RELEASED', 'CONSUMED') AND released_at IS NOT NULL)",
            name="release_coherence",
        ),
        UniqueConstraint("redemption_id", name="uq_point_reservations_redemption_id"),
    )

    id: Mapped[UUID] = mapped_column(
        server_default=text("gen_random_uuid()"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    redemption_id: Mapped[UUID] = mapped_column(ForeignKey("reward_redemptions.id"))
    # Positive frozen amount; the sign convention lives in the ledger.
    points: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'ACTIVE'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
