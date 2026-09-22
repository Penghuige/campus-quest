# backend/tests/integration/points/test_reward_admin.py
"""RewardItem administration and the scoped Teacher review
authorization against real PostgreSQL (Plan 08 T4; spec §4.2-§4.3, §16).

Scenario map (the plan's steps, verbatim semantics):

- **Permissions (step 1):** an ACTIVE+TOTP Teacher WITHOUT a
  reward-review grant is 403 on the queue AND all three decisions
  (direct-URL privilege test through the real app); a GRANTED Teacher
  reviews — approve consumes the freeze, fulfill records delivery, the
  queue reads — and an Admin revocation makes the very next call 403
  again (the guard reads the grant row per request, no cache); Admin
  reviews globally; a granted Teacher WITHOUT a confirmed TOTP never
  reaches the surface (TOTP_SETUP_REQUIRED — the §33.4 gate the
  delegation inherits).
- **Update rules (step 2):** editing point_cost / per_user_term_limit /
  stock binds FUTURE redemption requests only — the open redemption
  keeps its request-time ``points`` snapshot and its ACTIVE reservation
  the frozen amount (negative assertions), while the next request
  snapshots the new cost and consults the new limit.
- **Audit (G12):** every committed catalogue change, grant, and
  revocation leaves exactly one audit row in the same transaction —
  snapshots carry the CHANGED fields only (G11: business facts), the
  grant pair targets the TEACHER account with the mandatory reason.
  Refused, replayed, and unauthenticated calls write nothing.

Harness notes: savepoint-wrapped ``db_session`` (services commit inside
it; the outer rollback keeps tests hermetic); the review flow uses the
REAL request path (``RedemptionService.request_redemption`` against a
wallet funded through the ledger), so the snapshot assertions see
exactly what production writes; HTTP cases drive ``create_app()`` with
the session and clock overridden (the test_points_api pattern).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import FrozenClock
from app.core.error_codes import ErrorCode
from app.core.errors import BusinessError
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.identity.dependencies import (
    get_access_token_codec,
    get_business_clock,
)
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import TotpCredential, User
from app.modules.identity.session_service import SessionService
from app.modules.points.admin_service import (
    AUDIT_REWARD_ITEM_CREATED,
    AUDIT_REWARD_ITEM_DISABLED,
    AUDIT_REWARD_ITEM_UPDATED,
    AUDIT_REWARD_REVIEW_GRANTED,
    AUDIT_REWARD_REVIEW_REVOKED,
    AdminReasonRequiredError,
    DuplicateRewardReviewGrantError,
    RewardAdminService,
    RewardItemChanges,
    RewardItemValidationError,
    RewardReviewGrantNotFoundError,
)
from app.modules.points.enums import LedgerType, RedemptionStatus, ReservationStatus
from app.modules.points.ledger_service import LedgerService
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    RewardItem,
    RewardRedemption,
    RewardReviewGrant,
)
from app.modules.points.redemption_service import (
    REDEMPTION_APPROVE,
    REDEMPTION_FULFILL,
    RedemptionLimitReachedError,
    RedemptionPermissionDeniedError,
    RedemptionService,
    StaticAcademicTermProvider,
    has_reward_review_grant,
)

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_PASSWORD = "correct-horse-battery"
_TERM = "2026-fall"


# --- fixtures


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


# --- seeding helpers


async def _seed_user(db: AsyncSession, *, username: str, role: Role) -> User:
    user = User(
        username=username,
        password_hash=hash_password(_PASSWORD),
        nickname=f"用户{username[-4:]}",
        phone_e164=None,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    return user


async def _headers(
    db: AsyncSession, clock: FrozenClock, user: User
) -> tuple[dict[str, str], UUID]:
    sessions = SessionService(clock=clock, access_codec=get_access_token_codec())
    session_row, tokens = await sessions.issue_session(db, user=user, now=clock.now())
    return {"Authorization": f"Bearer {tokens.access_token}"}, session_row.id


async def _totp_teacher(
    db: AsyncSession, clock: FrozenClock, *, username: str, role: Role = Role.TEACHER
) -> tuple[User, dict[str, str]]:
    """A staff account that passes the management gate (role + ACTIVE +
    a confirmed TOTP credential row); ``role=ADMIN`` passes the admin
    guard as well."""
    user = await _seed_user(db, username=username, role=role)
    db.add(
        TotpCredential(
            user_id=user.id,
            secret_encrypted=b"test-stand-in-secret",
            confirmed_at=clock.now(),
        )
    )
    await db.flush()
    headers, session_id = await _headers(db, clock, user)
    return user, headers, session_id


def _actor(user: User, role: Role | None = None) -> Actor:
    """Actor from captured ids/role (service rollbacks expire ORM
    instances — the concurrency-suite discipline)."""
    return Actor(user_id=user.id, role=role if role is not None else user.role)


def _admin_service() -> RewardAdminService:
    return RewardAdminService()


def _redemptions() -> RedemptionService:
    return RedemptionService(
        clock=FrozenClock(_T0), terms=StaticAcademicTermProvider(_TERM)
    )


async def _fund(db: AsyncSession, user_id: UUID, amount: int) -> None:
    """Wallet balance through the real ledger path (the only sanctioned
    way a wallet ever grows)."""
    await LedgerService().grant_assignment_reward(
        db,
        claim_id=uuid4(),
        user_id=user_id,
        amount=amount,
        ranking_effective_at=_T0,
    )


async def _audit_rows(
    db: AsyncSession, actor_id: UUID, *actions: str
) -> dict[str, AuditLog]:
    """This actor's rows for ``actions`` — scoped because the shared
    integration database carries committed residue from other
    sessions' runs, and an unscoped action query would read theirs."""
    stmt = select(AuditLog).where(
        AuditLog.actor_user_id == actor_id,
        AuditLog.action.in_(actions),
    )
    return {row.action: row for row in (await db.scalars(stmt)).all()}


