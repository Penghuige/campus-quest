# backend/tests/fakes/points.py
"""Deterministic in-memory fake for the frozen `PointsRewardPort`
(docs/architecture/interfaces.md, cross-module ports).

TEST-ONLY code: no database, no ledger side effects. The fake records
every accepted grant for exact-argument assertions and enforces the
port's idempotency contract in memory: a repeated `idempotency_key`
raises `DuplicateGrantKeyError`, mirroring the UNIQUE(claim)
PointsLedger semantics Plan 05's concrete adapter will own at the
database boundary (spec §14: 绝不能发两次积分). A hidden double grant
inside one test therefore fails loudly at the second call instead of
silently counting twice (docs/quality/backend-engineering.md §13:
adapters distinguish the already-exists response).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.modules.submissions.review_service import GrantResult

__all__ = [
    "DuplicateGrantKeyError",
    "FakePointsRewardPort",
    "GrantCall",
]


class DuplicateGrantKeyError(AssertionError):
    """The same idempotency_key reached the port twice — a double grant.

    AssertionError subclass so plain `pytest.raises(AssertionError)`
    also catches it in tests that do not care about the specific type.
    """


@dataclass(frozen=True, slots=True)
class GrantCall:
    """One recorded grant invocation, exactly as invoked."""

    user_id: UUID
    claim_id: UUID
    base_points: int
    locked_points: int
    idempotency_key: str


class FakePointsRewardPort:
    """In-memory `PointsRewardPort`: records grants, rejects duplicate keys.

    `calls` preserves invocation order for exact-delivery assertions;
    `grant_count` is the one-liner the double-approve test asserts
    against (exactly one grant across both racing transactions).
    """

    def __init__(self) -> None:
        self.calls: list[GrantCall] = []
        self._seen_keys: set[str] = set()

    @property
    def grant_count(self) -> int:
        """How many grants the port accepted (duplicate keys raise)."""
        return len(self.calls)

    async def grant_assignment_reward(
        self,
        *,
        user_id: UUID,
        claim_id: UUID,
        base_points: int,
        locked_points: int,
        idempotency_key: str,
    ) -> GrantResult:
        if idempotency_key in self._seen_keys:
            raise DuplicateGrantKeyError(
                f"idempotency_key {idempotency_key!r} granted twice; "
                "UNIQUE(claim) reward semantics violated"
            )
        self._seen_keys.add(idempotency_key)
        self.calls.append(
            GrantCall(
                user_id=user_id,
                claim_id=claim_id,
                base_points=base_points,
                locked_points=locked_points,
                idempotency_key=idempotency_key,
            )
        )
        return GrantResult(
            user_id=user_id, claim_id=claim_id, points_granted=locked_points
        )
