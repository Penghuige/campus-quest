# backend/app/modules/points/enums.py
"""Points module enums frozen by docs/architecture/interfaces.md.

Members and values are canonical (`value == member name`); database columns
persist the exact string as VARCHAR + CHECK constraints (see models.py for
why they are not PostgreSQL native enums).

`ReservationStatus` is this module's own closed universe (ACTIVE/RELEASED/
CONSUMED, spec §16.2): unlike the two interfaces.md-frozen enums it is not
referenced across modules, so it is defined here rather than frozen in the
cross-plan contract document.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "LedgerType",
    "RedemptionStatus",
    "ReservationStatus",
]


class LedgerType(StrEnum):
    """Ledger entry types (spec §15 typical types; frozen by
    interfaces.md).

    ASSIGNMENT_REWARD and its REVERSAL pair per Claim; REWARD_REDEMPTION
    and its REFUND pair per Redemption; ADMIN_ADJUSTMENT is the manual
    correction channel (must carry a reason, defaults
    affects_ranking=false).
    """

    ASSIGNMENT_REWARD = "ASSIGNMENT_REWARD"
    ASSIGNMENT_REWARD_REVERSAL = "ASSIGNMENT_REWARD_REVERSAL"
    REWARD_REDEMPTION = "REWARD_REDEMPTION"
    REWARD_REDEMPTION_REFUND = "REWARD_REDEMPTION_REFUND"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"


class RedemptionStatus(StrEnum):
    """Reward redemption states (spec §16.1; frozen by interfaces.md).

    REQUESTED -> UNDER_REVIEW -> APPROVED -> FULFILLED, with REJECTED as
    the other terminal decision. REQUESTED/UNDER_REVIEW/APPROVED/FULFILLED
    occupy reward stock and per-term quota; REJECTED releases them.
    """

    REQUESTED = "REQUESTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    FULFILLED = "FULFILLED"
    REJECTED = "REJECTED"


class ReservationStatus(StrEnum):
    """Point reservation lifecycle (spec §16.2).

    ACTIVE while the redemption request holds the points frozen; CONSUMED
    when the review approves (the freeze becomes a REWARD_REDEMPTION
    ledger row); RELEASED when the review rejects. An expiry-release for
    stale requests is an OPEN PRODUCT DECISION — interfaces.md's
    RedemptionStatus ruling forbids adding an auto-cancel/timeout rule
    without a new owner ruling, and no such path is implemented. Mirrors
    the `point_reservations.status` CHECK member set.
    """

    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"
