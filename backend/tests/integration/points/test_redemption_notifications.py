# backend/tests/integration/points/test_redemption_notifications.py
"""The redemption notification producers (spec §25; MERGE_CARRIES item 2,
closed at the PR #2 hardening merge).

One integration test per decision: approve and reject record their §25
result events through the REAL ``NotificationPort`` constructor-injected
into ``RedemptionService`` (the points-router production shape) — the
logical Notification plus its IN_APP delivery row commit with the
decision state (the outbox rule), keyed per-redemption, with the frozen
template variables rendered from the row facts (item NAME, points).

Harness notes (the redemption-concurrency conventions): seeding and
assertions use explicit committed sessions from the engine factory;
committed rows are removed by explicit committed DELETEs in ``finally``
(deliveries -> notifications -> ledger -> reservations/redemptions ->
wallet -> items -> users, the FK order).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.clock import FrozenClock
from app.modules.identity.enums import Role, UserStatus
from app.modules.identity.events import Actor
from app.modules.identity.models import User
from app.modules.notifications.enums import (
    DeliveryStatus,
    NotificationChannel,
    NotificationEventType,
)
from app.modules.notifications.models import Notification, NotificationDelivery
from app.modules.points.enums import ReservationStatus
from app.modules.points.ledger_service import LedgerService
from app.modules.points.models import (
    PointReservation,
    PointsLedger,
    PointWallet,
    RewardItem,
    RewardRedemption,
)
from app.modules.points.redemption_service import (
    RedemptionService,
    StaticAcademicTermProvider,
)

_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

_NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
_TERM = "2026-fall"


def _student(username: str) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试同学",
        phone_e164=None,
        role=Role.STUDENT,
        status=UserStatus.ACTIVE,
    )


def _admin(username: str) -> User:
    return User(
        username=username,
        password_hash=_PASSWORD_HASH,
        nickname="测试管理员",
        phone_e164=None,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )


def _reward_item(**overrides: Any) -> RewardItem:
    fields: dict[str, Any] = {
        "name": "平时成绩 +1",
        "description": "在参与课程的平时成绩中加一分。",
        "point_cost": 200,
        "stock": None,
        "per_user_term_limit": None,
    }
    fields.update(overrides)
    return RewardItem(**fields)


def _recording_service() -> RedemptionService:
    """RedemptionService with the REAL NotificationPort injected (the
    production wiring shape from the points router)."""
    from app.modules.notifications.port import NotificationPort

    return RedemptionService(
        clock=FrozenClock(_NOW),
        terms=StaticAcademicTermProvider(_TERM),
        notification_recorder=NotificationPort(clock=FrozenClock(_NOW)),
    )


def _factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def _committed_cleanup(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_ids: list[UUID],
    item_ids: list[UUID],
) -> None:
    async with factory() as session:
        for user_id in user_ids:
            await session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.user_id == user_id
                )
            )
            await session.execute(
                delete(Notification).where(Notification.user_id == user_id)
            )
            await session.execute(
                delete(PointsLedger).where(PointsLedger.user_id == user_id)
            )
            await session.execute(
                delete(PointReservation).where(PointReservation.user_id == user_id)
            )
            await session.execute(
                delete(RewardRedemption).where(RewardRedemption.user_id == user_id)
            )
            await session.execute(
                delete(PointWallet).where(PointWallet.user_id == user_id)
            )
        for item_id in item_ids:
            await session.execute(
                delete(RewardRedemption).where(
                    RewardRedemption.reward_item_id == item_id
                )
            )
        if item_ids:
            await session.execute(delete(RewardItem).where(RewardItem.id.in_(item_ids)))
        if user_ids:
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


async def _notification_with_delivery(
    factory: async_sessionmaker[AsyncSession],
    event_key: str,
    user_id: UUID,
) -> tuple[Notification, tuple[str, str, datetime]]:
    async with factory() as session:
        notification = await session.scalar(
            select(Notification).where(
                Notification.event_key == event_key,
                Notification.user_id == user_id,
            )
        )
        assert notification is not None, f"no committed notification for {event_key}"
        row = (
            await session.execute(
                select(
                    NotificationDelivery.channel,
                    NotificationDelivery.status,
                    NotificationDelivery.scheduled_at,
                ).where(NotificationDelivery.notification_id == notification.id)
            )
        ).one()
        return notification, tuple(row)  # type: ignore[return-value]


@pytest.mark.integration
async def test_approve_redemption_records_notification(db_engine: AsyncEngine) -> None:
    """REWARD_REDEMPTION_APPROVED commits with the decision: reservation
    CONSUMED, ledger entry posted, status APPROVED — and the logical
    Notification plus its IN_APP delivery row ride the same transaction,
    with the item name and spent points rendered."""
    factory = _factory(db_engine)
    service = _recording_service()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    item_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(f"2025{run}001")
            admin = _admin(f"a{run}")
            item = _reward_item(name="定制文创套装", point_cost=200)
            session.add_all([student, admin, item])
            await session.flush()
            user_ids.extend([student.id, admin.id])
            item_ids.append(item.id)
            await LedgerService().grant_assignment_reward(
                session,
                claim_id=uuid4(),
                user_id=student.id,
                amount=500,
                ranking_effective_at=_NOW,
            )
            await session.commit()

        redemption = await _request(factory, service, user_ids[0], item_ids[0])
        async with factory() as session:
            decided = await service.approve_redemption(
                session, Actor(user_id=user_ids[1], role=Role.ADMIN), redemption.id
            )
        assert decided.status == "APPROVED"

        notification, delivery = await _notification_with_delivery(
            factory, f"redemption:{redemption.id}:approved", user_ids[0]
        )
        assert (
            notification.event_type
            == NotificationEventType.REWARD_REDEMPTION_APPROVED.value
        )
        assert "定制文创套装" in notification.body
        assert "200积分" in notification.body
        assert delivery == (
            NotificationChannel.IN_APP.value,
            DeliveryStatus.PENDING.value,
            _NOW,
        )
        # The business state the notification describes committed.
        async with factory() as session:
            reservation = await session.scalar(
                select(PointReservation).where(
                    PointReservation.redemption_id == redemption.id
                )
            )
            assert reservation is not None
            assert reservation.status == ReservationStatus.CONSUMED.value
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, item_ids=item_ids)


async def _request(
    factory: async_sessionmaker[AsyncSession],
    service: RedemptionService,
    user_id: UUID,
    reward_item_id: UUID,
) -> RewardRedemption:
    async with factory() as session:
        return await service.request_redemption(session, user_id, reward_item_id)


@pytest.mark.integration
async def test_reject_redemption_records_notification(db_engine: AsyncEngine) -> None:
    """REWARD_REDEMPTION_REJECTED commits with the decision: reservation
    RELEASED, status REJECTED, no consumption ledger entry — and the
    notification rows commit with it, with the mandatory rejection
    reason and the refunded points rendered."""
    factory = _factory(db_engine)
    service = _recording_service()
    run = uuid4().hex[:8]
    user_ids: list[UUID] = []
    item_ids: list[UUID] = []
    try:
        async with factory() as session:
            student = _student(f"2025{run}001")
            admin = _admin(f"a{run}")
            item = _reward_item(name="定制文创套装", point_cost=200)
            session.add_all([student, admin, item])
            await session.flush()
            user_ids.extend([student.id, admin.id])
            item_ids.append(item.id)
            await LedgerService().grant_assignment_reward(
                session,
                claim_id=uuid4(),
                user_id=student.id,
                amount=500,
                ranking_effective_at=_NOW,
            )
            await session.commit()

        redemption = await _request(factory, service, user_ids[0], item_ids[0])
        async with factory() as session:
            decided = await service.reject_redemption(
                session,
                Actor(user_id=user_ids[1], role=Role.ADMIN),
                redemption.id,
                "库存不足，无法发放",
            )
        assert decided.status == "REJECTED"

        notification, delivery = await _notification_with_delivery(
            factory, f"redemption:{redemption.id}:rejected", user_ids[0]
        )
        assert (
            notification.event_type
            == NotificationEventType.REWARD_REDEMPTION_REJECTED.value
        )
        assert "定制文创套装" in notification.body
        assert "库存不足，无法发放" in notification.body
        assert "200积分" in notification.body  # the refunded points
        assert delivery == (
            NotificationChannel.IN_APP.value,
            DeliveryStatus.PENDING.value,
            _NOW,
        )
        async with factory() as session:
            reservation = await session.scalar(
                select(PointReservation).where(
                    PointReservation.redemption_id == redemption.id
                )
            )
            assert reservation is not None
            assert reservation.status == ReservationStatus.RELEASED.value
    finally:
        await _committed_cleanup(factory, user_ids=user_ids, item_ids=item_ids)