# --- catalogue: create


async def test_admin_creates_reward_item_and_audits(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-create-0001", role=Role.ADMIN)
    service = _admin_service()

    item = await service.create_reward_item(
        db_session,
        _actor(admin),
        name="平时成绩 +1",
        point_cost=400,
        stock=10,
        per_user_term_limit=2,
        description="在本学期平时成绩中加一分。",
        fulfillment_instructions="教务录入",
    )

    assert item.enabled is True  # new rows start on the shelf
    assert item.point_cost == 400
    assert item.stock == 10
    assert item.per_user_term_limit == 2

    rows = await _audit_rows(db_session, admin.id, AUDIT_REWARD_ITEM_CREATED)
    assert set(rows) == {AUDIT_REWARD_ITEM_CREATED}
    row = rows[AUDIT_REWARD_ITEM_CREATED]
    assert row.actor_user_id == admin.id
    assert row.actor_role == Role.ADMIN.value
    assert row.target_type == "reward_item"
    assert row.target_id == str(item.id)
    assert row.before_snapshot is None
    assert row.after_snapshot == {
        "name": "平时成绩 +1",
        "point_cost": 400,
        "stock": 10,
        "per_user_term_limit": 2,
        "available_from": None,
        "available_until": None,
        "requires_manual_review": False,
        "enabled": True,
    }


async def test_reward_item_create_rejects_invalid_shapes(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-shape-0002", role=Role.ADMIN)
    service = _admin_service()
    later = _T0 + timedelta(days=1)
    invalid = (
        {"name": "   ", "point_cost": 100},
        {"name": "空价格", "point_cost": 0},
        {"name": "负库存", "point_cost": 100, "stock": -1},
        {"name": "负上限", "point_cost": 100, "per_user_term_limit": -1},
        {
            "name": "倒置窗口",
            "point_cost": 100,
            "available_from": later,
            "available_until": _T0,
        },
    )
    for kwargs in invalid:
        with pytest.raises(BusinessError) as denied:
            await service.create_reward_item(db_session, _actor(admin), **kwargs)
        assert denied.value.code == ErrorCode.VALIDATION_ERROR
        assert denied.value.status_code == 422

    # Friendly-first gates refuse BEFORE the database: nothing landed,
    # nothing audited.
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(RewardItem)
            .where(RewardItem.name.in_(("空价格", "负库存", "负上限", "倒置窗口")))
        )
    ).scalar_one()
    assert count == 0
    audit_count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AUDIT_REWARD_ITEM_CREATED)
        )
    ).scalar_one()
    assert audit_count == 0


