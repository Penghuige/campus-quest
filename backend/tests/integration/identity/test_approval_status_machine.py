# backend/tests/integration/identity/test_approval_status_machine.py
"""The approval status machine (V1.1 auth-extension II spec §1-§2).

PENDING_APPROVAL joins the user status machine (open enrollment lands
here), approved_at/rejected_reason record the admin's verdict, and the
whitelist era ends: its gate is gone from registration and its admin
surface answers 404.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import LoginMobileChallenge, User

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)


async def test_pending_approval_status_roundtrip(db_session: AsyncSession) -> None:
    user = User(
        username="20250019999",
        password_hash=None,
        nickname="待审同学",
        phone_e164="+8613800139999",
        role=Role.STUDENT,
        status=UserStatus.PENDING_APPROVAL,
    )
    db_session.add(user)
    await db_session.flush()

    reloaded = await db_session.get(User, user.id)
    assert reloaded is not None
    assert reloaded.status == UserStatus.PENDING_APPROVAL.value
    # The verdict columns rest empty until an admin acts.
    assert reloaded.approved_at is None
    assert reloaded.rejected_reason is None

    reloaded.status = UserStatus.ACTIVE.value
    reloaded.approved_at = _T0
    await db_session.flush()
    assert reloaded.approved_at == _T0


async def test_rejected_reason_roundtrip(db_session: AsyncSession) -> None:
    user = User(
        username="20250018888",
        password_hash=None,
        nickname="被拒同学",
        phone_e164="+8613800138888",
        role=Role.STUDENT,
        status=UserStatus.BANNED,
        rejected_reason="资料不符",
    )
    db_session.add(user)
    await db_session.flush()

    reloaded = await db_session.get(User, user.id)
    assert reloaded is not None
    assert reloaded.rejected_reason == "资料不符"


async def test_mobile_challenge_model_roundtrip(db_session: AsyncSession) -> None:
    """The login-mobile challenge mirrors the email one: keyed digest,
    one live row per phone, attempt counter."""
    from datetime import timedelta

    challenge = LoginMobileChallenge(
        phone_e164="+8613800137777",
        code_hash="a" * 64,
        expires_at=_T0 + timedelta(minutes=5),
    )
    db_session.add(challenge)
    await db_session.flush()

    got = (
        await db_session.execute(
            select(LoginMobileChallenge).where(
                LoginMobileChallenge.phone_e164 == "+8613800137777"
            )
        )
    ).scalar_one()
    assert got.attempts == 0
    assert got.created_at is not None
