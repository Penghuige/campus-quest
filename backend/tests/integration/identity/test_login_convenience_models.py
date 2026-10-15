# backend/tests/integration/identity/test_login_convenience_models.py
"""Model + migration round-trip for the Wave-1 login tables (V1.1 §2).

Lives under integration (not unit) because the round-trips are real
PostgreSQL behavior against the rollback harness — the same reason as
test_identity_constraints.py.

`test_passwordless_staff_roundtrip` pins migration 0028's
`users.password_hash` nullable flip: the TEACHER invitation path (V1.1
§2.3) creates accounts whose only login channel is the email code, and
the column must accept NULL end to end, not just in the ORM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import (
    LoginEmailChallenge,
    RememberedDevice,
    User,
)

pytestmark = pytest.mark.integration


async def _seed_staff(db: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        # None since migration 0028: email-code-only staff accounts
        # (V1.1 §2.3) have no password at all.
        password_hash=None,
        nickname="wave1-models",
        role=Role.TEACHER,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def test_login_email_challenge_roundtrip(db_session: AsyncSession) -> None:
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    challenge = LoginEmailChallenge(
        email_normalized="wave1@campus.example.edu",
        code_hash="a" * 64,
        expires_at=expires_at,
    )
    db_session.add(challenge)
    await db_session.flush()

    got = (
        await db_session.execute(
            select(LoginEmailChallenge).where(
                LoginEmailChallenge.email_normalized == "wave1@campus.example.edu"
            )
        )
    ).scalar_one()
    assert got.attempts == 0
    assert got.code_hash == "a" * 64
    assert got.expires_at == expires_at
    assert got.created_at is not None


async def test_remembered_device_roundtrip(db_session: AsyncSession) -> None:
    staff = await _seed_staff(db_session, "wave1-devices@campus.example.edu")
    device = RememberedDevice(
        user_id=staff.id,
        token_hash="b" * 64,
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=30),
    )
    db_session.add(device)
    await db_session.flush()

    assert device.id is not None
    assert device.last_used_at is None
    assert device.created_at is not None


async def test_passwordless_staff_roundtrip(db_session: AsyncSession) -> None:
    """Migration 0028's nullable flip, pinned at the model boundary."""
    staff = await _seed_staff(db_session, "wave1-passwordless@campus.example.edu")
    await db_session.flush()

    reloaded = await db_session.get(User, staff.id)
    assert reloaded is not None
    assert reloaded.password_hash is None