async def test_catalogue_administration_requires_admin(
    db_session: AsyncSession,
) -> None:
    teacher = await _seed_user(
        db_session, username="rewadm-role-0003", role=Role.TEACHER
    )
    student = await _seed_user(
        db_session, username="rewadm-role-0004", role=Role.STUDENT
    )
    admin = await _seed_user(db_session, username="rewadm-role-0005", role=Role.ADMIN)
    service = _admin_service()
    item = await service.create_reward_item(
        db_session, _actor(admin), name="占位奖品", point_cost=100
    )

    for actor in (_actor(teacher), _actor(student)):
        with pytest.raises(BusinessError) as create_denied:
            await service.create_reward_item(
                db_session, actor, name="越权创建", point_cost=100
            )
        with pytest.raises(BusinessError) as update_denied:
            await service.update_reward_item(
                db_session,
                actor,
                item.id,
                RewardItemChanges(point_cost=999),
            )
        with pytest.raises(BusinessError) as disable_denied:
            await service.disable_reward_item(
                db_session, actor, item.id, reason="越权下架"
            )
        for denied in (create_denied, update_denied, disable_denied):
            assert denied.value.code == ErrorCode.PERMISSION_DENIED
            assert denied.value.status_code == 403

    # Nothing moved, nothing audited beyond the Admin's own create.
    await db_session.refresh(item)
    assert item.point_cost == 100
    assert item.enabled is True
    actions = {
        row.action
        for row in (
            await db_session.scalars(
                select(AuditLog).where(AuditLog.actor_user_id == admin.id)
            )
        ).all()
    }
    assert actions == {AUDIT_REWARD_ITEM_CREATED}


# --- catalogue: update + disable


async def test_admin_updates_only_provided_fields_auditing_the_migration(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-upd-0006", role=Role.ADMIN)
    service = _admin_service()
    item = await service.create_reward_item(
        db_session, _actor(admin), name="盲盒", point_cost=400, stock=10
    )

    updated = await service.update_reward_item(
        db_session,
        _actor(admin),
        item.id,
        RewardItemChanges(point_cost=500, stock=20),
        reason="调价与补货",
    )
    assert updated.point_cost == 500
    assert updated.stock == 20
    assert updated.name == "盲盒"  # untouched field stays

    rows = await _audit_rows(db_session, admin.id, AUDIT_REWARD_ITEM_UPDATED)
    row = rows[AUDIT_REWARD_ITEM_UPDATED]
    assert row.target_id == str(item.id)
    assert row.reason == "调价与补货"
    # CHANGED fields only — the snapshot is the migration, not the row.
    assert row.before_snapshot == {"point_cost": 400, "stock": 10}
    assert row.after_snapshot == {"point_cost": 500, "stock": 20}


async def test_update_validates_the_merged_window_and_requires_a_field(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-window-0007", role=Role.ADMIN)
    service = _admin_service()
    opens = _T0
    closes = _T0.replace(day=30)
    item = await service.create_reward_item(
        db_session,
        _actor(admin),
        name="限时奖品",
        point_cost=100,
        available_from=opens,
        available_until=closes,
    )

    # Editing one bound is checked against the SURVIVING other bound:
    # pulling until before from degenerates the merged window.
    with pytest.raises(RewardItemValidationError):
        await service.update_reward_item(
            db_session,
            _actor(admin),
            item.id,
            RewardItemChanges(available_until=opens - timedelta(days=1)),
        )
    with pytest.raises(RewardItemValidationError):
        await service.update_reward_item(
            db_session, _actor(admin), item.id, RewardItemChanges()
        )

    await db_session.refresh(item)
    assert item.available_until == closes  # refused updates change nothing
    updates = await _audit_rows(db_session, admin.id, AUDIT_REWARD_ITEM_UPDATED)
    assert updates == {}


async def test_update_unknown_item_is_typed_404(db_session: AsyncSession) -> None:
    admin = await _seed_user(db_session, username="rewadm-404-0008", role=Role.ADMIN)
    with pytest.raises(BusinessError) as denied:
        await _admin_service().update_reward_item(
            db_session,
            _actor(admin),
            UUID(int=404),
            RewardItemChanges(point_cost=1),
        )
    assert denied.value.code == ErrorCode.NOT_FOUND
    assert denied.value.status_code == 404


async def test_admin_disables_item_with_mandatory_reason_and_idempotent_replay(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-off-0009", role=Role.ADMIN)
    service = _admin_service()
    item = await service.create_reward_item(
        db_session, _actor(admin), name="将下架", point_cost=100
    )

    with pytest.raises(AdminReasonRequiredError) as blank:
        await service.disable_reward_item(
            db_session, _actor(admin), item.id, reason=" "
        )
    assert blank.value.status_code == 400

    disabled = await service.disable_reward_item(
        db_session, _actor(admin), item.id, reason="学期结束，收回库存"
    )
    assert disabled.enabled is False

    rows = await _audit_rows(db_session, admin.id, AUDIT_REWARD_ITEM_DISABLED)
    row = rows[AUDIT_REWARD_ITEM_DISABLED]
    assert row.reason == "学期结束，收回库存"
    assert row.before_snapshot == {"enabled": True}
    assert row.after_snapshot == {"enabled": False}

    # Replay on the disabled row: the idempotent no-op writes no second
    # audit row (the redemption-decision discipline).
    replay = await service.disable_reward_item(
        db_session, _actor(admin), item.id, reason="再次下架"
    )
    assert replay.enabled is False
    disabled_rows = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AUDIT_REWARD_ITEM_DISABLED)
        )
    ).scalar_one()
    assert disabled_rows == 1


