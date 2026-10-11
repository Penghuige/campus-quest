"""Real job composition: discovery -> intent -> dispatch -> actual Inbox."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.clock import FrozenClock, SystemClock
from app.modules.innovation.recruitment_schemas import RecruitmentVersionCommand
from app.modules.innovation.recruitment_service import RecruitmentService
from app.modules.notifications.delivery_service import DeliveryService, SendOutcome
from app.modules.notifications.inbox_service import InboxService
from app.modules.notifications.models import Notification, NotificationDelivery
from app.workers.jobs.send_notification import (
    build_delivery_service,
    send_notification_delivery,
)
from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.test_recruitment_contact_races import open_role
from tests.integration.innovation.test_recruitment_races import CONTEXT
from tests.integration.innovation.test_recruitment_races import committed as committed

pytestmark = pytest.mark.integration


async def seed_due(committed, clock):
    from app.workers.jobs.schedule_recruitments import schedule_recruitments

    sessions, service, args = committed
    service = RecruitmentService(clock=FrozenClock(clock.now() - timedelta(days=31)))
    row = await open_role(sessions, service, args)
    result = await asyncio.to_thread(schedule_recruitments.run, "r3-composition-scan")
    assert result["reminded"] == 1
    async with sessions() as db:
        delivery = (
            await db.execute(
                select(NotificationDelivery.id).where(
                    NotificationDelivery.user_id == args["actor"].user_id
                )
            )
        ).scalar_one()
    return row, delivery


async def test_real_sender_composition_makes_one_readable_in_app(committed, clock):
    sessions, _, args = committed
    _, delivery = await seed_due(committed, clock)
    result = await asyncio.to_thread(
        send_notification_delivery.run, str(delivery), "r3-composition"
    )
    assert result["outcome"] == "SENT"
    duplicate = await asyncio.to_thread(
        send_notification_delivery.run, str(delivery), "r3-duplicate"
    )
    assert duplicate["outcome"] == "ALREADY_SENT"
    async with sessions() as db:
        items, total = await InboxService(clock=clock).list_inbox(
            db, args["actor"].user_id
        )
        assert total == 1
        assert items[0].event_type == "IE_RECRUITMENT_CONFIRMATION_DUE"
        assert "demo_partner" not in items[0].body
        assert (
            len(
                list(
                    await db.scalars(
                        select(Notification).where(
                            Notification.user_id == args["actor"].user_id
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("action", ["close", "confirm_active"])
async def test_obsolete_reminder_skipped_and_absent_from_inbox(
    committed, clock, action
):
    from uuid import uuid4

    sessions, service, args = committed
    row, delivery = await seed_due(committed, clock)
    async with sessions() as db:
        row = await service.get_owned(db, **args, recruitment_id=row.id)
        await getattr(service, action)(
            db,
            **args,
            recruitment_id=row.id,
            payload=RecruitmentVersionCommand(version=row.version, request_id=uuid4()),
            context=CONTEXT,
        )
    sender = build_delivery_service(session_maker=sessions)
    assert (await sender.send(delivery, "r3-obsolete")).outcome == SendOutcome.SKIPPED
    async with sessions() as db:
        assert (await InboxService(clock=clock).list_inbox(db, args["actor"].user_id))[
            1
        ] == 0


async def test_missing_resolver_fails_closed(committed, clock):
    sessions, _, _ = committed
    _, delivery = await seed_due(committed, clock)
    production = build_delivery_service(session_maker=sessions)
    sender = DeliveryService(
        session_maker=sessions,
        clock=SystemClock(),
        sms_sender=production._sms_sender,
        email_sender=production._email_sender,
    )
    with pytest.raises(RuntimeError, match="recruitment reminder resolver"):
        await sender.send(delivery, "r3-no-resolver")


async def test_filtered_dispatch_discovery_preserves_other_events(committed, clock):
    from app.modules.notifications.port import NotificationPort
    from app.workers.jobs.dispatch_due_notifications import collect_due_deliveries

    sessions, _, args = committed
    _, recruitment_delivery = await seed_due(committed, clock)
    async with sessions() as db:
        await NotificationPort(clock=clock).record_event(
            db,
            event_key="r3-other:" + str(args["actor"].user_id),
            event_type="ACCOUNT_SECURITY",
            user_id=args["actor"].user_id,
            payload={
                "event_summary": "模拟安全事件",
                "event_time": clock.now().isoformat(),
            },
        )
        await db.commit()
    now = SystemClock().now()
    async with sessions() as db:
        filtered = await collect_due_deliveries(
            db,
            now,
            limit=100,
            stale_after=timedelta(minutes=15),
            event_types=("IE_RECRUITMENT_CONFIRMATION_DUE",),
        )
        assert [item.delivery_id for item in filtered] == [recruitment_delivery]
        all_due = await collect_due_deliveries(
            db, now, limit=100, stale_after=timedelta(minutes=15)
        )
        assert len(all_due) == 2
