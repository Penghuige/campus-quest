# backend/tests/integration/system/test_setting_first_write_race.py
"""Two-session first-write race on a brand-new settings key (PR #2
closure review P2): the insert path is a PostgreSQL UPSERT, so two
concurrent ``set`` calls on the same fresh key serialize inside the
database instead of racing the primary key into an IntegrityError 500.
Committed sessions (the rollback harness cannot cross-transaction race).
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.system.models import SystemSetting
from app.modules.system.service import SystemSettingService

pytestmark = pytest.mark.integration


async def _seed_admin(session: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        password_hash=hash_password("correct-horse-battery"),
        nickname=f"管理员{username[-4:]}",
        phone_e164=None,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_concurrent_first_writes_serialize_without_500(db_engine) -> None:
    """Two independent sessions set the SAME brand-new key at once: no
    IntegrityError escapes, exactly one row survives, and both writes
    land their audit rows (both decisions really happened)."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as seed_session:
        first_admin = await _seed_admin(seed_session, f"race-a-{uuid4().hex[:6]}")
        second_admin = await _seed_admin(seed_session, f"race-b-{uuid4().hex[:6]}")
        key = f"race-key-{uuid4().hex[:8]}"

    async def _write(user: User, value: str) -> None:
        async with maker() as session:
            service = SystemSettingService()
            await service.set(
                session,
                actor=Actor(user_id=user.id, role=Role.ADMIN),
                key=key,
                value=value,
            )

    # Barrier start: both transactions race the same missing row.
    await asyncio.gather(
        _write(first_admin, "2027-spring"),
        _write(second_admin, "2026-fall"),
    )

    async with maker() as check:
        row = await check.scalar(select(SystemSetting).where(SystemSetting.key == key))
        assert row is not None
        assert row.value in {"2027-spring", "2026-fall"}
        audits = (
            (
                await check.execute(
                    select(AuditLog).where(
                        AuditLog.action == "SYSTEM_SETTING_UPDATED",
                        AuditLog.target_id == key,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 2  # each committed write is a real decision