# --- update rules: future-bound snapshots (plan step 2)


async def test_cost_and_limit_changes_bind_future_requests_only(
    db_session: AsyncSession,
) -> None:
    """The plan's update rule, end to end through the REAL request path:
    editing point_cost / per_user_term_limit affects only requests made
    AFTER the edit — the open redemption keeps its snapshotted cost and
    its ACTIVE freeze, and the tightened limit refuses the NEXT request
    without touching the accepted ones."""
    admin = await _seed_user(db_session, username="rewadm-fut-0010", role=Role.ADMIN)
    student = await _seed_user(
        db_session, username="rewadm-fut-0011", role=Role.STUDENT
    )
    service = _admin_service()
    redemptions = _redemptions()

    item = await service.create_reward_item(
        db_session, _actor(admin), name="快照奖品", point_cost=400, stock=10
    )
    await _fund(db_session, student.id, 2000)
    await db_session.commit()

    first = await redemptions.request_redemption(db_session, student.id, item.id)
    assert first.points == 400  # the request-time snapshot

    # The edit: cost 400 -> 500, no per-term limit -> 2.
    await service.update_reward_item(
        db_session,
        _actor(admin),
        item.id,
        RewardItemChanges(point_cost=500, per_user_term_limit=2),
    )

    # NEGATIVE ASSERTIONS: the open request is untouched by the edit.
    await db_session.refresh(first)
    assert first.points == 400
    assert first.status == RedemptionStatus.REQUESTED.value
    reservation = await db_session.scalar(
        select(PointReservation).where(PointReservation.redemption_id == first.id)
    )
    assert reservation is not None
    assert reservation.points == 400  # the freeze keeps the frozen amount
    assert reservation.status == ReservationStatus.ACTIVE.value

    # The NEXT request snapshots the new cost (and fits the wallet).
    second = await redemptions.request_redemption(db_session, student.id, item.id)
    assert second.points == 500

    # The tightened limit refuses the THIRD request only; the two
    # accepted ones stay exactly as accepted.
    with pytest.raises(RedemptionLimitReachedError):
        await redemptions.request_redemption(db_session, student.id, item.id)
    for row in (first, second):
        await db_session.refresh(row)
        assert row.status == RedemptionStatus.REQUESTED.value
    await db_session.refresh(reservation)
    assert reservation.status == ReservationStatus.ACTIVE.value


# --- reviewer authorization: the grant lifecycle


