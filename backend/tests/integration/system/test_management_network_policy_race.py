# backend/tests/integration/system/test_management_network_policy_race.py
"""The MANAGEMENT_NETWORK_* pair's single serial domain over real
PostgreSQL with TWO committed sessions (PR #5 final-review fix A, P0;
G15: concurrency invariants need real-PG races, not mocks).

The P0 being closed: the two policy keys are written by SEPARATE
single-key PUTs whose cross-key validation used to read the partner
key outside any serialization — two Admins concurrent-enough could
both validate against the OLD partner value and commit an UNLOADABLE
pair (enabled=true + cidrs=[]), wedging every guarded admin surface
on the loader's ValueError while the repair API itself sat behind the
broken guard.

``SystemSettingService.set_management_network_policy`` now takes a
fixed transaction-scoped ``pg_advisory_xact_lock`` BEFORE reading the
partner key's effective value (store row or env fallback), so:

- the four-initial-state matrix (no rows / enabled-only / cidrs-only /
  both) races ``enabled=True`` against ``cidrs=[]``: EXACTLY ONE write
  is refused (the typed 422) and one applies — the adversarial pair
  cannot both win — and the terminal pair is ALWAYS loadable, in every
  serialization order;
- a policy write SERIALIZES behind the lock (it cannot complete while
  another connection holds it) — the lock is really taken, not
  decorative;
- the first write to a key overrides its env fallback (the W4
  migration's move-to-store semantics under the new path): enabling
  succeeds against a stored CIDR row even though the env fallback
  lists nothing.

Committed sessions throughout (the rollback harness cannot
cross-transaction race); every test cleans its committed rows in
``finally`` — the policy keys are fixed registry keys whose leftovers
would leak into other suites.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.admin_network_policy import load_management_network_policy
from app.core.config import get_settings
from app.core.errors import BusinessError
from app.core.security import hash_password
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.system.models import SystemSetting
from app.modules.system.service import (
    MANAGEMENT_NETWORK_CIDRS,
    MANAGEMENT_NETWORK_ENABLED,
    MANAGEMENT_NETWORK_POLICY_LOCK,
    SYSTEM_SETTING_UPDATED,
    SystemSettingService,
)

pytestmark = pytest.mark.integration

_PASSWORD = "correct-horse-battery"


async def _seed_admin(maker: async_sessionmaker[AsyncSession], username: str) -> User:
    async with maker() as session:
        user = User(
            username=username,
            password_hash=hash_password(_PASSWORD),
            nickname=f"管理员{username[-4:]}",
            phone_e164=None,
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.commit()
        return user


async def _seed_rows(
    maker: async_sessionmaker[AsyncSession],
    actor_id,
    rows: dict[str, str],
) -> None:
    """Insert INITIAL policy rows directly, bypassing the write path —
    the test seeds a PRE-STATE (including states the aggregate gate
    would sequence differently), it does not exercise the path under
    test for setup."""
    async with maker() as session:
        for key, value in rows.items():
            session.add(
                SystemSetting(key=key, value=value, updated_by_user_id=actor_id)
            )
        await session.commit()


async def _policy_rows(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, str]:
    async with maker() as session:
        rows = (
            await session.execute(
                select(SystemSetting).where(
                    SystemSetting.key.in_(
                        [MANAGEMENT_NETWORK_ENABLED, MANAGEMENT_NETWORK_CIDRS]
                    )
                )
            )
        ).scalars()
        return {row.key: row.value for row in rows}


async def _policy_audits(
    maker: async_sessionmaker[AsyncSession],
) -> list[AuditLog]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == SYSTEM_SETTING_UPDATED,
                    AuditLog.target_id.in_(
                        [MANAGEMENT_NETWORK_ENABLED, MANAGEMENT_NETWORK_CIDRS]
                    ),
                )
            )
        )


async def _cleanup(maker: async_sessionmaker[AsyncSession], *user_ids) -> None:
    """Test garbage collection only: this module commits for real, and
    the policy keys are FIXED registry keys whose leftovers would leak
    into every other settings test."""
    async with maker() as session:
        await session.execute(
            delete(AuditLog).where(
                AuditLog.target_id.in_(
                    [MANAGEMENT_NETWORK_ENABLED, MANAGEMENT_NETWORK_CIDRS]
                )
            )
        )
        await session.execute(
            delete(SystemSetting).where(
                SystemSetting.key.in_(
                    [MANAGEMENT_NETWORK_ENABLED, MANAGEMENT_NETWORK_CIDRS]
                )
            )
        )
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


async def _write(
    maker: async_sessionmaker[AsyncSession],
    actor: Actor,
    **kwargs: object,
) -> BusinessError | None:
    """One racing writer: the typed refusal, or None when the write
    applied (the call commits its own transaction)."""
    async with maker() as session:
        try:
            await SystemSettingService().set_management_network_policy(
                session,
                actor=actor,
                **kwargs,  # type: ignore[arg-type]
            )
            return None
        except BusinessError as exc:
            return exc


# --- the four-initial-state race matrix -----------------------------------------------


@pytest.mark.parametrize(
    ("initial", "rows"),
    [
        ("no-rows", {}),
        ("enabled-only", {MANAGEMENT_NETWORK_ENABLED: "false"}),
        ("cidrs-only", {MANAGEMENT_NETWORK_CIDRS: "10.0.0.0/8"}),
        (
            "both",
            {
                MANAGEMENT_NETWORK_ENABLED: "false",
                MANAGEMENT_NETWORK_CIDRS: "10.0.0.0/8",
            },
        ),
    ],
    ids=["双无行", "仅-enabled", "仅-cidrs", "双有行"],
)
async def test_concurrent_enable_and_empty_cidrs_keep_the_pair_loadable(
    db_engine: AsyncEngine, initial: str, rows: dict[str, str]
) -> None:
    """The adversarial race — ``enabled=True`` against ``cidrs=[]`` from
    two committed sessions, from every initial state: the writers
    serialize on the aggregate advisory lock, so EXACTLY ONE write is
    the typed 422 and one applies (the pair cannot be enabled-empty and
    emptied-enabled at once), and whatever order the lock chose, the
    TERMINAL pair resolves through the per-request loader without
    raising — the P0's unloadable terminal state is unwritable now."""
    # The matrix's expectations are grounded in the env fallback being
    # the disabled/empty default (the integration gate sets no
    # MANAGEMENT_NETWORK_* env vars).
    settings = get_settings()
    assert settings.management_network_enabled is False
    assert settings.management_network_cidrs == ""

    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:6]
    admin_a = await _seed_admin(maker, f"polrace-a-{suffix}")
    admin_b = await _seed_admin(maker, f"polrace-b-{suffix}")
    if rows:
        await _seed_rows(maker, admin_a.id, rows)

    try:
        # Barrier start: both writers race for the same lock.
        enable_outcome, empty_outcome = await asyncio.gather(
            _write(maker, Actor(user_id=admin_a.id, role=Role.ADMIN), enabled=True),
            _write(maker, Actor(user_id=admin_b.id, role=Role.ADMIN), cidrs=[]),
        )
        outcomes = [enable_outcome, empty_outcome]
        # Exactly one refusal — WHICH writer loses depends on the lock's
        # order, but the adversarial pair can never both apply.
        refusals = [outcome for outcome in outcomes if outcome is not None]
        assert len(refusals) == 1, outcomes
        refused = refusals[0]
        assert refused.status_code == 422
        assert refused.details["key"] in (
            MANAGEMENT_NETWORK_ENABLED,
            MANAGEMENT_NETWORK_CIDRS,
        )

        # The terminal pair is loadable — exactly what
        # require_management_network_from_store would resolve.
        final_rows = await _policy_rows(maker)
        policy = load_management_network_policy(
            stored_enabled=final_rows.get(MANAGEMENT_NETWORK_ENABLED),
            stored_cidrs=final_rows.get(MANAGEMENT_NETWORK_CIDRS),
        )
        if policy.enabled:
            assert policy.networks  # enabled implies a non-empty allowlist
        # The refused writer stored nothing; the applied writer's key
        # carries exactly one audit row for its decision.
        audits = await _policy_audits(maker)
        assert len(audits) == 1
        assert audits[0].target_id != refused.details["key"]
    finally:
        await _cleanup(maker, admin_a.id, admin_b.id)


