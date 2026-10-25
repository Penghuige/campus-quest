# backend/tests/unit/notifications/test_delivery_recruitment_policy.py
"""The delivery service's recruitment-reminder policy, pinned directly.

Two defensive branches guard the batch-4 reminder flow: the dispatch
policy refuses a non-IN_APP channel and a missing resolver loudly (a
composition error must never silently send a reminder through a
provider), and _finalize refuses a reminder tuple without its resolver.
Both are exercised through the service's own methods with a minimal
claim snapshot — no database, no provider.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.notifications.delivery_service import DeliveryService
from app.modules.notifications.enums import NotificationChannel, NotificationEventType


class _NullSession:
    """Minimal async session stand-in: the guard fires inside tx2 before
    any statement is issued, so no real database is needed."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def begin(self):
        return self


def _claim(*, channel: NotificationChannel) -> object:
    from app.modules.notifications.delivery_service import _Claim

    return _Claim(
        delivery_id=uuid4(),
        notification_id=uuid4(),
        user_id=uuid4(),
        event_key=f"ie-recruitment:{uuid4()}:{uuid4()}:2026-10-25T00:00:00+00:00",
        event_type=NotificationEventType.IE_RECRUITMENT_CONFIRMATION_DUE,
        channel=channel,
        attempts=0,
        scheduled_at=datetime.now(UTC),
        title="请确认招募是否继续",
        body="body",
        phone_e164=None,
        email_normalized=None,
        channel_skip_reason=None,
    )


def _service(**kwargs) -> DeliveryService:
    return DeliveryService(
        session_maker=_NullSession,  # type: ignore[arg-type]  the finalize guard fires inside tx2
        sms_sender=None,  # type: ignore[arg-type]
        email_sender=None,  # type: ignore[arg-type]
        clock=None,  # type: ignore[arg-type]
        **kwargs,
    )


async def test_non_in_app_recruitment_reminder_is_refused():
    service = _service(recruitment_reminder_resolver=lambda *a: None)
    with pytest.raises(ValueError, match="IN_APP"):
        await service._dispatch_skip_token(
            _claim(channel=NotificationChannel.EMAIL), datetime.now(UTC)
        )


async def test_missing_resolver_is_a_composition_error():
    service = _service()
    with pytest.raises(RuntimeError, match="resolver is required"):
        await service._dispatch_skip_token(
            _claim(channel=NotificationChannel.IN_APP), datetime.now(UTC)
        )


async def test_finalize_refuses_a_reminder_tuple_without_resolver():
    service = _service()
    with pytest.raises(RuntimeError, match="resolver is required"):
        await service._finalize(
            uuid4(),
            datetime.now(UTC),
            outcome=None,  # type: ignore[arg-type]  the guard fires before use
            status=None,  # type: ignore[arg-type]
            last_error=None,
            recruitment_reminder=(uuid4(), uuid4()),
        )