async def test_grant_requires_admin_actor_and_teacher_target(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-grt-0012", role=Role.ADMIN)
    teacher = await _seed_user(
        db_session, username="rewadm-grt-0013", role=Role.TEACHER
    )
    student = await _seed_user(
        db_session, username="rewadm-grt-0014", role=Role.STUDENT
    )
    service = _admin_service()

    # A Teacher cannot grant (Admin-only, typed 403).
    with pytest.raises(BusinessError) as role_denied:
        await service.grant_reward_review(
            db_session, _actor(teacher), teacher.id, reason="自我授权"
        )
    assert role_denied.value.status_code == 403

    # Targets must be Teacher accounts (the collaborator-service
    # boundary: role facts through the identity directory port).
    with pytest.raises(BusinessError) as student_denied:
        await service.grant_reward_review(
            db_session, _actor(admin), student.id, reason="错发给学生"
        )
    assert student_denied.value.status_code == 400
    with pytest.raises(BusinessError) as unknown_denied:
        await service.grant_reward_review(
            db_session, _actor(admin), UUID(int=13), reason="未知账号"
        )
    assert unknown_denied.value.status_code == 400

    # Blank reason is refused before anything is written.
    with pytest.raises(AdminReasonRequiredError):
        await service.grant_reward_review(
            db_session, _actor(admin), teacher.id, reason="  "
        )

    assert (
        await db_session.execute(select(func.count()).select_from(RewardReviewGrant))
    ).scalar_one() == 0

    grant = await service.grant_reward_review(
        db_session, _actor(admin), teacher.id, reason="教务处指定的兑换审阅人"
    )
    assert grant.teacher_id == teacher.id
    assert grant.capability == "REWARD_REVIEW"
    assert grant.granted_by == admin.id
    assert await has_reward_review_grant(db_session, teacher.id) is True

    # Duplicate grant: the typed conflict, still exactly one row.
    with pytest.raises(DuplicateRewardReviewGrantError) as duplicate:
        await service.grant_reward_review(
            db_session, _actor(admin), teacher.id, reason="再次授权"
        )
    assert duplicate.value.status_code == 409
    assert (
        await db_session.execute(select(func.count()).select_from(RewardReviewGrant))
    ).scalar_one() == 1


async def test_revoke_unknown_grant_is_typed_404(db_session: AsyncSession) -> None:
    admin = await _seed_user(db_session, username="rewadm-rvk-0015", role=Role.ADMIN)
    with pytest.raises(RewardReviewGrantNotFoundError) as denied:
        await _admin_service().revoke_reward_review(
            db_session, _actor(admin), UUID(int=15), reason="收回"
        )
    assert denied.value.status_code == 404


async def test_grant_and_revoke_write_audit_rows(
    db_session: AsyncSession,
) -> None:
    admin = await _seed_user(db_session, username="rewadm-aud-0016", role=Role.ADMIN)
    teacher = await _seed_user(
        db_session, username="rewadm-aud-0017", role=Role.TEACHER
    )
    service = _admin_service()

    await service.grant_reward_review(
        db_session, _actor(admin), teacher.id, reason="上岗授权"
    )
    await service.revoke_reward_review(
        db_session, _actor(admin), teacher.id, reason="岗位调整"
    )

    rows = await _audit_rows(
        db_session,
        admin.id,
        AUDIT_REWARD_REVIEW_GRANTED,
        AUDIT_REWARD_REVIEW_REVOKED,
    )
    assert set(rows) == {AUDIT_REWARD_REVIEW_GRANTED, AUDIT_REWARD_REVIEW_REVOKED}
    granted, revoked = (
        rows[AUDIT_REWARD_REVIEW_GRANTED],
        rows[AUDIT_REWARD_REVIEW_REVOKED],
    )
    for row in (granted, revoked):
        assert row.actor_user_id == admin.id
        assert row.target_type == "user"  # target = the TEACHER
        assert row.target_id == str(teacher.id)
    assert granted.reason == "上岗授权"
    assert granted.after_snapshot == {"capability": "REWARD_REVIEW"}
    assert revoked.reason == "岗位调整"
    assert revoked.before_snapshot == {"capability": "REWARD_REVIEW"}


# --- the review-guard permission matrix through the real app (step 1) ------------


class _ReviewWorld:
    """The shared HTTP-stage: TOTP-passing Admin/Teacher, a funded
    Student, one live item, and open redemptions made through the REAL
    request path."""

    def __init__(self, world: dict[str, Any]) -> None:
        self.db: AsyncSession = world["db"]
        self.admin: User = world["admin"]
        self.admin_headers: dict[str, str] = world["admin_headers"]
        self.teacher: User = world["teacher"]
        self.teacher_headers: dict[str, str] = world["teacher_headers"]
        self.student: User = world["student"]
        self.item: RewardItem = world["item"]
        self.session_id: UUID = world["teacher_session_id"]


@pytest_asyncio.fixture
async def review_world(
    db_session: AsyncSession, api_clock: FrozenClock
) -> dict[str, Any]:
    admin, admin_headers, _admin_session = await _totp_teacher(
        db_session, api_clock, username="rewadm-http-0018", role=Role.ADMIN
    )
    teacher, teacher_headers, teacher_session = await _totp_teacher(
        db_session, api_clock, username="rewadm-http-0019"
    )
    student = await _seed_user(
        db_session, username="rewadm-http-0020", role=Role.STUDENT
    )
    item = await _admin_service().create_reward_item(
        db_session,
        Actor(user_id=admin.id, role=Role.ADMIN),
        name="HTTP 矩阵奖品",
        point_cost=400,
        stock=10,
    )
    await _fund(db_session, student.id, 4000)
    await db_session.commit()
    return {
        "db": db_session,
        "admin": admin,
        "admin_headers": admin_headers,
        "teacher": teacher,
        "teacher_headers": teacher_headers,
        "student": student,
        "item": item,
        "teacher_session_id": teacher_session,
    }


async def _open_redemption(world: _ReviewWorld) -> RewardRedemption:
    return await _redemptions().request_redemption(
        world.db, world.student.id, world.item.id
    )


def _decision_urls(redemption_id: UUID) -> list[tuple[str, dict[str, Any] | None]]:
    base = "/api/v1/teacher/rewards/redemptions"
    return [
        (f"{base}/{redemption_id}/approve", None),
        (f"{base}/{redemption_id}/reject", {"reason": "矩阵拒绝"}),
        (f"{base}/{redemption_id}/fulfill", {"note": "矩阵发放"}),
    ]


async def test_ungranted_teacher_is_403_on_queue_and_every_decision(
    client: httpx.AsyncClient, review_world: dict[str, Any]
) -> None:
    world = _ReviewWorld(review_world)
    redemption = await _open_redemption(world)

    denied = await client.get(
        "/api/v1/teacher/rewards/redemptions", headers=world.teacher_headers
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"

    for url, payload in _decision_urls(redemption.id):
        response = await client.post(url, json=payload, headers=world.teacher_headers)
        assert response.status_code == 403, url
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    # Nothing was written: the freeze survives untouched.
    await world.db.refresh(redemption)
    assert redemption.status == RedemptionStatus.REQUESTED.value
    consumption = (
        await world.db.execute(
            select(func.count())
            .select_from(PointsLedger)
            .where(
                PointsLedger.source_id == redemption.id,
                PointsLedger.ledger_type == LedgerType.REWARD_REDEMPTION.value,
            )
        )
    ).scalar_one()
    assert consumption == 0


async def test_granted_teacher_reviews_until_revocation_is_immediate(
    client: httpx.AsyncClient, review_world: dict[str, Any]
) -> None:
    world = _ReviewWorld(review_world)
    service = _admin_service()
    first = await _open_redemption(world)

    await service.grant_reward_review(
        world.db, _actor(world.admin), world.teacher.id, reason="教务处指定"
    )

    # The queue reads with the decisions (the ruling's read/write
    # consistency): the granted Teacher sees the pending set.
    queued = await client.get(
        "/api/v1/teacher/rewards/redemptions", headers=world.teacher_headers
    )
    assert queued.status_code == 200
    assert queued.json()["total"] >= 1

    approve = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{first.id}/approve",
        headers=world.teacher_headers,
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "APPROVED"
    fulfill = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{first.id}/fulfill",
        json={"note": "已录入平时分"},
        headers=world.teacher_headers,
    )
    assert fulfill.status_code == 200
    assert fulfill.json()["status"] == "FULFILLED"
    # The teacher's applied decision carries its durable audit row with
    # the TEACHER role snapshot (G12).
    decision_rows = (
        await world.db.scalars(
            select(AuditLog).where(AuditLog.target_id == str(first.id))
        )
    ).all()
    assert {row.action for row in decision_rows} == {
        REDEMPTION_APPROVE,
        REDEMPTION_FULFILL,
    }
    assert all(row.actor_role == Role.TEACHER.value for row in decision_rows)

    # Revocation: the guard reads the grant row per request, so the
    # teacher's very NEXT call fails — no cache window.
    second = await _open_redemption(world)
    await service.revoke_reward_review(
        world.db, _actor(world.admin), world.teacher.id, reason="审阅人更换"
    )

    denied = await client.get(
        "/api/v1/teacher/rewards/redemptions", headers=world.teacher_headers
    )
    assert denied.status_code == 403
    for url, payload in _decision_urls(second.id):
        response = await client.post(url, json=payload, headers=world.teacher_headers)
        assert response.status_code == 403, url

    await world.db.refresh(second)
    assert second.status == RedemptionStatus.REQUESTED.value

    # Admin stays global after the grant's whole lifecycle.
    approved = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{second.id}/approve",
        headers=world.admin_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"


async def test_service_gate_matches_the_transport_guard(
    db_session: AsyncSession,
) -> None:
    """Defense in depth: the RedemptionService gate alone enforces the
    same standing — a grantless Teacher and any Student are the typed
    403; the granted Teacher passes; the revoked Teacher fails again."""
    admin = await _seed_user(db_session, username="rewadm-svc-0021", role=Role.ADMIN)
    teacher = await _seed_user(
        db_session, username="rewadm-svc-0022", role=Role.TEACHER
    )
    student = await _seed_user(
        db_session, username="rewadm-svc-0023", role=Role.STUDENT
    )
    service = _admin_service()
    redemptions = _redemptions()
    item = await service.create_reward_item(
        db_session, _actor(admin), name="服务层矩阵", point_cost=100
    )
    await _fund(db_session, student.id, 500)
    await db_session.commit()
    redemption = await redemptions.request_redemption(db_session, student.id, item.id)

    for actor in (_actor(teacher), _actor(student)):
        with pytest.raises(RedemptionPermissionDeniedError):
            await redemptions.approve_redemption(db_session, actor, redemption.id)

    await service.grant_reward_review(
        db_session, _actor(admin), teacher.id, reason="服务层授权"
    )
    approved = await redemptions.approve_redemption(
        db_session, _actor(teacher), redemption.id
    )
    assert approved.status == RedemptionStatus.APPROVED.value

    await service.revoke_reward_review(
        db_session, _actor(admin), teacher.id, reason="服务层收回"
    )
    second = await redemptions.request_redemption(db_session, student.id, item.id)
    with pytest.raises(RedemptionPermissionDeniedError):
        await redemptions.reject_redemption(
            db_session, _actor(teacher), second.id, "已收回"
        )


async def test_granted_teacher_without_confirmed_totp_cannot_reach_review(
    client: httpx.AsyncClient, db_session: AsyncSession, api_clock: FrozenClock
) -> None:
    """The delegation inherits the §33.4 management gate: a Teacher may
    hold the grant and STILL not reach the surface before the second
    factor is confirmed (ACTIVE+TOTP is part of the widened guard, not
    an optional extra)."""
    admin = await _seed_user(db_session, username="rewadm-totp-0024", role=Role.ADMIN)
    teacher = await _seed_user(
        db_session, username="rewadm-totp-0025", role=Role.TEACHER
    )
    student = await _seed_user(
        db_session, username="rewadm-totp-0026", role=Role.STUDENT
    )
    teacher_headers, _session_id = await _headers(db_session, api_clock, teacher)
    item = await _admin_service().create_reward_item(
        db_session, _actor(admin), name="TOTP 矩阵", point_cost=100
    )
    await _admin_service().grant_reward_review(
        db_session, _actor(admin), teacher.id, reason="先授权，后配 2FA"
    )
    await _fund(db_session, student.id, 500)
    await db_session.commit()
    redemption = await _redemptions().request_redemption(
        db_session, student.id, item.id
    )

    queue = await client.get(
        "/api/v1/teacher/rewards/redemptions", headers=teacher_headers
    )
    assert queue.status_code == 403
    assert queue.json()["error"]["code"] == "TOTP_SETUP_REQUIRED"

    approve = await client.post(
        f"/api/v1/teacher/rewards/redemptions/{redemption.id}/approve",
        headers=teacher_headers,
    )
    assert approve.status_code == 403
    assert approve.json()["error"]["code"] == "TOTP_SETUP_REQUIRED"
    await db_session.refresh(redemption)
    assert redemption.status == RedemptionStatus.REQUESTED.value
