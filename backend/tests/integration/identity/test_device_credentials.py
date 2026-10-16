# backend/tests/integration/identity/test_device_credentials.py
"""Remembered-device credential lifecycle (V1.1 §2.2, Wave 1 Task 3).

Integration (real PostgreSQL rollback harness) because every operation
is a table round-trip; the Clock is frozen so the 30-day expiry and the
``last_used_at`` bump are exact, not slept for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.modules.identity.device_credentials import (
    DEVICE_COOKIE_NAME,
    DEVICE_TTL_DAYS,
    issue,
    revoke_all,
    validate,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import RememberedDevice, User

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 10, 15, 8, 0, 0, tzinfo=UTC)


async def _seed_staff(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        password_hash=None,
        nickname="devices",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


def _frozen_at(moment: datetime) -> FrozenClock:
    return FrozenClock(moment)


async def test_issue_returns_plaintext_and_stores_only_the_hash(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-hash@campus.example.edu")
    clock = _frozen_at(_T0)

    cookie_value = await issue(db_session, staff.id, clock=clock)

    rows = (
        (
            await db_session.execute(
                select(RememberedDevice).where(RememberedDevice.user_id == staff.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    stored = rows[0]
    assert stored.token_hash == sha256(cookie_value.encode()).hexdigest()
    assert stored.token_hash != cookie_value
    assert stored.expires_at == _T0 + timedelta(days=DEVICE_TTL_DAYS)
    assert stored.last_used_at is None
    # The cookie value is urlsafe(32): 43 base64url characters.
    assert len(cookie_value) == 43


async def test_validate_roundtrip_returns_user_and_bumps_last_used(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-roundtrip@campus.example.edu")
    clock = _frozen_at(_T0)
    cookie_value = await issue(db_session, staff.id, clock=clock)

    assert await validate(db_session, cookie_value, clock=clock) == staff.id

    row = (
        await db_session.execute(
            select(RememberedDevice).where(RememberedDevice.user_id == staff.id)
        )
    ).scalar_one()
    assert row.last_used_at == _T0


async def test_validate_expired_cookie_returns_none(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-expired@campus.example.edu")
    cookie_value = await issue(db_session, staff.id, clock=_frozen_at(_T0))

    later = FrozenClock(_T0 + timedelta(days=DEVICE_TTL_DAYS, seconds=1))
    assert await validate(db_session, cookie_value, clock=later) is None


async def test_validate_wrong_value_returns_none(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-wrong@campus.example.edu")
    await issue(db_session, staff.id, clock=_frozen_at(_T0))

    assert (
        await validate(db_session, "not-a-device-cookie", clock=_frozen_at(_T0)) is None
    )


async def test_revoke_all_deletes_every_device_and_counts(
    db_session: AsyncSession,
) -> None:
    staff = await _seed_staff(db_session, "devices-revoke@campus.example.edu")
    clock = _frozen_at(_T0)
    first = await issue(db_session, staff.id, clock=clock)
    second = await issue(db_session, staff.id, clock=clock)

    deleted = await revoke_all(db_session, staff.id)

    assert deleted == 2
    assert await validate(db_session, first, clock=clock) is None
    assert await validate(db_session, second, clock=clock) is None


async def test_two_users_devices_are_independent(
    db_session: AsyncSession,
) -> None:
    first = await _seed_staff(db_session, "devices-a@campus.example.edu")
    second = await _seed_staff(db_session, "devices-b@campus.example.edu")
    clock = _frozen_at(_T0)
    first_cookie = await issue(db_session, first.id, clock=clock)
    second_cookie = await issue(db_session, second.id, clock=clock)

    assert await revoke_all(db_session, first.id) == 1

    assert await validate(db_session, first_cookie, clock=clock) is None
    assert await validate(db_session, second_cookie, clock=clock) == second.id


def test_cookie_contract_constants() -> None:
    """The frontend contract: cookie name and the 30-day lifetime."""
    assert DEVICE_COOKIE_NAME == "cq_staff_device"
    assert DEVICE_TTL_DAYS == 30


async def test_validate_uuid_return_type(db_session: AsyncSession) -> None:
    """validate answers a UUID (or None) — never the raw row."""
    staff = await _seed_staff(db_session, "devices-type@campus.example.edu")
    cookie_value = await issue(db_session, staff.id, clock=_frozen_at(_T0))

    user_id = await validate(db_session, cookie_value, clock=_frozen_at(_T0))
    assert isinstance(user_id, UUID)
