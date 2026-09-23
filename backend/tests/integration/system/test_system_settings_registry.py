# backend/tests/integration/system/test_system_settings_registry.py
"""The typed system-settings key registry over real PostgreSQL (Plan 08
T5): every writable key's validator/normalizer, the per-write version
column (0020) and its ``details.version`` audit mirror, and the
management-network resolution contract — store keys written through the
audited store, resolved store-first with env fallback by
``admin_network_policy.load_management_network_policy``.

Drives ``SystemSettingService`` directly (the storage-layer contract;
the router surface stays T9's) with the rollback-harness session:
rejected writes leave no row and no audit row; accepted writes store
exactly the canonical spelling the registry defines.

The two ``MANAGEMENT_NETWORK_*`` keys are written through
``set_management_network_policy`` — their ONLY write path since PR #5
fix A (``set`` refuses them; the pair's cross-key invariant is checked
under the aggregate advisory lock). The tests below therefore seed the
partner key first whenever the pair under test must stay loadable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_network_policy import load_management_network_policy
from app.core.config import Settings
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import hash_password
from app.modules.audit.models import AuditLog
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.system.models import SystemSetting
from app.modules.system.service import (
    ABANDON_DAILY_LIMIT,
    CURRENT_ACADEMIC_TERM,
    EMOJI_WHITELIST,
    MANAGEMENT_NETWORK_CIDRS,
    MANAGEMENT_NETWORK_ENABLED,
    SYSTEM_SETTING_UPDATED,
    SystemSettingService,
)

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)


async def _admin(db: AsyncSession) -> Actor:
    user = User(
        username=f"registry-admin-{uuid4().hex[:8]}",
        password_hash=hash_password("correct-horse-battery"),
        nickname="注册表管理员",
        phone_e164=None,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return Actor(user_id=user.id, role=Role.ADMIN)


async def _row(db: AsyncSession, key: str) -> SystemSetting | None:
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == key))
    if row is not None:
        await db.refresh(row)
    return row


async def _audits(db: AsyncSession, key: str) -> list[AuditLog]:
    result = await db.scalars(
        select(AuditLog)
        .where(AuditLog.action == SYSTEM_SETTING_UPDATED, AuditLog.target_id == key)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list(result)


# --- version column + audit details.version (0020) -----------------------------------


async def test_each_write_bumps_version_and_audits_it(
    db_session: AsyncSession,
) -> None:
    actor = await _admin(db_session)
    service = SystemSettingService()

    await service.set(
        db_session, actor=actor, key=CURRENT_ACADEMIC_TERM, value="2027-spring"
    )
    await service.set(
        db_session, actor=actor, key=CURRENT_ACADEMIC_TERM, value="2027-summer"
    )

    row = await _row(db_session, CURRENT_ACADEMIC_TERM)
    assert row is not None
    assert row.value == "2027-summer"
    assert row.version == 2

    audits = await _audits(db_session, CURRENT_ACADEMIC_TERM)
    # Both writes land inside the rollback harness's ONE outer
    # transaction, so created_at ties and no insertion order is
    # observable — compare per written value (the api-test precedent).
    by_value = {audit.after_snapshot["value"]: audit for audit in audits}
    assert set(by_value) == {"2027-spring", "2027-summer"}
    assert by_value["2027-spring"].details == {"version": 1}
    assert by_value["2027-spring"].before_snapshot == {"value": None}
    assert by_value["2027-summer"].details == {"version": 2}
    assert by_value["2027-summer"].before_snapshot == {"value": "2027-spring"}


# --- EMOJI_WHITELIST -----------------------------------------------------------------


async def test_emoji_whitelist_stores_json_list(db_session: AsyncSession) -> None:
    actor = await _admin(db_session)

    stored = await SystemSettingService().set(
        db_session, actor=actor, key=EMOJI_WHITELIST, value=["👍", "🎉", "❤️"]
    )

    assert stored == '["👍", "🎉", "❤️"]'
    row = await _row(db_session, EMOJI_WHITELIST)
    assert row is not None and row.value == stored


async def test_empty_emoji_whitelist_is_legal_all_banned(
    db_session: AsyncSession,
) -> None:
    actor = await _admin(db_session)

    stored = await SystemSettingService().set(
        db_session, actor=actor, key=EMOJI_WHITELIST, value=[]
    )

    assert stored == "[]"


@pytest.mark.parametrize(
    ("value", "reason_fragment"),
    [
        (["👍", ""], "码点"),  # an empty item is 0 code points
        (["123456789"], "码点"),  # 9 code points
        (["👍", 7], "字符串数组"),  # a non-str item
        ("👍", "字符串数组"),  # not a list at all
    ],
)
async def test_invalid_emoji_whitelist_is_typed_422(
    db_session: AsyncSession, value: object, reason_fragment: str
) -> None:
    actor = await _admin(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await SystemSettingService().set(
            db_session, actor=actor, key=EMOJI_WHITELIST, value=value
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    assert reason_fragment in exc_info.value.details["reason"]
    assert await _row(db_session, EMOJI_WHITELIST) is None
    assert await _audits(db_session, EMOJI_WHITELIST) == []


# --- ABANDON_DAILY_LIMIT --------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 5])
async def test_abandon_limit_stores_decimal_text(
    db_session: AsyncSession, value: int
) -> None:
    actor = await _admin(db_session)

    stored = await SystemSettingService().set(
        db_session, actor=actor, key=ABANDON_DAILY_LIMIT, value=value
    )

    assert stored == str(value)
    row = await _row(db_session, ABANDON_DAILY_LIMIT)
    assert row is not None and row.value == str(value)


@pytest.mark.parametrize("value", [-1, "5", 2.5, True])
async def test_invalid_abandon_limit_is_typed_422(
    db_session: AsyncSession, value: object
) -> None:
    actor = await _admin(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await SystemSettingService().set(
            db_session, actor=actor, key=ABANDON_DAILY_LIMIT, value=value
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.details["key"] == ABANDON_DAILY_LIMIT
    assert await _row(db_session, ABANDON_DAILY_LIMIT) is None
    assert await _audits(db_session, ABANDON_DAILY_LIMIT) == []


# --- MANAGEMENT_NETWORK_ENABLED / _CIDRS ----------------------------------------------


async def _set_policy(db: AsyncSession, actor: Actor, **kwargs: object) -> object:
    """The pair's one write path (PR #5 fix A): thin wrapper so the
    tests read as the routes do."""
    return await SystemSettingService().set_management_network_policy(
        db,
        actor=actor,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("value", "stored"),
    [(True, "true"), (False, "false")],
)
async def test_network_enabled_stores_canonical_flag(
    db_session: AsyncSession, value: bool, stored: str
) -> None:
    actor = await _admin(db_session)
    # enabled=true needs a non-empty effective CIDR list: seed the
    # partner row first (the aggregate path validates the PAIR).
    await _set_policy(db_session, actor, cidrs=["10.0.0.0/8"])

    result = await _set_policy(db_session, actor, enabled=value)

    assert result.value == stored
    assert result.key == MANAGEMENT_NETWORK_ENABLED


@pytest.mark.parametrize("value", ["true", 1])
async def test_non_bool_network_enabled_is_typed_422(
    db_session: AsyncSession, value: object
) -> None:
    actor = await _admin(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await _set_policy(db_session, actor, enabled=value)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 422
    assert await _row(db_session, MANAGEMENT_NETWORK_ENABLED) is None


async def test_policy_write_requires_a_key(
    db_session: AsyncSession,
) -> None:
    """Neither key provided is the typed 422 — the aggregate path is a
    write path, not a no-op."""
    actor = await _admin(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await _set_policy(db_session, actor)

    assert exc_info.value.status_code == 422
    assert await _row(db_session, MANAGEMENT_NETWORK_ENABLED) is None
    assert await _row(db_session, MANAGEMENT_NETWORK_CIDRS) is None


async def test_network_cidrs_store_canonical_comma_separated(
    db_session: AsyncSession,
) -> None:
    actor = await _admin(db_session)

    result = await _set_policy(
        db_session,
        actor,
        cidrs=["10.0.0.0/8", "192.168.1.7", "2001:db8::/48"],
    )

    # Bare IPs canonicalize to /32; v6 keeps its compressed spelling.
    assert result.value == "10.0.0.0/8,192.168.1.7/32,2001:db8::/48"
    row = await _row(db_session, MANAGEMENT_NETWORK_CIDRS)
    assert row is not None and row.value == result.value
    # The full CIDR list rides the audit snapshot — non-sensitive
    # infrastructure fact, never a truncated summary (Plan 08 T5).
    audits = await _audits(db_session, MANAGEMENT_NETWORK_CIDRS)
    assert len(audits) == 1
    assert audits[0].after_snapshot == {"value": result.value}


async def test_empty_network_cidrs_is_legal(db_session: AsyncSession) -> None:
    actor = await _admin(db_session)

    result = await _set_policy(db_session, actor, cidrs=[])

    assert result.value == ""


@pytest.mark.parametrize(
    "value",
    [
        ["10.0.0.5/24"],  # host bits set: strict parsing refuses
        ["not-a-cidr"],
        ["10.0.0.0/8", 42],
        "10.0.0.0/8",  # not a list
    ],
)
async def test_invalid_network_cidrs_is_typed_422(
    db_session: AsyncSession, value: object
) -> None:
    actor = await _admin(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await _set_policy(db_session, actor, cidrs=value)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 422
    assert exc_info.value.details["key"] == MANAGEMENT_NETWORK_CIDRS
    assert await _row(db_session, MANAGEMENT_NETWORK_CIDRS) is None
    assert await _audits(db_session, MANAGEMENT_NETWORK_CIDRS) == []


@pytest.mark.parametrize(
    ("kwargs", "seed_cidrs"),
    [
        ({"enabled": True}, None),  # no row, env fallback empty
        ({"cidrs": []}, "10.0.0.0/8"),  # emptying under an enabled pair
    ],
)
async def test_unloadable_policy_pair_is_refused_at_write_time(
    db_session: AsyncSession,
    kwargs: dict[str, object],
    seed_cidrs: str | None,
) -> None:
    """The cross-key gate lives INSIDE the aggregate path now: enabling
    with an empty effective list, or emptying the list while enabled,
    is the typed 422 and stores nothing (PR #5 fix A keeps the pair
    loadable from every direction)."""
    actor = await _admin(db_session)
    if seed_cidrs is not None:
        await _set_policy(db_session, actor, cidrs=[seed_cidrs])
        await _set_policy(db_session, actor, enabled=True)

    with pytest.raises(BusinessError) as exc_info:
        await _set_policy(db_session, actor, **kwargs)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
    # The refused write stored nothing and audited nothing.
    if "enabled" in kwargs:
        assert await _row(db_session, MANAGEMENT_NETWORK_ENABLED) is None
    else:
        row = await _row(db_session, MANAGEMENT_NETWORK_CIDRS)
        assert row is not None and row.value == seed_cidrs


async def test_plain_set_refuses_the_policy_keys(
    db_session: AsyncSession,
) -> None:
    """The single serial domain has no side door: ``set`` answers the
    typed 422 for both policy keys (PR #5 fix A)."""
    actor = await _admin(db_session)

    for key in (MANAGEMENT_NETWORK_ENABLED, MANAGEMENT_NETWORK_CIDRS):
        with pytest.raises(BusinessError) as exc_info:
            await SystemSettingService().set(
                db_session,
                actor=actor,
                key=key,
                value=False if key == MANAGEMENT_NETWORK_ENABLED else [],
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.details["key"] == key

    assert await _row(db_session, MANAGEMENT_NETWORK_ENABLED) is None
    assert await _row(db_session, MANAGEMENT_NETWORK_CIDRS) is None


# --- the unregistered-key guard --------------------------------------------------


@pytest.mark.parametrize("key", ["NOT_A_KNOWN_KEY", "", " current_academic_term "])
async def test_unregistered_key_is_unwritable(
    db_session: AsyncSession, key: str
) -> None:
    actor = await _admin(db_session)

    with pytest.raises(BusinessError) as exc_info:
        await SystemSettingService().set(
            db_session, actor=actor, key=key, value="anything"
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.details["reason"] == "未注册的设置键（不可写入）"
    assert (await db_session.scalars(select(SystemSetting))).all() == []
    assert (
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == SYSTEM_SETTING_UPDATED)
        )
    ).all() == []


# --- the loader contract: store first, env fallback -------------------------------


def _env_settings(
    monkeypatch: pytest.MonkeyPatch, *, enabled: str, cidrs: str
) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/test")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_BUCKET", "campusquest")
    monkeypatch.setenv("S3_ACCESS_KEY", "access")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("BUSINESS_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("MANAGEMENT_NETWORK_ENABLED", enabled)
    monkeypatch.setenv("MANAGEMENT_NETWORK_CIDRS", cidrs)
    return Settings()


async def test_store_keys_win_over_env_and_absent_keys_fall_back(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The T5 resolution contract, end to end through the store: rows
    written via the audited store drive the policy; keys with no row
    resolve from the (deprecated) env fields."""
    actor = await _admin(db_session)
    # The pair's one write path: CIDRs first (the env flag is disabled
    # in this environment, so the pair stays loadable), then the flag.
    await _set_policy(
        db_session,
        actor,
        cidrs=["10.20.0.0/16"],
    )
    await _set_policy(db_session, actor, enabled=True)
    settings = _env_settings(monkeypatch, enabled="false", cidrs="203.0.113.0/24")

    service = SystemSettingService()
    stored_enabled = await service.get(db_session, MANAGEMENT_NETWORK_ENABLED)
    stored_cidrs = await service.get(db_session, MANAGEMENT_NETWORK_CIDRS)
    assert stored_enabled == "true"
    assert stored_cidrs == "10.20.0.0/16"

    # Both rows present: the store wins on both keys.
    policy = load_management_network_policy(
        stored_enabled=stored_enabled,
        stored_cidrs=stored_cidrs,
        settings=settings,
    )
    assert policy.enabled is True
    assert [str(network) for network in policy.networks] == ["10.20.0.0/16"]

    # One row present (enabled only): per-key fallback — the stored flag,
    # the env CIDRs.
    partial = load_management_network_policy(
        stored_enabled=stored_enabled,
        stored_cidrs=None,
        settings=settings,
    )
    assert partial.enabled is True
    assert [str(network) for network in partial.networks] == ["203.0.113.0/24"]

    # No rows at all: pure env (today's behavior, the transition path).
    env_only = load_management_network_policy(
        stored_enabled=None, stored_cidrs=None, settings=settings
    )
    assert env_only.enabled is False
    assert [str(network) for network in env_only.networks] == ["203.0.113.0/24"]
