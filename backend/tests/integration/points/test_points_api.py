# backend/tests/integration/points/test_points_api.py
"""Points wallet, rewards, and redemption-review HTTP APIs over real
PostgreSQL (spec §15/§16/§16.1/§16.2, §17.1 spending never ranks, §28 URL
shapes, §29 envelope, §32 advisory Idempotency-Key; plan 05 task 8;
backend-engineering §3/§16).

Drives the real app (``create_app()`` — the points router mounted under
/api/v1) with the rollback-harness session:

- ``GET /points/me``: the wallet projection (available / earned) plus
  the spendable figure (available minus ACTIVE reservations, spec
  §16.2); a user without a wallet reads all zeros;
- ``GET /rewards``: enabled items only, with the redemption window's
  open/closed verdict computed SERVER-SIDE at the business clock (the
  half-open ``available_from <= now < available_until`` rule);
- ``POST /rewards/{id}/redeem``: the atomic freeze (status REQUESTED,
  spendable drops, available untouched until approval), the typed
  conflict envelopes (insufficient points, out of stock), and the V1
  Idempotency-Key ruling: the header is ACCEPTED but ADVISORY — the
  database constraints (one reservation per redemption, wallet-row lock)
  are the duplicate-side-effect protection, so two requests carrying
  the same key are two redemptions;
- the staff review surface (``require_staff_management_actor``): the
  pending queue (oldest first, offset-paginated, requester nickname +
  item name enrichment), approve (one negative REWARD_REDEMPTION ledger
  row, wallet available drops, idempotent replay), reject (reason
  mandatory at the transport, reservation released, no consumption
  row), fulfill with a note (APPROVED -> FULFILLED);
- the role boundary: student surfaces reject staff roles, staff
  surfaces reject students, both with PERMISSION_DENIED.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
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
from app.modules.points.enums import LedgerType, RedemptionStatus, ReservationStatus
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_TERM = "2026-fall"

_WALLET_FIELDS = {"available_points", "earned_points", "spendable_points"}
_REWARD_FIELDS = {
    "id",
    "name",
    "description",
    "point_cost",
    "stock",
    "per_user_term_limit",
    "available_from",
    "available_until",
    "requires_manual_review",
    "window_open",
}
_STUDENT_REDEMPTION_FIELDS = {
    "id",
    "reward_item_id",
    "status",
    "points",
    "term_key",
    "created_at",
}
_STAFF_REDEMPTION_FIELDS = _STUDENT_REDEMPTION_FIELDS | {
    "requester_nickname",
    "item_name",
    "decided_at",
    "fulfilled_at",
    "fulfillment_note",
}
_QUEUE_FIELDS = {"items", "total", "limit", "offset"}


# --- fixtures -----------------------------------------------------------------------


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


# --- seeding helpers ----------------------------------------------------------------


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


async def _management_teacher(
    db: AsyncSession, clock: FrozenClock, *, username: str
) -> tuple[User, dict[str, str]]:
    """A TEACHER able to pass ``require_staff_management_actor`` (role +
    ACTIVE + a confirmed TOTP credential row)."""
    user = await _seed_user(db, username=username, role=Role.TEACHER)
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    return user, await _headers(db, clock, user)


def _item(**overrides: Any) -> RewardItem:
    fields: dict[str, Any] = {
        "name": "平时成绩 +1",
        "description": "在本学期平时成绩中加一分。",
        "point_cost": 400,
        "stock": 10,
        "per_user_term_limit": None,
        "available_from": None,
        "available_until": None,
        "enabled": True,
        "requires_manual_review": True,
        "fulfillment_instructions": "教务录入",
    }
    fields.update(overrides)
    return RewardItem(**fields)


async def _frozen_redemption(
    db: AsyncSession, *, user: User, item: RewardItem, points: int
) -> RewardRedemption:
    """One open redemption holding an ACTIVE freeze (the request
    transaction's committed pair)."""
    redemption = RewardRedemption(
        user_id=user.id,
        reward_item_id=item.id,
        status=RedemptionStatus.REQUESTED.value,
        term_key=_TERM,
        points=points,
        created_at=_T0 - timedelta(hours=2),
    )
    db.add(redemption)
    await db.flush()
    db.add(
        PointReservation(
            user_id=user.id,
            redemption_id=redemption.id,
            points=points,
            status=ReservationStatus.ACTIVE.value,
        )
    )
    await db.flush()
    return redemption


@pytest_asyncio.fixture
async def points_world(
    db_session: AsyncSession, api_clock: FrozenClock
) -> dict[str, Any]:
    teacher, teacher_headers = await _management_teacher(
        db_session, api_clock, username="points-teacher-0001"
    )
    student = await _seed_user(
        db_session, username="points-student-0002", role=Role.STUDENT
    )
    student_headers = await _headers(db_session, api_clock, student)
    newcomer = await _seed_user(
        db_session, username="points-new-user-0003", role=Role.STUDENT
    )
    newcomer_headers = await _headers(db_session, api_clock, newcomer)

    wallet = PointWallet(user_id=student.id, available_points=1000, earned_points=1000)
    open_item = _item()
    future_item = _item(
        name="期中盲盒",
        point_cost=1200,
        available_from=_T0 + timedelta(days=1),
    )
    pricey_item = _item(name="期末大礼包", point_cost=5000)
    db_session.add_all([wallet, open_item, future_item, pricey_item])
    await db_session.flush()

    return {
        "db": db_session,
        "teacher": teacher,
        "teacher_headers": teacher_headers,
        "student": student,
        "student_headers": student_headers,
        "newcomer": newcomer,
        "newcomer_headers": newcomer_headers,
        "open_item": open_item,
        "future_item": future_item,
        "pricey_item": pricey_item,
    }


# --- GET /points/me ------------------------------------------------------------------


async def test_points_me_reports_wallet_and_spendable(
    client: httpx.AsyncClient, db_session: AsyncSession, points_world: dict[str, Any]
) -> None:
    world = points_world
    # An existing open redemption freezes 100 of the 1000 available.
    await _frozen_redemption(
        db_session, user=world["student"], item=world["open_item"], points=100
    )

    response = await client.get("/api/v1/points/me", headers=world["student_headers"])
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _WALLET_FIELDS
    assert body == {
        "available_points": 1000,
        "earned_points": 1000,
        "spendable_points": 900,
    }


async def test_points_me_reads_zeros_without_a_wallet(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    response = await client.get(
        "/api/v1/points/me", headers=points_world["newcomer_headers"]
    )
    assert response.status_code == 200
    assert response.json() == {
        "available_points": 0,
        "earned_points": 0,
        "spendable_points": 0,
    }


# --- GET /rewards --------------------------------------------------------------------


async def test_rewards_list_enabled_items_with_server_side_window_verdict(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    disabled = _item(name="下架奖品", enabled=False)
    points_world["db"].add(disabled)
    await points_world["db"].flush()

    response = await client.get(
        "/api/v1/rewards", headers=points_world["student_headers"]
    )
    assert response.status_code == 200
    items = response.json()["items"]
    names = {item["name"] for item in items}
    assert names == {"平时成绩 +1", "期中盲盒", "期末大礼包"}  # disabled excluded

    by_name = {item["name"]: item for item in items}
    assert set(by_name["平时成绩 +1"]) == _REWARD_FIELDS
    assert by_name["平时成绩 +1"]["window_open"] is True
    # available_from is tomorrow at the frozen clock: closed server-side.
    assert by_name["期中盲盒"]["window_open"] is False
    assert by_name["期末大礼包"]["window_open"] is True


async def test_rewards_rejects_staff_roles(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    response = await client.get(
        "/api/v1/rewards", headers=points_world["teacher_headers"]
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


# --- POST /rewards/{id}/redeem --------------------------------------------------------


async def test_redeem_freezes_points_atomically(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    world = points_world
    response = await client.post(
        f"/api/v1/rewards/{world['open_item'].id}/redeem",
        headers=world["student_headers"],
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == _STUDENT_REDEMPTION_FIELDS
    assert body["status"] == "REQUESTED"
    assert body["points"] == 400
    assert body["term_key"] == _TERM
    assert body["reward_item_id"] == str(world["open_item"].id)

    # The freeze lands on spendable; available itself is untouched until
    # an approval writes the consumption entry (spec §16.2).
    wallet = await client.get("/api/v1/points/me", headers=world["student_headers"])
    assert wallet.json() == {
        "available_points": 1000,
        "earned_points": 1000,
        "spendable_points": 600,
    }


async def test_redeem_insufficient_points_answers_conflict_envelope(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    world = points_world
    response = await client.post(
        f"/api/v1/rewards/{world['pricey_item'].id}/redeem",
        headers=world["student_headers"],
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "INSUFFICIENT_POINTS"
    assert error["details"] == {"required": 5000, "spendable": 1000}


async def test_redeem_out_of_stock_answers_conflict_envelope(
    client: httpx.AsyncClient, db_session: AsyncSession, points_world: dict[str, Any]
) -> None:
    world = points_world
    last_one = _item(name="绝版周边", stock=1)
    db_session.add(last_one)
    await db_session.flush()
    await _frozen_redemption(
        db_session, user=world["student"], item=last_one, points=400
    )

    response = await client.post(
        f"/api/v1/rewards/{last_one.id}/redeem", headers=world["student_headers"]
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REWARD_OUT_OF_STOCK"


async def test_redeem_idempotency_key_is_advisory_in_v1(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    """Spec §32 names redeem as a SHOULD for Idempotency-Key; the V1
    ruling (plan 05 task 8) is that the header is ADVISORY: the database
    constraints — one reservation row per redemption, the wallet-row
    lock — are the duplicate-side-effect protection, so a repeated key
    is accepted and simply creates a second, independent redemption."""
    world = points_world
    key = {"Idempotency-Key": "9f2c7a1e-1b4d-4c8a-9d3e-6a5b8c7d9e0f"}
    first = await client.post(
        f"/api/v1/rewards/{world['open_item'].id}/redeem",
        headers={**world["student_headers"], **key},
    )
    second = await client.post(
        f"/api/v1/rewards/{world['open_item'].id}/redeem",
        headers={**world["student_headers"], **key},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    # Both freezes hold: 1000 - 400 - 400.
    wallet = await client.get("/api/v1/points/me", headers=world["student_headers"])
    assert wallet.json()["spendable_points"] == 200


# --- staff review surface ------------------------------------------------------------


async def test_staff_lists_pending_redemptions_oldest_first(
    client: httpx.AsyncClient, db_session: AsyncSession, points_world: dict[str, Any]
) -> None:
    world = points_world
    older = await _frozen_redemption(
        db_session, user=world["student"], item=world["open_item"], points=400
    )
    newer = await _frozen_redemption(
        db_session, user=world["student"], item=world["open_item"], points=400
    )
    newer.created_at = _T0 - timedelta(hours=1)
    await db_session.flush()
    rejected = RewardRedemption(
        user_id=world["student"].id,
        reward_item_id=world["open_item"].id,
        status=RedemptionStatus.REJECTED.value,
        term_key=_TERM,
        points=400,
    )
    db_session.add(rejected)
    await db_session.flush()

    response = await client.get(
        "/api/v1/teacher/rewards/redemptions",
        headers=world["teacher_headers"],
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _QUEUE_FIELDS
    assert body["total"] == 2  # the REJECTED row never queues
    assert [item["id"] for item in body["items"]] == [
        str(older.id),
        str(newer.id),
    ]
    item = body["items"][0]
    assert set(item) == _STAFF_REDEMPTION_FIELDS
    assert item["requester_nickname"] == world["student"].nickname
    assert item["item_name"] == world["open_item"].name
    assert item["status"] == "REQUESTED"

    paged = await client.get(
        "/api/v1/teacher/rewards/redemptions",
        params={"limit": 1, "offset": 1},
        headers=world["teacher_headers"],
    )
    assert paged.json()["items"][0]["id"] == str(newer.id)
    assert paged.json()["total"] == 2


async def test_staff_approve_then_fulfill_the_redemption(
    client: httpx.AsyncClient, db_session: AsyncSession, points_world: dict[str, Any]
) -> None:
    world = points_world
    redemption = await _frozen_redemption(
        db_session, user=world["student"], item=world["open_item"], points=400
    )

    approve = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{redemption.id}/approve",
        headers=world["teacher_headers"],
    )
    assert approve.status_code == 200
    body = approve.json()
    assert set(body) == _STAFF_REDEMPTION_FIELDS
    assert body["status"] == "APPROVED"
    assert body["decided_at"] is not None

    # The freeze became exactly ONE negative consumption entry (spec
    # §16.2), the wallet's available dropped, and spending never ranks:
    # earned is untouched (spec §17.1).
    consumption = (
        await db_session.execute(
            select(func.count())
            .select_from(PointsLedger)
            .where(
                PointsLedger.source_id == redemption.id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION.value,
            )
        )
    ).scalar_one()
    assert consumption == 1
    wallet = await client.get("/api/v1/points/me", headers=world["student_headers"])
    assert wallet.json() == {
        "available_points": 600,
        "earned_points": 1000,
        "spendable_points": 600,
    }

    # Approve replay is idempotent: still 200, no second entry.
    replay = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{redemption.id}/approve",
        headers=world["teacher_headers"],
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "APPROVED"
    consumption_after = (
        await db_session.execute(
            select(func.count())
            .select_from(PointsLedger)
            .where(
                PointsLedger.source_id == redemption.id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION.value,
            )
        )
    ).scalar_one()
    assert consumption_after == 1

    fulfill = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{redemption.id}/fulfill",
        json={"note": "已录入第九周平时分"},
        headers=world["teacher_headers"],
    )
    assert fulfill.status_code == 200
    assert fulfill.json()["status"] == "FULFILLED"
    assert fulfill.json()["fulfillment_note"] == "已录入第九周平时分"
    assert fulfill.json()["fulfilled_at"] is not None


async def test_staff_reject_requires_reason_and_releases_the_freeze(
    client: httpx.AsyncClient, db_session: AsyncSession, points_world: dict[str, Any]
) -> None:
    world = points_world
    redemption = await _frozen_redemption(
        db_session, user=world["student"], item=world["open_item"], points=400
    )

    blank = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{redemption.id}/reject",
        json={"reason": "   "},
        headers=world["teacher_headers"],
    )
    assert blank.status_code == 422
    assert blank.json()["error"]["code"] == "VALIDATION_ERROR"

    reject = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{redemption.id}/reject",
        json={"reason": "库存调拨给线下活动"},
        headers=world["teacher_headers"],
    )
    assert reject.status_code == 200
    assert reject.json()["status"] == "REJECTED"

    # The freeze is released and NO consumption entry exists (spec
    # §16.2: 拒绝不产生消费负流水).
    wallet = await client.get("/api/v1/points/me", headers=world["student_headers"])
    assert wallet.json()["spendable_points"] == 1000
    consumption = (
        await db_session.execute(
            select(func.count())
            .select_from(PointsLedger)
            .where(
                PointsLedger.source_id == redemption.id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION.value,
            )
        )
    ).scalar_one()
    assert consumption == 0


async def test_staff_review_rejects_student_roles(
    client: httpx.AsyncClient, points_world: dict[str, Any]
) -> None:
    world = points_world
    some_id = str(UUID(int=1))
    for path, method in (
        ("/api/v1/teacher/rewards/redemptions", "GET"),
        (f"/api/v1/teacher/rewards/redemptions/{some_id}/approve", "POST"),
        (f"/api/v1/teacher/rewards/redemptions/{some_id}/reject", "POST"),
        (f"/api/v1/teacher/rewards/redemptions/{some_id}/fulfill", "POST"),
    ):
        response = await client.request(method, path, headers=world["student_headers"])
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"
