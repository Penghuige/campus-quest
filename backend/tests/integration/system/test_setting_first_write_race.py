# backend/tests/integration/system/test_setting_first_write_race.py
"""Two-session first-write race on a brand-new settings key (PR #2
closure review P2; chain assertion added in round 5): the race decides
INSIDE the database (INSERT ... ON CONFLICT DO NOTHING RETURNING), the
loser locks the winner's committed row and reads the TRUE previous, so
the audit chain is connected — None→X, X→Y — never two stale None→?
rows (quality-gates §16/G12: under concurrency the audit migration
must not be false).
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
async def test_concurrent_first_writes_audit_a_connected_chain(db_engine) -> None:
    """Two independent sessions set the SAME brand-new key at once: no
    IntegrityError escapes, exactly one row survives, and the two audit
    rows form a CONNECTED chain — the winner's None→X (it created the
    key), the loser's X→Y (it locked the winner's committed row and
    read the true previous) — with the stored row's final value and
    attribution matching the chain's tail. Both writes really happened
    (two rows), and neither carries a stale before."""
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
        audits = sorted(
            (
                await check.execute(
                    select(AuditLog).where(
                        AuditLog.action == "SYSTEM_SETTING_UPDATED",
                        AuditLog.target_id == key,
                    )
                )
            )
            .scalars()
            .all(),
            # The chain's head is the first write (no previous value);
            # the tail is the loser's locked update. Order by the
            # snapshot pair itself, not created_at (transaction-
            # timestamp ties would make row order unobservable).
            key=lambda audit: audit.before_snapshot["value"] is not None,
        )
        assert len(audits) == 2  # each committed write is a real decision
        head, tail = audits
        # Head: the winner created the key — no previous value exists.
        assert head.before_snapshot == {"value": None}
        first_value = head.after_snapshot["value"]
        assert first_value in {"2027-spring", "2026-fall"}
        # Tail: the loser serialized behind the winner's commit and
        # audited the winner's committed value as its TRUE previous.
        assert tail.before_snapshot == {"value": first_value}
        second_value = tail.after_snapshot["value"]
        assert second_value in {"2027-spring", "2026-fall"}
        assert second_value != first_value
        # The stored row agrees with the chain's tail — value AND the
        # last writer's attribution.
        assert row.value == second_value
        assert row.updated_by_user_id == tail.actor_user_id
        # Each audit names its own writer (both decisions happened).
        by_value = {
            audit.after_snapshot["value"]: audit.actor_user_id for audit in audits
        }
        assert by_value == {
            "2027-spring": first_admin.id,
            "2026-fall": second_admin.id,
        }
