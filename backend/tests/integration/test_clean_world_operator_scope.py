# backend/tests/integration/test_clean_world_operator_scope.py
"""clean_world must survive operator-owned ledger rows (2026-10-09 FK abort).

The e2e teardown abort (r3 forensics): a ledger row whose user_id is
OUTSIDE the clean's user set but whose operator_id is INSIDE it —
created when an admin suite approves another world's redemption —
escaped the user_id-only ledger delete and killed the user delete on
fk_points_ledger_users_operator_id, aborting the whole clean and
leaving the world behind as residue.

Regression shape, at the integration level: seed two disjoint users
(one "outside owner", one "set operator"), post the exact row through
the ledger service, then run clean_world over the operator's set —
previously a ForeignKeyViolation on the users delete, now the operator
scope catches the row and the clean completes.

Everything rides REAL committed sessions (the ledger-service battery's
_factory pattern): the harness's outer-rollback db_session cannot
share rows with clean_world's own connections. The tail clean over
BOTH users leaves nothing behind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.security import hash_password
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import User
from app.modules.points.enums import LedgerType
from app.modules.points.ledger_service import LedgerService, PostLedgerEntry
from app.modules.points.models import PointsLedger
from tests.e2e.factories import clean_world

_T0 = datetime(2026, 10, 9, 8, 0, 0, tzinfo=UTC)


async def _seed_user(
    factory: async_sessionmaker, nickname: str
) -> User:
    async with factory() as db:
        user = User(
            username=str(uuid.uuid4().int)[:20],
            password_hash=hash_password("clean-scope-horse"),
            nickname=nickname,
            role=Role.STUDENT.value,
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.integration
async def test_clean_world_covers_operator_ledger_rows(
    db_engine: AsyncEngine,
) -> None:
    """The exact r3 shape: row owned outside, operated inside."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    outside = await _seed_user(factory, "集外持有者")
    operator = await _seed_user(factory, "集内操作员")

    service = LedgerService()
    async with factory() as db:
        await service.post_entry(
            db,
            PostLedgerEntry(
                user_id=outside.id,
                ledger_type=LedgerType.REWARD_REDEMPTION,
                amount=-30,
                source_type="REWARD_REDEMPTION",
                source_id=uuid.uuid4(),
                affects_balance=True,
                affects_ranking=False,
                operator_id=operator.id,
                reason="the r3 shape: approve another world's redemption",
            ),
        )
        await db.commit()

    # The regression: pre-fix this dies on fk_points_ledger_users_
    # operator_id when deleting the operator (the set member).
    await clean_world(factory, user_ids=[operator.id])

    async with factory() as db:
        ledger_rows = (
            (
                await db.execute(
                    select(PointsLedger.id).where(
                        PointsLedger.user_id.in_([outside.id, operator.id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert ledger_rows == []
        # The row's OWNER is outside the set — the clean cannot remove
        # her (and must not try); only the row and the operator go.
        assert await db.get(User, operator.id) is None
        assert await db.get(User, outside.id) is not None

        # Leave nothing behind: with the operator row gone, the outside
        # owner's own clean is FK-safe.
    await clean_world(factory, user_ids=[outside.id])
    async with factory() as db:
        assert await db.get(User, outside.id) is None
