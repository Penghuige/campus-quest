"""Deliberate current contact authorization, rate cap and access audit."""

import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditLog
from tests.integration.innovation.recruitment_support import client as client
from tests.integration.innovation.recruitment_support import clock as clock
from tests.integration.innovation.recruitment_support import command
from tests.integration.innovation.test_achievement_review_operations import account
from tests.integration.innovation.test_public_recruitments import published

pytestmark = pytest.mark.integration


async def test_contacts_only_return_on_deliberate_read_and_revoke_is_immediate(
    client, db_session, clock
):
    _, _, headers, base, row = await published(client, db_session, clock)
    _, viewer = await account(db_session, clock)
    path = "/api/v1/ie/recruitments/" + row["id"]
    assert "demo_partner" not in (await client.get(path, headers=viewer)).text
    response = await client.get(path + "/contacts", headers=viewer)
    assert response.status_code == 200, response.text
    assert response.json()["contacts"] == [
        {"channel": "WECHAT", "value": "demo_partner"}
    ]
    assert response.headers["Cache-Control"] == "private, no-store"
    audits = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.action == "ie.recruitment.contacts_read")
        )
    )
    assert len(audits) == 1
    assert "demo_partner" not in str(audits[0].details)
    revoke = await client.post(
        base + "/revoke-contact",
        headers=headers,
        json=command(row) | {"channel": "WECHAT", "close_if_last": True},
    )
    assert revoke.status_code == 200
    hidden = await client.get(path + "/contacts", headers=viewer)
    assert hidden.status_code == 404
    assert "demo_partner" not in hidden.text


async def test_contact_limit_is_account_scoped_30_per_60_seconds(
    client, db_session, clock
):
    _, _, _, _, row = await published(client, db_session, clock)
    _, viewer = await account(db_session, clock)
    path = "/api/v1/ie/recruitments/" + row["id"] + "/contacts"
    for _ in range(30):
        response = await client.get(path, headers=viewer)
        assert response.status_code == 200, response.text
    blocked = await client.get(path, headers=viewer)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert "demo_partner" not in blocked.text
    from datetime import timedelta

    from app.core.clock import FrozenClock
    from app.modules.identity.dependencies import get_business_clock

    client._transport.app.dependency_overrides[get_business_clock] = lambda: (
        FrozenClock(clock.now() + timedelta(seconds=60))
    )
    assert (await client.get(path, headers=viewer)).status_code == 200


@pytest.mark.parametrize("failure", ["redis", "audit"])
async def test_contact_dependency_failure_never_discloses_values(
    client, db_session, clock, failure
):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app.modules.audit.service import AuditLogWriter
    from app.modules.innovation.public_recruitment_router import (
        get_contact_rate_limiter,
        get_public_recruitment_service,
    )
    from app.modules.innovation.public_recruitment_service import (
        PublicRecruitmentService,
    )

    _, _, _, _, row = await published(client, db_session, clock)
    _, viewer = await account(db_session, clock)
    app = client._transport.app

    class UnavailableLimiter:
        async def check(self, **kwargs):
            raise RedisConnectionError("controlled unavailable Redis")

    class UnavailableAudit(AuditLogWriter):
        async def append(self, *args, **kwargs):
            raise RuntimeError("controlled unavailable audit")

    if failure == "redis":
        app.dependency_overrides[get_contact_rate_limiter] = lambda: (
            UnavailableLimiter()
        )
    else:
        app.dependency_overrides[get_public_recruitment_service] = lambda: (
            PublicRecruitmentService(clock=clock, audit=UnavailableAudit())
        )
    response = await client.get(
        "/api/v1/ie/recruitments/" + row["id"] + "/contacts", headers=viewer
    )
    assert response.status_code == (503 if failure == "redis" else 500), response.text
    assert "demo_partner" not in response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert (
        list(
            await db_session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "ie.recruitment.contacts_read"
                )
            )
        )
        == []
    )


async def test_contact_unauthenticated_response_is_not_cacheable(client):
    from uuid import uuid4

    response = await client.get(f"/api/v1/ie/recruitments/{uuid4()}/contacts")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "private, no-store"
