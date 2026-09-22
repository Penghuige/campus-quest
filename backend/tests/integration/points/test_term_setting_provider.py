# backend/tests/integration/points/test_term_setting_provider.py
"""The production academic-term composition over real PostgreSQL (PR #2
hardening step 8): ``SystemAcademicTermProvider`` wired as the redeem
path's provider (points/router.py).

Drives the REAL app with no provider overrides (G18: composition
boundaries are tested against the production wiring) — the redemption
snapshot is the observable answer of the whole chain
``system_settings`` row -> ``SystemSettingService.get`` ->
``SystemAcademicTermProvider`` -> ``RewardRedemption.term_key``:

- no setting row -> the deployment seed ("2026-fall") is snapshotted
  (G7: the env var is the initial seed);
- a row set through the audited admin API -> the ROW's term is
  snapshotted (the row is the fact);
- the snapshot is frozen at creation: after the setting moves on, an
  existing redemption keeps the term it was created under (spec §16.1),
  while the next redemption snapshots the new one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.points.models import PointWallet, RewardItem, RewardRedemption
from app.modules.system.models import SystemSetting
from app.modules.system.service import CURRENT_ACADEMIC_TERM

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_TERM_PATH = "/api/v1/admin/settings/current-academic-term"
_SEED_TERM = "2026-fall"  # Settings.current_academic_term dev default


# --- fixtures and seeding -------------------------------------------------------------


@pytest.fixture
def api_clock() -> FrozenClock:
    return FrozenClock(_T0)


@pytest.fixture
def api_app(db_session: AsyncSession, api_clock: FrozenClock) -> FastAPI:
    app = create_app()

    async def _test_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _test_db_session
    app.dependency_overrides[get_business_clock] = lambda: api_clock
    return app


@pytest_asyncio.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as http:
        yield http


async def _seed_user(db: AsyncSession, *, username: str, role: Role) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"同学{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _headers(db: AsyncSession, clock: FrozenClock, user: User) -> dict[str, str]:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    _, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, api_clock: FrozenClock) -> dict[str, Any]:
    """An admin (for the settings API) plus a student with a funded
    wallet and one redeemable item (the test_points_api seeding
    pattern, scoped down to what the term assertions need)."""
    admin = await _seed_user(db_session, username="term-admin-0001", role=Role.ADMIN)
    db_session.add(
        TotpCredential(
            user_id=admin.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=api_clock.now(),
        )
    )
    student = await _seed_user(
        db_session, username="term-student-0002", role=Role.STUDENT
    )
    wallet = PointWallet(user_id=student.id, available_points=1000, earned_points=1000)
    item = RewardItem(
        name="平时成绩 +1",
        description="在本学期平时成绩中加一分。",
        point_cost=400,
        stock=10,
        per_user_term_limit=None,
        available_from=None,
        available_until=None,
        enabled=True,
        requires_manual_review=True,
        fulfillment_instructions="教务录入",
    )
    db_session.add_all([wallet, item])
    await db_session.flush()
    return {
        "db": db_session,
        "admin_headers": await _headers(db_session, api_clock, admin),
        "student_headers": await _headers(db_session, api_clock, student),
        "item": item,
    }


async def _redeem(client: httpx.AsyncClient, world: dict[str, Any]) -> dict[str, Any]:
    """One redemption through the real API; returns the response body."""
    response = await client.post(
        f"/api/v1/rewards/{world['item'].id}/redeem",
        headers=world["student_headers"],
    )
    assert response.status_code == 201
    return response.json()


async def _term_keys(db: AsyncSession, item: RewardItem) -> list[str]:
    """The item's redemption term snapshots as a multiset — sorted, not
    creation-ordered: both redemptions land inside the rollback
    harness's ONE outer transaction, so ``created_at`` (transaction
    time) ties and creation order is not observable."""
    rows = await db.scalars(
        select(RewardRedemption.term_key).where(
            RewardRedemption.reward_item_id == item.id
        )
    )
    return sorted(rows)


# --- the priority chain ---------------------------------------------------------------


async def test_redemption_snapshots_the_seed_term_without_a_row(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    world: dict[str, Any],
) -> None:
    """No system_settings row: the provider falls back to the deployment
    seed, and that seed is what the redemption snapshots."""
    assert (
        await db_session.scalar(
            select(SystemSetting).where(SystemSetting.key == CURRENT_ACADEMIC_TERM)
        )
        is None
    )

    body = await _redeem(client, world)

    assert body["term_key"] == _SEED_TERM
    assert await _term_keys(db_session, world["item"]) == [_SEED_TERM]


async def test_redemption_snapshots_the_configured_term_once_set(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    world: dict[str, Any],
) -> None:
    """An admin sets the term through the audited API; the NEXT
    redemption snapshots the row's value, not the seed (G7: the settings
    row is the fact, the env var is the initial seed)."""
    put = await client.put(
        _TERM_PATH, json={"value": "2027-spring"}, headers=world["admin_headers"]
    )
    assert put.status_code == 200

    body = await _redeem(client, world)

    assert body["term_key"] == "2027-spring"
    assert await _term_keys(db_session, world["item"]) == ["2027-spring"]


async def test_existing_redemptions_keep_their_term_after_the_setting_moves(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    world: dict[str, Any],
) -> None:
    """Spec §16.1 snapshot semantics under a MOVING setting: redemption
    #1 keeps its seed term, redemption #2 (created after the admin moved
    the term) carries the new one — history keeps the term it was
    created under (the owner's original requirement, now exercised
    against the audited setting instead of a hardcoded key)."""
    first = await _redeem(client, world)
    put = await client.put(
        _TERM_PATH, json={"value": "2027-spring"}, headers=world["admin_headers"]
    )
    assert put.status_code == 200
    second = await _redeem(client, world)

    assert first["term_key"] == _SEED_TERM
    assert second["term_key"] == "2027-spring"
    assert await _term_keys(db_session, world["item"]) == sorted(
        [_SEED_TERM, "2027-spring"]
    )


async def test_get_answers_what_the_next_redemption_would_snapshot(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    world: dict[str, Any],
) -> None:
    """The admin GET and the redemption gate share ONE rule: after the
    row exists, GET's answer equals the term the next redemption
    snapshots (the window_open one-rule ruling, asserted end to end)."""
    await client.put(
        _TERM_PATH, json={"value": "2028-spring"}, headers=world["admin_headers"]
    )

    get = await client.get(_TERM_PATH, headers=world["admin_headers"])
    assert get.status_code == 200
    effective = get.json()["value"]

    body = await _redeem(client, world)
    assert body["term_key"] == effective

    row_value = await db_session.scalar(
        select(RewardRedemption.term_key).where(
            RewardRedemption.reward_item_id == world["item"].id
        )
    )
    assert row_value == effective
    # The setting row itself is the audited fact the answer came from.
    stored = await db_session.scalar(
        select(SystemSetting).where(SystemSetting.key == CURRENT_ACADEMIC_TERM)
    )
    assert stored is not None and stored.value == effective
