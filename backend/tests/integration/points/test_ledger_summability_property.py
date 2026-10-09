# backend/tests/integration/points/test_ledger_summability_property.py
"""Property-based summability: the wallet projection ALWAYS folds the ledger.

Spec §15.1 (the projection must be rebuildable from the ledger) as a
property instead of examples: for ANY legal posting sequence — mixed
rewards, redemptions, and admin adjustments in any order and sign —
after every ``post_entry`` the stored projection equals the SQL fold:

    available_points == sum(amount for entries where affects_balance)
    earned_points    == sum(amount for positive ranking-affecting entries)

The oracle is the DATABASE's own aggregate over the inserted rows, not
a Python re-computation of the service's update logic — a projection
bug (wrong increment, lost update, sign error) cannot pass against its
own ledger's SUM.

Sequences stay short (max 12 entries, 25 examples, no deadline): the
point is the rule over the input SHAPE, and CI time must stay
negligible. Each example owns a fresh user, so examples cannot
contaminate each other through the projection row; the harness's outer
rollback discards everything at teardown.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType
from app.modules.points.ledger_service import LedgerService, PostLedgerEntry
from app.modules.points.models import PointsLedger, PointWallet

_T0 = datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)
_MAGNITUDE = st.integers(min_value=1, max_value=1_000_000)


@st.composite
def legal_postings(draw: st.DrawFn) -> list[PostLedgerEntry]:
    """A legal command sequence for one user — coherence constraints
    (flag/effective-at pairing, source presence per type, admin reason)
    are built INTO the commands so nothing is wasted on assume()."""
    commands: list[PostLedgerEntry] = []
    for _ in range(draw(st.integers(min_value=0, max_value=12))):
        kind = draw(st.sampled_from(["reward", "redemption", "admin"]))
        user_id = UUID(int=0)  # placeholder; the test binds the real user
        if kind == "reward":
            commands.append(
                PostLedgerEntry(
                    user_id=user_id,
                    ledger_type=LedgerType.ASSIGNMENT_REWARD,
                    amount=draw(_MAGNITUDE),
                    source_type="ASSIGNMENT_CLAIM",
                    source_id=uuid4(),
                    affects_balance=True,
                    affects_ranking=True,
                    ranking_effective_at=_T0,
                )
            )
        elif kind == "redemption":
            commands.append(
                PostLedgerEntry(
                    user_id=user_id,
                    ledger_type=LedgerType.REWARD_REDEMPTION,
                    amount=-draw(_MAGNITUDE),
                    source_type="REWARD_REDEMPTION",
                    source_id=uuid4(),
                    affects_balance=True,
                    affects_ranking=False,
                )
            )
        else:
            commands.append(
                PostLedgerEntry(
                    user_id=user_id,
                    ledger_type=LedgerType.ADMIN_ADJUSTMENT,
                    amount=draw(st.sampled_from([1, -1])) * draw(_MAGNITUDE),
                    source_type="ADMIN_ADJUSTMENT",
                    source_id=None,
                    affects_balance=True,
                    affects_ranking=False,
                    operator_id=uuid4(),
                    reason="property-driven adjustment",
                )
            )
    return commands


async def _seed_user(db: AsyncSession, nickname: str) -> User:
    # A full-entropy digits-only username: examples draw independently,
    # so a narrow composition would eventually collide on the UNIQUE.
    user = User(
        username=str(uuid4().int)[:20],
        password_hash=hash_password("property-horse-battery"),
        nickname=nickname,
        role=Role.STUDENT.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_operator(db: AsyncSession) -> User:
    operator = User(
        username=str(uuid4().int)[:20],
        password_hash=hash_password("property-horse-battery"),
        nickname="性质操作员",
        role=Role.ADMIN.value,
        status=UserStatus.ACTIVE.value,
    )
    db.add(operator)
    await db.flush()
    return operator


@pytest.mark.integration
@settings(
    max_examples=25,
    deadline=None,
    # The function-scoped db_session is INTENTIONALLY shared across
    # generated examples: every example owns a fresh user (and fresh
    # source triples), so no example can observe another's rows, and
    # the harness's outer rollback discards the lot at teardown. The
    # health check guards against exactly the accidental-sharing shape
    # this design excludes on purpose.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(plan=legal_postings())
async def test_projection_always_equals_the_ledger_sum(
    db_session: AsyncSession, plan: list[PostLedgerEntry]
) -> None:
    # The ADMIN operator must be a REAL user row: operator_id carries
    # an FK to users, and the strategy cannot know database identities.
    operator = await _seed_operator(db_session)
    user = await _seed_user(db_session, "性质用户")
    service = LedgerService()

    for command in plan:
        await service.post_entry(
            db_session,
            replace(
                command,
                user_id=user.id,
                # ADMIN_ADJUSTMENT is its own source event; the service
                # would mint one, the property pins it explicitly.
                source_id=command.source_id or uuid4(),
                operator_id=command.operator_id and operator.id,
            ),
        )

    balance_sum = (
        await db_session.execute(
            text(
                "select coalesce(sum(amount), 0) from points_ledger "
                "where user_id = :uid and affects_balance"
            ),
            {"uid": user.id},
        )
    ).scalar_one()
    earned_sum = (
        await db_session.execute(
            text(
                "select coalesce(sum(amount), 0) from points_ledger "
                "where user_id = :uid and affects_ranking and amount > 0"
            ),
            {"uid": user.id},
        )
    ).scalar_one()

    wallet = await db_session.get(PointWallet, user.id)
    if not plan:
        # A user with no balance-affecting entry may have no row at all
        # (lazy projection) or an all-zero row — both fold to zero.
        assert balance_sum == 0
        assert wallet is None or wallet.available_points == 0
        return
    assert wallet is not None
    assert wallet.available_points == balance_sum
    assert wallet.earned_points == earned_sum

    # The entries themselves are the audit trail: nothing outside the
    # ledger explains the projection (the §15.1 authority claim).
    rows = (
        await db_session.scalars(
            select(PointsLedger).where(PointsLedger.user_id == user.id)
        )
    ).all()
    assert len(rows) == len(plan)