# --- the lock is really taken --------------------------------------------------------


async def test_policy_write_serializes_behind_the_lock(db_engine: AsyncEngine) -> None:
    """A second connection holding the aggregate lock BLOCKS a policy
    write until it releases — the serialization the matrix above relies
    on, observed directly (no cancellation: the write task is left
    pending, the holder releases, then the write completes)."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:6]
    admin = await _seed_admin(maker, f"pollock-{suffix}")

    holder = await db_engine.connect()
    try:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:lock)"),
            {"lock": MANAGEMENT_NETWORK_POLICY_LOCK},
        )
        writer = asyncio.create_task(
            _write(
                maker, Actor(user_id=admin.id, role=Role.ADMIN), cidrs=["10.0.0.0/8"]
            )
        )
        done, pending = await asyncio.wait({writer}, timeout=0.5)
        assert not done, "the policy write completed while the lock was held"
        assert pending == {writer}

        await holder.rollback()  # ends the holder's transaction: lock released
        refused = await writer
        assert refused is None
    finally:
        await holder.close()
        await _cleanup(maker, admin.id)


# --- the env fallback's first-write override under the new path -----------------------


async def test_first_write_overrides_the_env_fallback(db_engine: AsyncEngine) -> None:
    """The W4 migration's semantics through the aggregate path: with no
    rows, enabling against the (empty) env CIDR list is REFUSED, but
    storing a CIDR row first — validated against the env's disabled
    flag — makes the subsequent enable resolve the STORE row, not the
    env fallback, and succeed."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:6]
    admin = await _seed_admin(maker, f"polenv-{suffix}")
    actor = Actor(user_id=admin.id, role=Role.ADMIN)

    try:
        refused = await _write(maker, actor, enabled=True)
        assert refused is not None and refused.status_code == 422

        applied = await _write(maker, actor, cidrs=["10.0.0.0/8"])
        assert applied is None
        enabled = await _write(maker, actor, enabled=True)
        assert enabled is None

        final_rows = await _policy_rows(maker)
        policy = load_management_network_policy(
            stored_enabled=final_rows.get(MANAGEMENT_NETWORK_ENABLED),
            stored_cidrs=final_rows.get(MANAGEMENT_NETWORK_CIDRS),
        )
        assert policy.enabled is True
        assert [str(network) for network in policy.networks] == ["10.0.0.0/8"]
    finally:
        await _cleanup(maker, admin.id)
